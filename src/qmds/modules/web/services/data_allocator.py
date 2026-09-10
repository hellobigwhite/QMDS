"""数据分配服务 — 导出表格的主分类拆分与补充数据分配

流程（对应产品数据管理 → 数据导出页的"数据分配"卡片）：
1. 选择导出后的 Excel 表格，流式统计分类列（Categories）中每个分类的产品数量；
2. 由用户勾选可作为主分类的类目；
3. 生成方案：
   - 每个主分类的数据单独写入主数据表，表名以 main 开头（main{分类名}）；
   - 原表格去除主分类后的剩余数据分为两部分：
     * 数量超过拆分阈值（默认 3000）的大分类按比例均匀拆分到每一份补充表
       （不会整类分给单一主分类，每份分得 floor(n/P) 或 ceil(n/P) 条）；
     * 其余小分类保持整类不拆散，LPT 贪心装箱；
     * 每份补充表目标约 4~5 万条，尽量均衡；
   - 每个主分类分配一份补充表，多余的份作为额外补充（extra）输出；
4. 全部表格写入原表格同目录下的 {原文件名}_分配_{时间戳} 文件夹，
   每个主分类一个数据文件夹（以分类命名）：主数据表 main{分类名}.xlsx 与
   补充数据表 {分类名}_supp.xlsx（命名不含中文）放在同一文件夹；
   额外补充放入 extra{N} 文件夹。启用批量拆表后拆分结果仍在各自文件夹内。

方案计算为纯函数（plan_allocation），便于单元测试；任务执行体
run_allocation_task 供路由以后台线程方式启动。
"""

import heapq
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.data_allocator")

# 分类列候选名（导出列为 Categories，兼容历史"分类"列）
CATEGORY_COLUMN_CANDIDATES = ("Categories", "分类", "Category", "category")

# 分类统计接口返回的分类数上限（超出部分截断，仅影响前端列表，不影响分配任务）
MAX_API_CATEGORIES = 10000

# 大分类拆分阈值默认值：数量超过该值的分类将拆分后均匀分配到各补充表
DEFAULT_SPLIT_THRESHOLD = 3000


def detect_category_column(columns) -> str | None:
    """在表头中查找分类列名，返回实际列名；找不到返回 None"""
    cols = list(columns)
    for cand in CATEGORY_COLUMN_CANDIDATES:
        if cand in cols:
            return cand
    lower_map = {str(c).lower(): c for c in cols if c is not None}
    for cand in ("categories", "分类"):
        if cand in lower_map:
            return lower_map[cand]
    return None


