"""网站信息审核应用服务 — 把审核通过的域名写入网站数据表

对应产品数据管理 → 数据导出页「AI 批量生成网站信息」卡片的审核环节：
前端以表格展示 网站信息.xlsx 的内容，用户复制到 Excel 审核修改后，
在页面中勾选「通过」并应用。对每个审核通过的网站（使用通过后的域名）：

1. 修改数据表内容：网站文件夹内每个数据表（数据分配输出结构：
   主数据表 main{分类名}_part{N}_{后缀}.xlsx / 补充数据表
   {分类名}_supp_part{N}_{后缀}.xlsx），「原站域名」列整列统一为该表的
   域名标记（每个表格互不相同，N 取自表名 _partN）：
   - 主数据表   -> {域名}_main_part{N}
   - 补充数据表 -> {域名}_part{N}
   （ERP 站群按整列一致的标记识别站点与分卷；老 BB 工具拆表输出
   即为整列统一形态，混合来源域名会被服务器以「表错了」拒绝。）
2. 修改数据表名：表名最前面加 data_ 前缀（如 data_main....xlsx），
   用于后续上传数据时识别——只上传网站数据表（data_ 前缀）。
3. 网站文件夹改名为审核通过的域名（Toilet_Tank_Lid -> tanklidpro.com），
   网站信息.xlsx 行的「网站（文件夹）」同步更新，保证后续可定位。
4. 分类统计.xlsx / 网站信息.xlsx 为结果文件，不参与修改；
   重复应用（改域名后重新审核）时只更新标记，不重复加前缀（幂等）。
"""

import re
from datetime import datetime
from pathlib import Path

from qmds.modules.web.services.category_stats import INFO_FILE_NAME, STATS_FILE_NAME
from qmds.modules.web.services.site_info_generator import (
    _write_info_excel,
    read_site_info_excel,
)
from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.site_review")

# 数据表中标记「来源站域名」的列
ORIGIN_COLUMN = "原站域名"

# 数据表名前缀（审核应用后）：后续上传只识别 data_ 前缀的网站数据表
DATA_PREFIX = "data_"

# 「原站域名」列整列统一为该表标记（ERP 按整列一致的标记识别站点）

# 应用审核时回写 网站信息.xlsx 的字段映射（前端字段 -> 表格列）
_INFO_FIELD_MAP = (
    ("domain", "域名"),
    ("title", "标题"),
    ("description", "描述"),
    ("theme", "主题"),
    ("address", "地址"),
    ("keywords", "关键词"),
)


def _base_name(path: Path) -> str:
    """去掉 data_ 前缀的原始表名（识别/排序用原始名，保证重复应用一致）"""
    name = Path(path).name
    return name[len(DATA_PREFIX):] if name.startswith(DATA_PREFIX) else name


