"""AI 生成网站信息服务 — 读取分类统计表，调用 LLM 生成建站信息（批量）

对应产品数据管理 → 数据导出页的「AI 生成网站信息」卡片（位于数据分配之下）。

批量流程（run_batch_site_info_task）：
1. 选择一个父文件夹（如数据分配输出的分配文件夹），遍历其中所有
   「最后一层文件夹」（没有子文件夹的叶子目录，含 .xlsx 数据表格）——
   每个叶子文件夹 = 一个网站的数据（数据分配后每个主分类一个文件夹）；
   extra 前缀文件夹是未绑定主分类的额外补充，不属于网站，自动跳过；
2. 按顺序逐个处理每个网站（每次只取一个网站的分类结构，不会把
   所有网站的分类一次性塞给模型）：
   a. 读取该文件夹下的 分类统计.xlsx（category_stats 生成；数据分配后
      已自动生成；不存在时自动扫描文件夹内全部表格补生成）；
   b. 以该网站的分类结构（分类 + 产品数）+ 主类目为上下文构造提示词；
   c. 调用 LLM（默认 AgentRouter https://agentrouter.org/ 平台的模型）
      生成英文网站信息（面向美国用户）：
      - domain      网站域名（结合主类目生成，.com）
      - theme       网站主题
      - title       网站标题（SEO）
      - description 网站描述（meta description）
      - address     美国地址（ERP 建站需要的 store address）
      - keywords    SEO 关键词列表
   d. 每个网站的结果汇总为一行，全部写入所选文件夹下的 网站信息.xlsx
      （每个网站一行：主类目/域名/标题/描述/主题/地址/关键词/产品数等），
      供后续建站流程读取；
3. 每完成一个网站就落盘一次（中断也能保留已生成的行）；单个网站
   生成失败不影响其余网站（失败行在「备注」列记录原因，继续下一个）。

调用模式参考 ai_menu_builder：同步 OpenAI + 3 次重试 + 格式警告。
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from qmds.config.llm_models import (
    DEFAULT_AGENTROUTER_MODEL,
    chat_completion_with_fallback,
    extract_llm_text,
    get_llm_api_key,
    get_llm_default_headers,
    get_llm_model_config,
    get_llm_system_message,
)
from qmds.modules.web.services.category_stats import (
    INFO_FILE_NAME,
    STATS_FILE_NAME,
    aggregate_folder_categories,
    collect_stats_files,
    read_stats_excel,
    write_stats_excel,
)
from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.site_info_generator")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    log.warning("openai 未安装，AI 生成网站信息功能不可用")

# 网站信息输出表格（INFO_FILE_NAME = 网站信息.xlsx，从 category_stats 引入）
# 的数据列：批量任务把所有网站汇总成一张表，每个网站一行
INFO_COLUMNS = ("网站（文件夹）", "主类目", "域名", "标题", "描述", "主题", "地址",
                "关键词", "产品数", "分类数", "模型", "生成时间", "备注")

# 输出 JSON 较短，4000 token 足够；重试时附加格式警告
_LLM_MAX_TOKENS = 4000
_LLM_TIMEOUT = 300
_LLM_RETRIES = 3
RETRY_HINT = "\n\n⚠️ 上次返回格式有误，请务必返回纯JSON，无markdown围栏，无额外文字。"

# 提示词中携带的分类数上限（防止 token 超限）：
# 主类目相关分类（网站主打）最多展示 120 个；补充分类（数据分配带来的
# 其他类目，仅作商品广度背景）最多展示 50 个
MAX_CORE_CATEGORIES = 120
MAX_EXTRA_CATEGORIES = 50

# 垃圾/无意义分类名（纯数字、占位类目），不进入提示词
_JUNK_CATEGORIES = {"new products", "other", "others", "uncategorized", "misc"}


def _norm(s) -> str:
    """分类名规范化：去首尾空白、压缩连续空格、转小写"""
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()


def _clean_main_category(folder_name: str) -> str:
    """数据文件夹名 -> 干净的主类目名

    数据分配以类目名建文件夹（空格转下划线），展示给模型前还原：
    "2-piece_Toilets_-_Toilet_Tanks" -> "2-piece Toilets - Toilet Tanks"
    """
    # 下划线是唯一分隔符："_-_" 还原后天然成 " - "（词内连字符如
    # "2-piece" 不受影响），无需再处理破折号
    name = re.sub(r"_+", " ", str(folder_name or ""))
    name = re.sub(r"\s+", " ", name).strip(" -")
    return name


def _is_junk_category(name: str) -> bool:
    """垃圾分类：纯数字（如 "25"）或占位类目（如 "New Products"）"""
    n = _norm(name)
    if not n or n.isdigit():
        return True
    return n in _JUNK_CATEGORIES


def _category_tokens(name: str) -> set:
    """类目名 -> 显著词元集合（小写、单复数归一、忽略短词/数字）

    "Toilets ||| Toilet Tank Lids" -> {"toilet", "tank", "lid"}
    """
    words = re.findall(r"[a-z]{4,}", _norm(name))
    return {w[:-1] if w.endswith("s") else w for w in words}


def _is_core_category(cat_obj: dict, main_tokens: set) -> bool:
    """分类是否属于主类目（网站主打方向）

    按显著词元重叠判断：与主类目共享 >= 2 个词元（主类目词元不足 2 个时
    降为 >= 1）即为主打方向：
    - 主类目 "2-piece Toilets - Toilet Tanks"（{piece, toilet, tank}）匹配
      "2-piece Toilets - Toilet Bowls"（共享 piece/toilet）；
    - 不匹配 "Hardware ||| Plumbing"（无共享词元）。
    """
    if not main_tokens:
        return False
    tokens = _category_tokens(str(cat_obj.get("category") or ""))
    if not tokens:
        return False
    required = min(2, len(main_tokens))
    return len(tokens & main_tokens) >= required


def _write_info_excel(out_path: Path, rows: list[dict]) -> Path:
    """把网站信息行列表写入 网站信息.xlsx（单 Sheet「网站信息」，每行一个网站）

    rows 中的行按 INFO_COLUMNS 补齐缺失列（如失败行只有网站名和备注）。
    """
    import pandas as pd

    out_path = Path(out_path)
    df = pd.DataFrame([{c: row.get(c, "") for c in INFO_COLUMNS} for row in rows],
                      columns=list(INFO_COLUMNS))
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="网站信息", index=False)
        ws = writer.book["网站信息"]
        for i, col in enumerate(INFO_COLUMNS, start=1):
            width = max(len(col) * 2 + 2, 14)
            if col in ("标题", "描述", "关键词", "地址", "主类目"):
                width = 40
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    return out_path


def read_site_info_excel(path) -> list[dict]:
    """读取 网站信息.xlsx，还原网站信息行（每行一个网站）

    供测试与后续建站流程使用；返回 [{列名: 值, ...}, ...]。
    """
    import pandas as pd

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"网站信息表不存在: {path.name}")

    df = pd.read_excel(path, sheet_name="网站信息", engine="openpyxl")
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in INFO_COLUMNS:
            val = row.get(col)
            item[col] = "" if val is None or str(val).lower() == "nan" else val
        if item.get("网站（文件夹）"):
            rows.append(item)
    return rows


def build_site_info_prompt(stats: dict, folder_name: str) -> str:
    """根据分类统计构建网站信息生成提示词

    结构（主类目优先，避免被补充数据的大量其他分类淹没）：
    - STORE SPECIALTY：主类目名（清洗后的）+ 主类目相关分类（网站主打）
    - SUPPLEMENTARY mix：其余分类按产品数取前 N（仅作商品广度背景，
      明确指示不得主导品牌信息）
    """
    categories = stats.get("categories") or []
    summary = stats.get("summary") or {}
    total_products = summary.get("产品总数")
    if total_products in (None, "", "nan"):
        total_products = sum(c.get("count", 0) for c in categories)

    main_cat = _clean_main_category(folder_name)

    # 去垃圾 -> 主类目相关（core）与补充（extra）分流
    clean_cats = [c for c in categories
                  if not _is_junk_category(str(c.get("category") or ""))]
    main_tokens = _category_tokens(main_cat)
    core, extra = [], []
    for c in clean_cats:
        (core if _is_core_category(c, main_tokens) else extra).append(c)
    core.sort(key=lambda c: -int(c.get("count", 0)))
    extra.sort(key=lambda c: -int(c.get("count", 0)))
    if not core:
        # 主类目名与分类无交集（如中文/别名）时退化为全量列表
        core, extra = clean_cats, []

    core_products = sum(int(c.get("count", 0)) for c in core)
    extra_products = sum(int(c.get("count", 0)) for c in extra)

    def _lines(cats, limit):
        lines = [f"{i}. [{c.get('count', 0)}] {c.get('category', '')}"
                 for i, c in enumerate(cats[:limit], start=1)]
        if len(cats) > limit:
            lines.append(f"(showing top {limit} of {len(cats)} categories)")
        return "\n".join(lines) or "(none)"

    return f"""You are a senior e-commerce branding expert. Create the brand identity of a niche English e-commerce website targeting customers in the United States.