def sanitize_filename(name: str) -> str:
    """分类名 -> 安全文件名（||| 与空格、Windows 非法字符统一转下划线）

    例: "Hardware|||Plumbing & Fittings" -> "Hardware_Plumbing_&_Fittings"
    """
    s = str(name).replace("|||", " ")
    s = re.sub(r'[<>:"/\\|?*\s]+', "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "Unnamed"


def count_excel_categories(filepath: Path):
    """流式统计 Excel 分类列，避免整表载入内存

    返回 (分类列名, 数据总行数, [(分类, 数量) 按数量降序])。
    分类为空的行计入总行数（参与补充数据分配），但不计入分类列表。
    """
    from openpyxl import load_workbook

    wb = load_workbook(filepath, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError("表格为空或格式不正确")

        col = detect_category_column(header)
        if col is None:
            raise ValueError("表格中未找到分类列（Categories/分类）")

        idx = list(header).index(col)
        counter: Counter = Counter()
        total = 0
        for row in rows:
            if row is None:
                continue
            if all(v is None or str(v).strip() == "" for v in row):
                continue  # 跳过整行空行
            total += 1
            if idx < len(row):
                val = row[idx]
                if val is not None:
                    sval = str(val).strip()
                    if sval:
                        counter[sval] += 1

        cats = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        return col, total, cats
    finally:
        wb.close()


def _pack_portions(remaining_counts: dict, portion_count: int,
                   split_threshold: int) -> list[dict]:
    """把剩余分类打包进 portion_count 份补充表，尽量均衡

    - 数量 <= split_threshold 的小分类保持整类不拆散（LPT 贪心装箱）；
    - 数量 > split_threshold 的大分类按比例均匀拆分到每一份：
      每份分得 floor(n/P) 或 ceil(n/P) 条（最大余数法），
      余下的零头优先补到当前总量最少的份。
      同一分类会以分片形式出现在多份中，不会整类分给单一一份。

    返回按总量降序排列的 [{categories: {名: 数}, total: int}, ...]。
    """
    portions: list[dict] = [{"categories": {}, "total": 0} for _ in range(portion_count)]

    # 1) 小分类：LPT 贪心（数量降序），每次放入当前总量最少的份
    smalls = [(c, n) for c, n in remaining_counts.items() if n <= split_threshold]
    heap = [(0, i) for i in range(portion_count)]
    heapq.heapify(heap)
    for name, count in sorted(smalls, key=lambda x: (-x[1], x[0])):
        _, idx = heapq.heappop(heap)
        portions[idx]["categories"][name] = count
        portions[idx]["total"] += count
        heapq.heappush(heap, (portions[idx]["total"], idx))

    # 2) 大分类：按比例均匀拆分（最大余数法，零头补到当前总量最少的份）
    larges = [(c, n) for c, n in remaining_counts.items() if n > split_threshold]
    for name, count in sorted(larges, key=lambda x: (-x[1], x[0])):
        base, rem = divmod(count, portion_count)
        order = sorted(range(portion_count),
                       key=lambda i: (portions[i]["total"], i))
        extra = set(order[:rem])
        for i in range(portion_count):
            chunk = base + (1 if i in extra else 0)
            if chunk <= 0:
                continue
            portions[i]["categories"][name] = chunk
            portions[i]["total"] += chunk

    portions = [p for p in portions if p["total"] > 0]
    portions.sort(key=lambda p: (-p["total"], sorted(p["categories"])[:1]))
    return portions


def plan_allocation(category_counts: dict, main_categories: list,
                    min_size: int = 40000, max_size: int = 50000,
                    split_threshold: int = DEFAULT_SPLIT_THRESHOLD) -> dict:
    """计算数据分配方案（纯函数）

    参数:
        category_counts: {分类: 数量}（空分类名 "" 也计入，其数据参与补充分配）
        main_categories: 用户勾选的主分类列表
        min_size / max_size: 每份补充表的目标条数范围
        split_threshold: 大分类拆分阈值。数量超过该值的分类将按比例均匀
            拆分到每一份补充表（不整类分给单一主分类）；数量 <= 该值的
            分类保持整类不拆散。

    返回:
        {
            "main_tables": [{category, count}] 按数量降序,
            "remaining_total": 剩余总行数,
            "portion_count": 补充表份数,
            "target_size": 每份目标条数（约）,
            "portions": [{categories: {名: 数}, total}] 按总量降序
                        （大分类以分片形式出现在多份中）,
            "split_categories": [{category, count}] 将被拆分的大分类,
            "split_threshold": 拆分阈值,
            "warnings": [提示文本, ...],
        }
    """
    warnings: list[str] = []

    main_set = set()
    mains: list[tuple[str, int]] = []
    for cat in main_categories:
        cat = str(cat).strip()
        if not cat or cat == "__all__":
            continue
        cnt = int(category_counts.get(cat, 0))
        if cnt <= 0:
            warnings.append(f"主分类 “{cat}” 在表格中不存在或无数据，已跳过")
            continue
        if cat in main_set:
            continue
        main_set.add(cat)
        mains.append((cat, cnt))
    mains.sort(key=lambda x: (-x[1], x[0]))

    if not mains:
        raise ValueError("没有有效的主分类（所选分类在表格中均无数据）")

    remaining_counts = {c: n for c, n in category_counts.items() if c not in main_set}
    remaining_total = sum(remaining_counts.values())

    portions: list[dict] = []
    target_size = 0
    split_categories: list[dict] = []
    if remaining_total > 0:
        # 份数规则：至少每个主分类一份；数据多于 max_size×份数时增加份数，
        # 保证每份目标条数不超过 max_size。
        portion_count = max(len(mains), math.ceil(remaining_total / max_size), 1)
        portions = _pack_portions(remaining_counts, portion_count, split_threshold)
        target_size = round(remaining_total / portion_count)

        # 将被拆分的大分类（只有 1 份时无处可拆，不列出）
        if portion_count > 1:
            split_categories = [
                {"category": c, "count": n}
                for c, n in sorted(
                    ((c, n) for c, n in remaining_counts.items() if n > split_threshold),
                    key=lambda x: (-x[1], x[0]))
            ]

        if target_size < min_size:
            warnings.append(
                f"剩余数据较少：分为 {portion_count} 份后每份约 {target_size} 条，"
                f"低于最少目标 {min_size} 条")
        oversize = [p for p in portions if p["total"] > max_size]
        if oversize:
            warnings.append(
                f"{len(oversize)} 份补充表超过 {max_size} 条"
                f"（分类粒度限制，可调低拆分阈值或提高每份最多条数）")
        undersize = [p for p in portions if p["total"] < min_size]
        if undersize and target_size >= min_size:
            warnings.append(
                f"{len(undersize)} 份补充表低于 {min_size} 条（分类粒度限制，已尽量均衡）")
    else:
        warnings.append("去除主分类后没有剩余数据，将只生成主分类表格")

    return {
        "main_tables": [{"category": c, "count": n} for c, n in mains],
        "remaining_total": remaining_total,
        "portion_count": len(portions),
        "target_size": target_size,
        "portions": portions,
        "split_categories": split_categories,
        "split_threshold": split_threshold,
        "warnings": warnings,
    }


def run_allocation_task(task_id: str, file_path: Path, main_categories: list,
                        min_size: int = 40000, max_size: int = 50000,
                        split_threshold: int = DEFAULT_SPLIT_THRESHOLD,
                        split_options: dict | None = None):
    """数据分配后台任务体：读取表格 -> 计算方案 -> 写出主分类表与补充表
    -> 统计各网站数据文件夹的分类结构

    输出目录为原表格所在文件夹下的 {原文件名}_分配_{时间戳}，按主分类分文件夹：
    - {分类名}/main{分类名}.xlsx     主数据表（main 前缀）
    - {分类名}/{分类名}_supp.xlsx    该主分类的补充数据表（命名不含中文）
    - extra{N}/extra{N}.xlsx         额外补充（未绑定主分类）
    主数据与对应补充数据放在同一文件夹。

    剩余数据中数量 > split_threshold 的大分类按比例拆分到各补充表，
    每份分得该分类中的一段连续行（按原表顺序依次截取，不重不漏）；
    其余小分类整类打包不拆散。

    split_options（分配后批量拆表，移植自 BB 批量拆表工具）:
        enabled: 是否启用（默认 False）
        supp_rows_per_file: 补充数据每份最大行数（默认 5000；主数据不设上限）
        suffix_mode: 原站域名后缀模式 none/custom/part（默认 none）
        custom_suffix: 自定义后缀内容
        remove_source: 拆分后删除原表格（默认 True）
    拆分在各自文件夹内进行，文件名
    {表格名}_part{N}_{随机字母}{时间戳}.xlsx，与 BB 工具一致。

    分配（含拆表）完成后对每个主分类数据文件夹（= 每个网站，
    含主数据表与补充数据表）汇总分类及产品数，生成该文件夹下的
    分类统计.xlsx（见 category_stats.py），供后续 AI 生成网站信息读取。
    单个文件夹统计失败不影响整体任务。
    """
    import pandas as pd

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        task_manager.update(task_id, status="running",
                            message=f"开始数据分配: {file_path.name}")
        _log(f"任务启动: 数据分配 {file_path.name}")

        if task_manager.is_stopped(task_id):
            task_manager.update(task_id, status="stopped", message="任务已停止")
            return

        _log(f"读取表格: {file_path.name}")
        df = pd.read_excel(file_path, engine="openpyxl")
        cat_col = detect_category_column(df.columns)
        if cat_col is None:
            raise ValueError("表格中未找到分类列（Categories/分类）")

        cat_values = df[cat_col].fillna("").astype(str).str.strip()
        counts = Counter(cat_values)
        category_counts = dict(counts)  # 含 ""（空分类），其数据参与补充分配

        plan = plan_allocation(category_counts, main_categories,
                               min_size, max_size, split_threshold)

        main_tables = plan["main_tables"]
        portions = plan["portions"]
        out_dir = file_path.parent / (
            f"{file_path.stem}_分配_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── 输出方案日志 ─────────────────────────────
        _log(f"共 {len(df)} 行数据，分类列: {cat_col}，唯一分类 {len(category_counts)} 个")
        for t in main_tables:
            _log(f"主分类: {t['category']}（{t['count']} 条）")
        if plan["split_categories"]:
            _log(f"拆分阈值 {split_threshold} 条：{len(plan['split_categories'])} 个大分类"
                 f"将均匀拆分到各补充表（不整类分给单一主分类）")
            for s in plan["split_categories"][:10]:
                _log(f"  拆分: {s['category']}（{s['count']} 条）")
            if len(plan["split_categories"]) > 10:
                _log(f"  ……以及其他 {len(plan['split_categories']) - 10} 个大分类")
        if portions:
            _log(f"剩余数据 {plan['remaining_total']} 条，分为 {len(portions)} 份补充表，"
                 f"每份目标约 {plan['target_size']} 条")
            for i, p in enumerate(portions):
                assignee = (main_tables[i]["category"] if i < len(main_tables)
                            else f"额外补充（未绑定主分类）")
                _log(f"补充表 {i + 1}/{len(portions)} -> {assignee}: "
                     f"约 {p['total']} 条（{len(p['categories'])} 个分类）")
        for w in plan["warnings"]:
            _log(w, "warning")

        # ── 写出文件 ─────────────────────────────
        # 分类 -> 行位置索引；大分类拆分时按分片依次截取连续行，保证不重不漏
        groups = df.groupby(cat_values, sort=False).indices
        cursors: dict = {}

        def take_portion_rows(portion):
            """按分片取出一份补充表的行（同一分类的多份分片按原表顺序依次截取）"""
            idxs = []
            for cat, chunk in portion["categories"].items():
                arr = groups.get(cat)
                if arr is None or chunk <= 0:
                    continue
                start = cursors.get(cat, 0)
                take = arr[start:start + chunk]
                cursors[cat] = start + len(take)
                idxs.extend(take.tolist())
            idxs.sort()
            return df.iloc[idxs] if idxs else df.iloc[0:0]

        used_folders: set[str] = set()

        def category_folder(base: str) -> Path:
            """每个主分类/额外补充一个独立数据文件夹（防重名，Windows 大小写不敏感）"""
            name = base
            k = 2
            while name in used_folders or (out_dir / name).exists():
                name = f"{base}_{k}"
                k += 1
            used_folders.add(name)
            folder = out_dir / name
            folder.mkdir(parents=True, exist_ok=True)
            return folder

        # 拆分配置（分配后批量拆表）
        split_opts = split_options or {}
        split_enabled = bool(split_opts.get("enabled"))
        supp_rows_per_file = int(split_opts.get("supp_rows_per_file") or 5000)
        suffix_mode = split_opts.get("suffix_mode", "none")
        custom_suffix = split_opts.get("custom_suffix") or None
        remove_source = bool(split_opts.get("remove_source", True))

        write_steps = len(main_tables) + len(portions)
        split_steps = write_steps if split_enabled else 0
        stats_steps = len(main_tables)  # 每个主分类文件夹一份网站分类统计
        total_steps = 1 + write_steps + split_steps + stats_steps
        done = 1
        task_manager.update(task_id, progress=int(done / total_steps * 100),
                            message=f"已读取 {len(df)} 行，开始写出表格")

        # (路径, 每份行数(None=主数据不设上限), 预期行数, 所在文件夹)
        written: list[tuple[Path, int | None, int, Path]] = []
        main_folders: list[Path] = []

        for t in main_tables:
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            sub = df[cat_values == t["category"]]
            s = sanitize_filename(t["category"])
            folder = category_folder(s)
            main_folders.append(folder)
            # 主数据表: main 前缀 + 分类名，与补充数据同文件夹
            path = folder / f"main{s}.xlsx"
            sub.to_excel(path, index=False, engine="openpyxl")
            done += 1
            task_manager.update(task_id, progress=int(done / total_steps * 100))
            _log(f"主数据表: {path.relative_to(out_dir)}（{len(sub)} 条）")
            # 主数据不设上限（整表一份）
            written.append((path, None, len(sub), folder))

        extra_index = 0
        for i, portion in enumerate(portions):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            sub = take_portion_rows(portion)
            if i < len(main_tables):
                main_cat = main_tables[i]["category"]
                folder = main_folders[i]
                # 补充数据表: 分类名_supp（命名不含中文），与主数据同文件夹
                path = folder / f"{sanitize_filename(main_cat)}_supp.xlsx"
                desc = f"补充表 -> {main_cat}（与主数据同文件夹）"
            else:
                extra_index += 1
                folder = category_folder(f"extra{extra_index}")
                path = folder / f"extra{extra_index}.xlsx"
                desc = "额外补充表（未绑定主分类）"
            sub.to_excel(path, index=False, engine="openpyxl")
            done += 1
            task_manager.update(task_id, progress=int(done / total_steps * 100))
            _log(f"{desc}: {path.relative_to(out_dir)}（{len(sub)} 条，{len(portion['categories'])} 个分类）")
            # 补充数据按每份行数拆分
            written.append((path, supp_rows_per_file, len(sub), folder))

        # ── 分配后批量拆表（主数据不设上限，补充数据按每份行数拆分） ──
        if split_enabled and written:
            from qmds.modules.web.services.excel_splitter import split_excel_file

            _log(f"开始批量拆表: 主数据不设上限，补充数据每份 {supp_rows_per_file} 条"
                 f"（后缀模式: {suffix_mode}）")
            for path, limit, expected, folder in written:
                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return
                base_done = done

                def _cb(pct, _base=base_done):
                    task_manager.update(
                        task_id,
                        progress=int(min(100, (_base + pct / 100) / total_steps * 100)))

                try:
                    result = split_excel_file(
                        path, rows_per_file=limit, suffix_mode=suffix_mode,
                        custom_suffix=custom_suffix, remove_source=remove_source,
                        progress_callback=_cb,
                        stop_check=lambda: task_manager.is_stopped(task_id),
                        expected_rows=expected, output_folder=folder)
                except InterruptedError:
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                done += 1
                kind = "主数据" if limit is None else "补充数据"
                _log(f"拆表完成[{kind}]: {path.stem} -> {folder.name}/"
                     f"（{len(result['parts'])} 份，共 {result['rows']} 条）")

        # ── 分配后网站分类统计（每个主分类文件夹 = 一个网站） ──
        stats_written = 0
        if main_folders:
            from qmds.modules.web.services.category_stats import (
                STATS_FILE_NAME,
                aggregate_folder_categories,
                collect_stats_files,
                write_stats_excel,
            )

            _log(f"开始网站分类统计: {len(main_folders)} 个网站数据文件夹"
                 f"（每个文件夹生成 {STATS_FILE_NAME}，供 AI 生成网站信息使用）")
            for folder in main_folders:
                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return
                try:
                    files = collect_stats_files(folder)
                    agg = aggregate_folder_categories(
                        files, log_fn=_log,
                        stop_check=lambda: task_manager.is_stopped(task_id))
                    if not agg["files"]:
                        _log(f"网站分类统计跳过 {folder.name}: 文件夹中没有可统计的表格",
                             "warning")
                        continue
                    write_stats_excel(folder / STATS_FILE_NAME, agg,
                                      folder_label=folder.name)
                    stats_written += 1
                    done += 1
                    classified = sum(agg["counts"].values())
                    task_manager.update(
                        task_id, progress=int(done / total_steps * 100),
                        message=(f"网站分类统计 {stats_written}/{len(main_folders)}: "
                                 f"{folder.name}（{len(agg['counts'])} 个分类）"))
                    _log(f"网站分类统计完成: {folder.name}"
                         f"（{len(agg['counts'])} 个分类，{classified} 条产品"
                         f"，{len(agg['files'])} 个表格）-> {STATS_FILE_NAME}")
                except InterruptedError:
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return
                except Exception as e:
                    # 单个文件夹统计失败不影响整体分配结果
                    _log(f"网站分类统计失败 {folder.name}: {e}", "error")

        file_count = len(main_tables) + len(portions)
        folder_count = len({folder for _, _, _, folder in written}) or len(main_folders)
        split_note = "，已批量拆表" + ("，原表格已删除" if remove_source else "")             if split_enabled and written else ""
        stats_note = (f"，已生成 {stats_written} 份网站分类统计（{STATS_FILE_NAME}）"
                      if stats_written else "")
        summary = (f"完成: 共生成 {file_count} 个表格，分属 {folder_count} 个数据文件夹"
                   f"（主数据与对应补充数据同文件夹）{split_note}{stats_note}"
                   f" -> {out_dir.name}")
        task_manager.update(task_id, status="completed", message=summary, progress=100)
        _log(f"任务完成: 共生成 {file_count} 个表格（主数据 {len(main_tables)} + "
             f"补充 {len(portions)}），{folder_count} 个数据文件夹"
             + split_note
             + stats_note
             + f"，输出目录: {out_dir}")

    except Exception as e:
        log.error(f"数据分配任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")


def resolve_export_file(folder: str, filename: str) -> Path:
    """解析并校验 exports 目录下的表格文件路径（防目录穿越）"""
    from qmds.config import settings

    export_dir = (settings.data_dir / "exports").resolve()
    folder_path = (export_dir / folder).resolve()
    if not folder_path.is_relative_to(export_dir) or not folder_path.is_dir():
        raise FileNotFoundError("文件夹不存在")
    file_path = (folder_path / filename).resolve()
    if not file_path.is_relative_to(export_dir):
        raise FileNotFoundError("文件不存在")
    if file_path.suffix.lower() != ".xlsx" or file_path.name.startswith("~$"):
        raise FileNotFoundError("文件不存在")
    if not file_path.is_file():
        raise FileNotFoundError("文件不存在")
    return file_path