def _natural_key(s: str) -> list:
    """自然排序键：数字段按数值比较（part2 < part10）"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def _table_kind(base: str) -> str:
    """数据表类型：main 开头 = 主数据表；含 _supp（及其余）= 补充数据表"""
    return "main" if base.lower().startswith("main") else "supp"


def _part_number(base: str, fallback: int) -> int:
    """从表名提取分卷号 _part{N}；无标记时用排序序号（fallback，1 起）"""
    m = re.search(r"_part(\d+)", base)
    return int(m.group(1)) if m else fallback


def collect_data_tables(site_folder) -> dict:
    """收集网站文件夹内的数据表（跳过结果文件/临时文件）

    返回 {"main": [主数据表...], "supp": [补充数据表...]}，
    各组按原始表名自然排序；已加 data_ 前缀的表按去前缀后的原始名参与排序。
    """
    folder = Path(site_folder)
    mains: list[Path] = []
    supps: list[Path] = []
    if not folder.is_dir():
        return {"main": [], "supp": []}
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() != ".xlsx":
            continue
        if p.name.startswith("~$"):
            continue
        base = _base_name(p)
        if base in (STATS_FILE_NAME, INFO_FILE_NAME):
            continue
        (mains if _table_kind(base) == "main" else supps).append(p)
    mains.sort(key=lambda p: _natural_key(_base_name(p)))
    supps.sort(key=lambda p: _natural_key(_base_name(p)))
    return {"main": mains, "supp": supps}


def is_site_applied(site_folder) -> bool:
    """网站是否已应用过审核（任一数据表已带 data_ 前缀）"""
    tables = collect_data_tables(site_folder)
    return any(p.name.startswith(DATA_PREFIX)
               for group in tables.values() for p in group)


def locate_info_file(root):
    """定位所选文件夹下的 网站信息.xlsx（所选可以是其任意上级目录）

    优先所选文件夹本身；否则递归查找（如所选是 日期/大类 目录时，
    表格在其下的分配文件夹内）；多个时取路径最浅的。找不到返回 None。
    """
    root = Path(root)
    if not root.is_dir():
        return None
    direct = root / INFO_FILE_NAME
    if direct.is_file():
        return direct
    try:
        matches = [p for p in root.rglob(INFO_FILE_NAME) if p.is_file()]
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return matches[0]


def locate_site_folder(root, name):
    """定位网站数据文件夹（所选文件夹可以是网站的任意上级目录）

    查找顺序：
    1. root 下的直接同名子文件夹（所选 = 分配文件夹的常见情况）；
    2. root 自身即该网站（单网站文件夹）；
    3. 递归查找 root 下任意层级的同名子文件夹（所选文件夹是更上层
       的父目录，如 日期/大类 目录；同名时取路径最浅的一个）。
    """
    root = Path(root)
    name = str(name or "").strip()
    # 防御：名称为空或含路径分隔符（防止越出 root）
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    if not root.is_dir():
        return None
    cand = root / name
    if cand.is_dir():
        return cand
    if root.name == name:
        return root
    # 深层嵌套：递归查找同名子文件夹
    try:
        matches = [d for d in root.rglob("*")
                   if d.is_dir() and d.name == name]
    except OSError:
        return None
    if not matches:
        return None
    # 同名时取路径最浅、字典序最前的（确定性）
    matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return matches[0]


def apply_domain_to_site(site_folder, domain: str,
                         log_fn=None, stop_check=None) -> dict:
    """把审核通过的域名写入一个网站的数据表并改文件夹名

    - 每个数据表的「原站域名」列整列统一为该表标记：
      主数据 {domain}_main_part{N} / 补充数据 {domain}_part{N}
    - 表名加 data_ 前缀（已加过的不重复加）
    - 网站文件夹改名为域名（如 Toilet_Tank_Lid -> tanklidpro.com；
      已是域名时不重复改名，换域名重新应用时改到新域名）

    返回 {"main": 主数据标记数, "supp": 补充标记数, "renamed": 重命名数,
          "markers": [标记...], "folder": 改名后的文件夹路径}；
    域名为空或无数据表时抛 ValueError。
    """
    import pandas as pd

    folder = Path(site_folder)
    domain = str(domain or "").strip().lower()
    if not domain:
        raise ValueError("域名为空")
    tables = collect_data_tables(folder)
    if not tables["main"] and not tables["supp"]:
        raise ValueError("文件夹中没有数据表（.xlsx）")

    stats = {"main": 0, "supp": 0, "renamed": 0, "markers": []}
    for kind in ("main", "supp"):
        for idx, path in enumerate(tables[kind], start=1):
            if stop_check and stop_check():
                raise InterruptedError
            base = _base_name(path)
            n = _part_number(base, idx)
            marker = (f"{domain}_main_part{n}" if kind == "main"
                      else f"{domain}_part{n}")

            # ── 原站域名列整列统一为该表标记 ──
            # ERP 站群按整列一致的 域名_..._partN 识别目标站点与分卷
            # （老 BB 工具拆表输出的形态）；列中混有其他来源域名时
            # 服务器返回「表错了」。
            df = pd.read_excel(path, engine="openpyxl")
            if ORIGIN_COLUMN not in df.columns:
                if log_fn:
                    log_fn(f"[{folder.name}] ⚠ {path.name} 缺少"
                           f"「{ORIGIN_COLUMN}」列，跳过域名标记（仍重命名）",
                           "warning")
            else:
                df[ORIGIN_COLUMN] = marker
                df.to_excel(path, index=False, engine="openpyxl")
                stats[kind] += 1
                if log_fn:
                    log_fn(f"[{folder.name}] {path.name}: {ORIGIN_COLUMN} "
                           f"整列 {len(df)} 行 -> {marker}")

            # ── 表名加 data_ 前缀（幂等） ──
            if not path.name.startswith(DATA_PREFIX):
                new_path = path.with_name(DATA_PREFIX + path.name)
                if new_path.exists():
                    raise FileExistsError(f"目标表名已存在: {new_path.name}")
                path.rename(new_path)
                stats["renamed"] += 1
            stats["markers"].append(marker)

    # ── 网站文件夹改名为域名（幂等） ──
    new_folder = folder.with_name(domain)
    if folder.name != domain:
        if new_folder.exists():
            raise FileExistsError(f"目标文件夹已存在: {new_folder.name}")
        folder.rename(new_folder)
        if log_fn:
            log_fn(f"[{folder.name}] 网站文件夹已改名为域名: {new_folder.name}")
    stats["folder"] = new_folder
    return stats


def apply_site_review_task(task_id: str, folder, sites: list):
    """应用网站信息审核后台任务体

    对每个审核通过的网站（sites 元素含 folder 与通过后的 domain 等
    前端编辑字段）：用域名修改其数据表（整列原站域名标记 + data_ 前缀），
    并把审核结果（域名等编辑值 + 「审核通过」备注）回写 网站信息.xlsx。
    单个网站失败记录错误日志并继续，最后汇总。

    Args:
        task_id: 任务 ID
        folder: 所选数据文件夹（网站信息.xlsx 所在处，各网站为其子文件夹）
        sites: [{"folder": 网站文件夹名, "domain": 通过后的域名,
                 "title"/"description"/"theme"/"address"/"keywords": 编辑值}, ...]
    """
    root = Path(folder)

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        task_manager.update(task_id, status="running",
                            message=f"开始应用网站信息审核: {root.name}")
        _log(f"任务启动: 应用网站信息审核 -> {root}（{len(sites)} 个网站）")

        # ── 读取 网站信息.xlsx（存在时审核结果回写）──
        # 所选文件夹可能是网站信息的上级目录（如日期/大类目录），递归定位
        info_path = locate_info_file(root)
        info_rows = None
        if info_path is not None:
            try:
                info_rows = read_site_info_excel(info_path)
                _log(f"读取 {info_path.relative_to(root)}: {len(info_rows)} 行")
            except Exception as e:
                _log(f"读取 {INFO_FILE_NAME} 失败（不回写审核结果）: {e}",
                     "warning")

        # ── 逐个网站应用 ──
        applied: list[str] = []
        failed: list[tuple[str, str]] = []
        total_tables = 0
        for i, site in enumerate(sites):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            name = str(site.get("folder") or "").strip()
            domain = str(site.get("domain") or "").strip()
            task_manager.update(
                task_id, progress=int(i / len(sites) * 100),
                message=f"[{i + 1}/{len(sites)}] 正在应用: {name}")
            try:
                if not name or not domain:
                    raise ValueError("网站名或域名为空")
                site_folder = locate_site_folder(root, name)
                if site_folder is None:
                    raise FileNotFoundError(f"未找到网站数据文件夹: {name}")
                _log(f"[{i + 1}/{len(sites)}] 应用审核: {name}（域名 {domain}）")
                st = apply_domain_to_site(
                    site_folder, domain, log_fn=_log,
                    stop_check=lambda: task_manager.is_stopped(task_id))
                total_tables += st["main"] + st["supp"]
                applied.append(name)
                new_name = st["folder"].name
                _log(f"[{i + 1}/{len(sites)}] ✓ {name} 完成: "
                     f"主数据 {st['main']} 表 / 补充数据 {st['supp']} 表已标记，"
                     f"重命名 {st['renamed']} 表"
                     + (f"，文件夹改名 {new_name}" if new_name != name else ""))

                # 审核结果回写 网站信息.xlsx 的对应行（文件夹名同步为改名后的域名）
                if info_rows is not None:
                    _merge_review_into_row(info_rows, site, new_name)
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            except Exception as e:
                failed.append((name or "?", str(e)))
                log.error(f"网站 {name} 审核应用失败: {e}")
                _log(f"[{i + 1}/{len(sites)}] ✗ {name} 应用失败: {e}（继续下一个）",
                     "error")

        # ── 保存回写后的 网站信息.xlsx ──
        if info_rows is not None and applied:
            try:
                _write_info_excel(info_path, info_rows)
                _log(f"{INFO_FILE_NAME} 已更新（{len(applied)} 个网站标记审核通过）")
            except Exception as e:
                _log(f"回写 {INFO_FILE_NAME} 失败: {e}", "warning")

        # ── 汇总 ──
        parts = [f"完成: 已应用 {len(applied)}/{len(sites)} 个网站审核"
                 f"（{total_tables} 个数据表已标记域名）"]
        if failed:
            parts.append(f"（失败 {len(failed)}: "
                         + ", ".join(n for n, _ in failed) + "）")
        summary = "".join(parts)
        if not applied and failed:
            task_manager.update(task_id, status="failed", message=summary,
                                progress=100)
            _log(f"任务失败: {summary}", "error")
        else:
            task_manager.update(task_id, status="completed", message=summary,
                                progress=100)
            _log(summary)

    except Exception as e:
        log.error(f"应用网站信息审核任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")


def _merge_review_into_row(info_rows: list, site: dict, new_name: str = ""):
    """把审核通过的编辑值 + 审核备注合入 网站信息 行（按网站文件夹名匹配）

    new_name 非空时（审核应用后文件夹已改名为域名），行的「网站（文件夹）」
    同步更新为新文件夹名，保证后续表格加载/再次应用能定位到该文件夹。
    """
    name = str(site.get("folder") or "").strip()
    for row in info_rows:
        if str(row.get("网站（文件夹）") or "").strip() == name:
            for key, col in _INFO_FIELD_MAP:
                val = str(site.get(key) or "").strip()
                if val:
                    row[col] = val
            row["备注"] = f"审核通过 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            # 文件夹已改名为域名 -> 行的文件夹列同步（保持可定位）
            if new_name:
                row["网站（文件夹）"] = new_name
            return