STORE SPECIALTY (main category): {main_cat or "(unknown)"}
Catalog: {total_products} products across {len(clean_cats)} categories
({core_products} in the specialty below, {extra_products} supplementary items in other categories).

SPECIALTY categories ('A|||B' means B is a subcategory of A; the store is built around these — highest priority):
{_lines(core, MAX_CORE_CATEGORIES)}

SUPPLEMENTARY product mix (also carried, for breadth only — do NOT let these dominate the branding):
{_lines(extra, MAX_EXTRA_CATEGORIES)}

Generate (all in English, for US customers):
1. "domain": a brandable .com domain for this store, derived from the STORE SPECIALTY. Lowercase, short and memorable, no www, no scheme (e.g. "toilettanklidpro.com"). Prefer no hyphens.
2. "theme": concise English site theme matching the specialty, e.g. "Toilet Tank Lids & Repair Parts".
3. "title": natural SEO title, 40-60 characters, built around the specialty keyword. Avoid the lazy pattern "<keyword> Store".
4. "description": homepage meta description, 100-160 characters. Lead with the specialty, may add one clause about the wide selection.
5. "address": a plausible business address in the USA, format "Street, City, STATE ZIP".
6. "keywords": 10-15 lowercase English SEO keywords about the SPECIALTY, most important first.

Return ONLY valid JSON (no markdown, no code fences):
{{"domain": "...", "theme": "...", "title": "...", "description": "...", "address": "...", "keywords": ["..."]}}"""


def parse_site_info(content: str) -> dict:
    """解析并校验 LLM 返回的网站信息 JSON"""
    text = str(content or "").strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回的不是 JSON 对象")

    keywords = data.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,;\n]+", keywords) if k.strip()]
    if not isinstance(keywords, list):
        keywords = []

    domain = str(data.get("domain") or "").strip().lower()
    # 容错：模型可能带 www. 或 https:// 前缀
    domain = re.sub(r'^(https?://)?(www\.)?', '', domain).strip().rstrip("/.")

    info = {
        "domain": domain,
        "theme": str(data.get("theme") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "address": str(data.get("address") or "").strip(),
        "keywords": [str(k).strip() for k in keywords if str(k).strip()],
    }
    if not info["domain"]:
        raise ValueError("返回结果缺少网站域名 (domain)")
    if not info["title"]:
        raise ValueError("返回结果缺少网站标题 (title)")
    if not info["description"]:
        raise ValueError("返回结果缺少网站描述 (description)")
    return info


def _call_site_info_llm(config: dict, api_key: str, prompt: str, log_fn=None) -> dict:
    """调用 LLM 生成网站信息：3 次重试 + 格式警告 + max_tokens 降级

    部分 OpenAI 兼容网关不支持 max_completion_tokens 参数，
    报错时自动降级为 max_tokens 重试。
    """
    if not HAS_OPENAI:
        raise RuntimeError("openai 未安装，无法调用 LLM")

    last_err = ""
    for attempt in range(_LLM_RETRIES):
        try:
            # default_headers：AgentRouter 等平台按 UA 白名单拦截 SDK 默认 UA
            client = OpenAI(base_url=config["base_url"], api_key=api_key,
                            max_retries=0,
                            default_headers=get_llm_default_headers(config))
            messages = [
                {"role": "system", "content": get_llm_system_message(config)},
                {"role": "user", "content": prompt + (RETRY_HINT if attempt else "")},
            ]
            try:
                completion = chat_completion_with_fallback(
                    client, config=config, messages=messages,
                    temperature=0.4, max_completion_tokens=_LLM_MAX_TOKENS,
                    top_p=0.95, timeout=_LLM_TIMEOUT)
            except Exception as e:
                err = str(e).lower()
                if "max_completion_tokens" in err:
                    # 网关不支持 max_completion_tokens，降级为 max_tokens
                    completion = client.chat.completions.create(
                        model=config["model_id"], messages=messages,
                        temperature=0.4, max_tokens=_LLM_MAX_TOKENS,
                        top_p=0.95, timeout=_LLM_TIMEOUT)
                else:
                    raise

            content = extract_llm_text(completion.choices[0].message)
            if not content:
                raise ValueError("LLM 返回空内容（思考 token 耗尽或模型无输出）")
            return parse_site_info(content)

        except Exception as e:
            last_err = str(e)
            if log_fn:
                log_fn(f"AI 生成网站信息第 {attempt + 1}/{_LLM_RETRIES} 次失败: {e}",
                       "warning")
            if attempt < _LLM_RETRIES - 1:
                time.sleep(2 if "429" not in last_err else 5)

    raise RuntimeError(f"AI 生成网站信息失败（{_LLM_RETRIES} 次重试）: {last_err}")


def _process_site_folder(task_id: str, folder: Path, config: dict, api_key: str,
                         log_fn) -> dict:
    """处理单个网站文件夹：读/生成分类统计 -> 调用 LLM -> 返回表格行

    每次只处理一个网站的分类结构（批量任务按顺序逐个调用本函数），
    返回 INFO_COLUMNS 结构的一行，由调用方汇总写入 网站信息.xlsx。
    抛出异常表示该网站生成失败（由调用方决定是否继续下一个）。
    """
    # ── 读取分类统计（不存在时自动生成） ──
    stats_path = folder / STATS_FILE_NAME
    if not stats_path.is_file():
        log_fn(f"[{folder.name}] 未找到 {STATS_FILE_NAME}，自动扫描文件夹生成分类统计",
               "warning")
        files = collect_stats_files(folder)
        if not files:
            raise ValueError("文件夹中没有可统计的 .xlsx 数据表格")
        agg = aggregate_folder_categories(
            files, log_fn=log_fn,
            stop_check=lambda: task_manager.is_stopped(task_id))
        if not agg["files"]:
            raise ValueError("文件夹中的表格均无法统计（缺少分类列或读取失败）")
        write_stats_excel(stats_path, agg, folder_label=folder.name)
        log_fn(f"[{folder.name}] 分类统计已生成: {stats_path.name}")
    else:
        log_fn(f"[{folder.name}] 读取分类统计: {stats_path.name}")

    stats = read_stats_excel(stats_path)
    categories = stats.get("categories") or []
    if not categories:
        raise ValueError(f"{STATS_FILE_NAME} 中没有分类数据")

    # ── 调用 LLM（只带这一个网站的分类结构） ──
    prompt = build_site_info_prompt(stats, folder.name)
    info = _call_site_info_llm(config, api_key, prompt, log_fn=log_fn)

    if not info["address"]:
        log_fn(f"[{folder.name}] 返回结果缺少地址 (address)，已留空", "warning")
    if not info["keywords"]:
        log_fn(f"[{folder.name}] 返回结果缺少关键词 (keywords)，已留空", "warning")

    # ── 保存 网站信息.json ──
    summary = stats.get("summary") or {}
    total_products = summary.get("产品总数")
    if total_products in (None, "", "nan"):
        total_products = sum(c.get("count", 0) for c in categories)

    # 汇总为表格行（批量任务逐行累积写入 网站信息.xlsx）
    row = {
        "网站（文件夹）": folder.name,
        "主类目": _clean_main_category(folder.name),
        "域名": info["domain"],
        "标题": info["title"],
        "描述": info["description"],
        "主题": info["theme"],
        "地址": info["address"],
        "关键词": ", ".join(info["keywords"]),
        "产品数": total_products,
        "分类数": len(categories),
        "模型": config["model_id"],
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "备注": "",
    }

    log_fn(f"[{folder.name}] 网站域名: {info['domain']}")
    log_fn(f"[{folder.name}] 网站主题: {info['theme'] or '-'}")
    log_fn(f"[{folder.name}] 网站标题: {info['title']}")
    log_fn(f"[{folder.name}] 网站描述: {info['description']}")
    log_fn(f"[{folder.name}] 地址: {info['address'] or '-'}")
    log_fn(f"[{folder.name}] 关键词: {', '.join(info['keywords']) or '-'}")
    return row


def run_site_info_task(task_id: str, folder, model_value: str = "",
                       model_id_override: str = "", site_db=None):
    """AI 生成网站信息后台任务体（单文件夹）：读分类统计 -> 调用 LLM -> 保存 网站信息.xlsx

    Args:
        task_id: 任务 ID
        folder: 主数据文件夹路径
        model_value: 模型唯一标识（空字符串时用 site_db 设置或 AgentRouter 默认模型）
        model_id_override: 模型 ID 覆盖（非空时替换所选模型的 model_id，
                           用于调用 AgentRouter 上任意未注册的模型）
        site_db: 可选 SiteDB 实例（缺省时自建，用于读取模型与 API Key 设置）
    """
    folder = Path(folder)
    own_db = site_db is None
    if own_db:
        from qmds.db.site_db import SiteDBClient
        site_db = SiteDBClient()

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        task_manager.update(task_id, status="running",
                            message=f"开始 AI 生成网站信息: {folder.name}")
        _log(f"任务启动: AI 生成网站信息 -> {folder}")

        # ── 解析模型与 API Key ──
        # model_id_override 优先（来自平台 /models 实时列表的选择），
        # 为空时回退配置页保存的默认模型
        if not model_value:
            model_value = (site_db.get_setting("llm_model", "") or
                           DEFAULT_AGENTROUTER_MODEL)
        config = get_llm_model_config(model_value, site_db,
                                      model_id_override=model_id_override)
        if model_id_override:
            _log(f"模型 ID（平台实时列表选择）: {config['model_id']}")
        api_key = get_llm_api_key(config, site_db)
        _log(f"使用模型: {config['label']}（{config['model_id']} @ {config['base_url']}）")

        if task_manager.is_stopped(task_id):
            task_manager.update(task_id, status="stopped", message="任务已停止")
            return

        # ── 单网站核心流程：生成一行 -> 写入该文件夹下的 网站信息.xlsx ──
        task_manager.update(task_id, progress=30,
                            message=f"正在调用 LLM 生成网站信息（{config['model_id']}）...")
        row = _process_site_folder(task_id, folder, config, api_key, _log)
        out_path = _write_info_excel(folder / INFO_FILE_NAME, [row])
        _log(f"[{folder.name}] 已保存: {out_path}")

        task_manager.update(
            task_id, status="completed", progress=100,
            message=f"完成: 网站信息已生成（{row['标题']}）-> {INFO_FILE_NAME}")

    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="任务已停止")
    except Exception as e:
        log.error(f"AI 生成网站信息任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")
    finally:
        if own_db:
            try:
                site_db.close()
            except Exception:
                pass


def _has_site_xlsx(folder: Path) -> bool:
    """文件夹内（不含子文件夹）是否有 .xlsx 文件（数据表或统计表均可）

    统计表 分类统计.xlsx 也算：数据上传后源表格可能被清理，
    只剩统计表的文件夹依然是网站数据文件夹。
    """
    try:
        for p in folder.iterdir():
            if (p.is_file() and p.suffix.lower() == ".xlsx"
                    and not p.name.startswith("~$")):
                return True
    except OSError:
        return False
    return False


def collect_site_folders(root) -> list[Path]:
    """收集 root 下所有「最后一层文件夹」（叶子目录）作为网站数据文件夹

    规则（与数据分配输出结构对应：分配文件夹/主分类文件夹/*.xlsx）：
    - 递归查找没有子文件夹的目录（最后一层），且其中含 .xlsx 文件
      （数据表格或分类统计.xlsx；数据分配后每个主分类/网站一个这样的文件夹）；
    - 名称以 extra 开头的文件夹是未绑定主分类的额外补充（不属于网站），
      自动跳过；
    - root 本身就是数据文件夹（无子文件夹且有表格）时，直接作为唯一网站。
    """
    root = Path(root)
    if not root.is_dir():
        return []

    subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    if not subdirs:
        # root 即最后一层：有 .xlsx（数据表或统计表）才算网站文件夹
        return [root] if _has_site_xlsx(root) else []

    sites: list[Path] = []
    for d in subdirs:
        sites.extend(collect_site_folders(d))
    # extra 前缀 = 数据分配的额外补充文件夹，不属于网站
    return [s for s in sites if not s.name.lower().startswith("extra")]


def run_batch_site_info_task(task_id: str, folder, model_value: str = "",
                             model_id_override: str = "", site_db=None):
    """批量 AI 生成网站信息后台任务体

    遍历所选文件夹下所有「最后一层文件夹」（每个 = 一个网站的数据），
    按顺序逐个生成网站信息（每次只把当前一个网站的分类结构交给模型，
    不会一次性把所有网站的分类结构发给模型）：
    读取/生成该网站的分类统计 -> LLM 生成域名/标题/描述等 -> 汇总为一行，
    全部结果写入所选文件夹下的 网站信息.xlsx（每个网站一行）。
    每完成一个网站就落盘一次（中断也能保留已生成的行）；
    失败的网站在表中以「备注」列记录原因，不影响其余网站。

    Args:
        task_id: 任务 ID
        folder: 父文件夹路径（如数据分配输出的分配文件夹）
        model_value / model_id_override / site_db: 同 run_site_info_task
    """
    folder = Path(folder)
    own_db = site_db is None
    if own_db:
        from qmds.db.site_db import SiteDBClient
        site_db = SiteDBClient()

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        task_manager.update(task_id, status="running",
                            message=f"开始批量 AI 生成网站信息: {folder.name}")
        _log(f"任务启动: 批量 AI 生成网站信息 -> {folder}")

        # ── 收集网站数据文件夹（最后一层文件夹） ──
        sites = collect_site_folders(folder)
        if not sites:
            raise ValueError(
                "所选文件夹下没有找到网站数据文件夹（最后一层文件夹需包含 .xlsx 数据表格；"
                "extra 前缀文件夹为额外补充，已自动跳过）")
        _log(f"共发现 {len(sites)} 个网站数据文件夹: "
             + ", ".join(s.name for s in sites))

        # ── 解析模型与 API Key（所有网站共用同一模型） ──
        if not model_value:
            model_value = (site_db.get_setting("llm_model", "") or
                           DEFAULT_AGENTROUTER_MODEL)
        config = get_llm_model_config(model_value, site_db,
                                      model_id_override=model_id_override)
        if model_id_override:
            _log(f"模型 ID（平台实时列表选择）: {config['model_id']}")
        api_key = get_llm_api_key(config, site_db)
        _log(f"使用模型: {config['label']}（{config['model_id']} @ {config['base_url']}）")

        # ── 按顺序逐个网站生成，逐行累积写入表格 ──
        out_path = folder / INFO_FILE_NAME
        rows: list[dict] = []
        succeeded: list[Path] = []
        failed: list[tuple[str, str]] = []
        for i, site in enumerate(sites):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            task_manager.update(
                task_id, progress=int(i / len(sites) * 100),
                message=f"[{i + 1}/{len(sites)}] 正在生成: {site.name}")
            try:
                _log(f"[{i + 1}/{len(sites)}] 开始生成网站信息: {site.name}")
                row = _process_site_folder(task_id, site, config, api_key, _log)
                rows.append(row)
                succeeded.append(site)
                _log(f"[{i + 1}/{len(sites)}] ✓ {site.name} 完成: "
                     f"{row['域名']} | {row['标题']}")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            except Exception as e:
                failed.append((site.name, str(e)))
                log.error(f"网站 {site.name} 信息生成失败: {e}")
                _log(f"[{i + 1}/{len(sites)}] ✗ {site.name} 生成失败: {e}（继续下一个）",
                     "error")
                # 失败也占一行（备注列记录原因），表格里一目了然
                rows.append({"网站（文件夹）": site.name, "备注": f"生成失败: {e}"})
            # 每完成一个网站就落盘，中断/失败也能保留已生成的行
            _write_info_excel(out_path, rows)

        # ── 汇总 ──
        parts = [f"完成: 批量生成 {len(succeeded)}/{len(sites)} 个网站信息"]
        if failed:
            parts.append(f"（失败 {len(failed)}: "
                         + ", ".join(n for n, _ in failed) + "）")
        summary = "".join(parts) + f" -> {out_path}"
        if not succeeded and failed:
            task_manager.update(task_id, status="failed", message=summary, progress=100)
            _log(f"任务失败: {summary}", "error")
        else:
            task_manager.update(task_id, status="completed", message=summary,
                                progress=100)
            _log(summary)

    except Exception as e:
        log.error(f"批量 AI 生成网站信息任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")
    finally:
        if own_db:
            try:
                site_db.close()
            except Exception:
                pass
