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
   b. 以该网站的分类结构（分类 + 产品数）+ 主类目为上下文构造提示词，
      并注入该网站专属的「创意方向」（品牌声线/命名风格/文案角度/城市，
      各网站互不相同）与反指纹规则（禁用套路化域名后缀/标题句式/描述
      开头/陈词滥调），避免整批站点呈现同一套措辞（被识别为批量建站）；
   c. 调用 LLM（默认 AgentRouter https://agentrouter.org/ 平台的模型，
      温度随创意方向在 0.7-0.95 间抖动）
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

import hashlib
import json
import os
import random
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
from qmds.utils import winpath
from qmds.utils.logger import get_logger

log = get_logger("web.site_info_generator")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    log.warning("openai 未安装，AI 生成网站信息功能不可用")

# 网站信息输出表格（INFO_FILE_NAME = 网站信息.xlsx，从 category_stats 引入）
# 的数据列：批量任务把所有网站汇总成一张表，每个网站一行。
# 网站大类：该网站数据表「自定义分类」列的唯一值（数据的自定义分类就是
# 网站的大类，即 ERP 站群分类树的中文名，如 动物/五金；一个网站只能有一个），
# 见 resolve_site_major_category。
# 主数据ID/补充数据ID：站群上传任务（site_uploader）把服务器返回的数据 ID
# 按网站回写（各逗号分隔，主数据在前），供后续站群管理追溯数据分卷。
INFO_COLUMNS = ("网站（文件夹）", "主类目", "网站大类", "域名", "标题", "描述", "主题",
                "地址", "关键词", "产品数", "分类数", "模型", "生成时间",
                "主数据ID", "补充数据ID", "备注")

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

# ── 反模板化（避免批量生成被识别为站群）──────────────────────
# 每个网站从下列池子随机抽取一组「创意方向」（品牌声线/命名风格/文案角度/
# 所在城市等），写进提示词强制差异化。种子 = 网站名 + 任务 nonce 的稳定哈希：
# 同一网站同一任务内方向固定（重试幂等），不同网站/不同批次方向互不相同。

_BRAND_VOICES = (
    "family-run shop, second generation, plain-spoken",
    "trade-grade supplier that contractors and repair pros order from",
    "boutique curator with an editorial eye",
    "workshop-direct maker brand",
    "hobbyist-founded specialist retailer",
    "no-nonsense replacement-parts dealer",
    "independent dealer carrying a unusually deep catalog",
    "budget-minded seller focused on bulk buys",
)

_TONES = (
    "warm and conversational",
    "crisp and technical",
    "premium and understated",
    "practical and direct",
    "lightly playful",
    "authoritative and detail-oriented",
)

_DOMAIN_STYLES = (
    "a coined, made-up word that evokes the niche (like real DTC brands use)",
    "a two-word compound of two real English words from the niche",
    "a short evocative real word paired with one niche word",
    "a niche word plus an unexpected but fitting modifier (material, craft, era)",
    "a compact two-to-three-word phrase that reads like an independent shop's name",
)

_TITLE_STYLES = (
    "brand word first, then a plain descriptor after a colon",
    "main keyword first, then a specific differentiator after a dash",
    "a natural sentence-style title with no separator punctuation",
    "short and plain - just what the store offers, no slogan",
    "benefit- or use-case-led, still containing the main keyword",
    "a craft or material angle that names what the products are for",
)

_TITLE_LENGTHS = ("35-50", "40-60", "45-65")

_DESC_ANGLES = (
    "open with the concrete product range, then one sentence on service",
    "open with the customer's problem or project, then how the store solves it",
    "open with what the selection is unusually deep in, then who it serves",
    "open with one specific product example, then the broader range",
    "open with the audience, then what has been picked for them",
)

_DESC_LENGTHS = ("80-120", "100-150", "120-170", "140-200", "90-140")

_KEYWORD_RECIPES = (
    "8-11 keywords, leaning toward long-tail multi-word phrases",
    "10-14 keywords, head terms first then long-tail",
    "6-9 keywords, only the highest-intent phrases",
    "12-16 keywords, covering the main subcategories",
)

# 城市池：刻意避开模型最爱扎堆的 Austin/Denver/Portland/Miami 等热门城市，
# 分散在各州中等城市，让整批站点的地址不呈现同一地理聚集
_ADDRESS_CITIES = (
    ("Huntsville", "AL"), ("Anchorage", "AK"), ("Mesa", "AZ"),
    ("Fayetteville", "AR"), ("Stockton", "CA"), ("Fort Collins", "CO"),
    ("Hartford", "CT"), ("Wilmington", "DE"), ("Ocala", "FL"),
    ("Marietta", "GA"), ("Coeur d'Alene", "ID"), ("Schaumburg", "IL"),
    ("Carmel", "IN"), ("Cedar Rapids", "IA"), ("Overland Park", "KS"),
    ("Bowling Green", "KY"), ("Lafayette", "LA"), ("Grand Rapids", "MI"),
    ("Rochester", "MN"), ("Springfield", "MO"), ("Biloxi", "MS"),
    ("Billings", "MT"), ("Lincoln", "NE"), ("Sparks", "NV"),
    ("Manchester", "NH"), ("Cherry Hill", "NJ"), ("Rio Rancho", "NM"),
    ("Schenectady", "NY"), ("High Point", "NC"), ("Fargo", "ND"),
    ("Dayton", "OH"), ("Norman", "OK"), ("Salem", "OR"),
    ("Reading", "PA"), ("Warwick", "RI"), ("Greenville", "SC"),
    ("Sioux Falls", "SD"), ("Chattanooga", "TN"), ("Tyler", "TX"),
    ("Ogden", "UT"), ("Burlington", "VT"), ("Roanoke", "VA"),
    ("Bellingham", "WA"), ("Morgantown", "WV"), ("Appleton", "WI"),
    ("Cheyenne", "WY"),
)

_STREET_HINTS = (
    "a simple street number and name",
    "include a Suite or Unit number",
    "a small commercial-road address (number plus road name)",
)


def _site_creative_direction(key: str) -> dict:
    """按稳定哈希种子为单个网站抽取创意方向（品牌声线/命名/文案/地理）

    种子取 key 的 SHA-256（Python 内建 hash 受 PYTHONHASHSEED 影响不稳定，
    不能用于跨进程可复现的方向）。同一 key 结果固定；key 中含任务 nonce
    （task_id 含时间戳）时，重跑批次会得到不同的方向组合。

    Returns:
        {"voice", "tone", "domain_style", "title_style", "title_len",
         "desc_angle", "desc_len", "keyword_recipe", "city", "state",
         "street_hint", "temperature"} — 全部为提示词文本与采样参数
    """
    seed = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    city, state = rng.choice(_ADDRESS_CITIES)
    return {
        "voice": rng.choice(_BRAND_VOICES),
        "tone": rng.choice(_TONES),
        "domain_style": rng.choice(_DOMAIN_STYLES),
        "title_style": rng.choice(_TITLE_STYLES),
        "title_len": rng.choice(_TITLE_LENGTHS),
        "desc_angle": rng.choice(_DESC_ANGLES),
        "desc_len": rng.choice(_DESC_LENGTHS),
        "keyword_recipe": rng.choice(_KEYWORD_RECIPES),
        "city": city,
        "state": state,
        "street_hint": rng.choice(_STREET_HINTS),
        # 温度在 0.7-0.95 间抖动：进一步拉开同批站点输出分布
        "temperature": round(rng.uniform(0.7, 0.95), 2),
    }


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


def resolve_main_category(site_folder, categories) -> str:
    """网站文件夹 -> 原始主类目名（与导出表格「分类」列完全一致，含 ||| 层级分隔符）

    数据分配用 sanitize_filename(分类) 命名文件夹与主数据表（||| 与 Windows
    非法字符统一转下划线——文件名不能含 |），网站信息的「主类目」需还原为
    表格中的原始分类值。匹配优先级（分类按统计顺序，即产品数降序，首个命中）：
    1) sanitize(分类) == 文件夹名（数据分配的原始命名）；
    2) sanitize(分类) == 文件夹名去掉防重名后缀 _{N}（两个分类 sanitize 同名）；
    3) sanitize(分类) == 主数据表名 main 前缀后的部分（审核应用后文件夹已
       改名为域名，表名仍保留分类名；兼容 data_ 前缀与 _part{N} 分卷）。

    Args:
        site_folder: 网站数据文件夹（str/Path；不存在时跳过表名匹配）
        categories: 分类统计的分类列表（[{"category", "count", ...}] 或 [str]）

    Returns:
        原始分类值（含 |||）；未命中返回 ""（由调用方决定回退值）
    """
    from qmds.modules.web.services.data_allocator import sanitize_filename

    folder = Path(site_folder) if site_folder else None
    name = folder.name if folder is not None else ""
    if not name:
        return ""

    cats = []
    for c in categories or []:
        raw = c.get("category") if isinstance(c, dict) else c
        cat = str(raw or "").strip()
        if cat:
            cats.append(cat)
    if not cats:
        return ""

    # 数据分配防重名会追加 _{N} 后缀（两个分类 sanitize 后同名时）
    base = re.sub(r"_\d+$", "", name)

    # 主数据表名集合（main{sanitize(分类)}，可能带 data_ 前缀 / _part 分卷）
    main_names: set = set()
    if folder is not None and winpath.is_dir(folder):
        try:
            with os.scandir(winpath.long_path(folder)) as it:
                for e in it:
                    n = e.name
                    if not e.is_file() or not n.lower().endswith(".xlsx"):
                        continue
                    if n.startswith("~$") or n in (STATS_FILE_NAME, INFO_FILE_NAME):
                        continue
                    if n.startswith("data_"):
                        n = n[len("data_"):]
                    if n.lower().startswith("main"):
                        # 去掉 main 前缀与 .xlsx 后缀 -> sanitize(分类) 候选
                        main_names.add(n[len("main"):-len(".xlsx")])
        except OSError:
            pass

    # 匹配分三轮：先精确文件夹名（含分类本身带数字尾的，如 "Widgets 2"），
    # 再去后缀基名（防重名 _{N}），最后主数据表名——避免同批分类中
    # "Widgets"（基名）抢先于 "Widgets 2"（精确名）误匹配
    for target in (name, base):
        if not target:
            continue
        for cat in cats:
            key = sanitize_filename(cat)
            if key and key != "Unnamed" and key == target:
                return cat
    for cat in cats:
        key = sanitize_filename(cat)
        if not key or key == "Unnamed":
            continue
        if any(n == key or n.startswith(f"{key}_part") for n in main_names):
            return cat
    return ""


def resolve_site_major_category(site_folder, log_fn=None,
                                stop_check=None) -> tuple:
    """聚合网站数据表「自定义分类」列，得到网站大类

    数据的自定义分类就是网站的大类（ERP 站群分类树的中文名，如 动物/五金）。
    流式扫描该网站文件夹下所有数据表格（collect_stats_files 自动跳过
    分类统计.xlsx / 网站信息.xlsx / ~$ 锁文件，兼容 data_ 前缀），按行数
    聚合各自定义分类值；只读表头与该列，不加载整表。

    Args:
        site_folder: 网站数据文件夹（str/Path）
        log_fn: 可选日志函数（单表读取失败时告警）
        stop_check: 可选停止检查（返回 True 时抛 InterruptedError）

    Returns:
        (网站大类, 计数 dict)。网站大类为唯一值：正常情况一个网站的
        数据全部来自同一份导出（自定义分类 全表一致），直接取该值；
        出现多个值（历史英文透传残留等）时取行数最多者（行数相同取
        字典序首位），调用方应通过日志告警。计数前各值先经
        get_cn_category_name 映射（'animals pet supplies' 与 '动物'
        合并计为 动物），避免同一大类的中英形态分裂成多值。
        没有任何非空值时返回 ("", {})。
    """
    from openpyxl import load_workbook

    from qmds.config.categories import get_cn_category_name

    folder = Path(site_folder) if site_folder else None
    if folder is None:
        return "", {}
    counts: dict = {}
    for path in collect_stats_files(folder):
        if stop_check is not None and stop_check():
            raise InterruptedError()
        try:
            wb = load_workbook(winpath.long_path(path),
                               read_only=True, data_only=True)
        except Exception as e:
            msg = f"[{folder.name}] 读取数据表失败，跳过（{path.name}）: {e}"
            if log_fn:
                log_fn(msg, "warning")
            else:
                log.warning(msg)
            continue
        try:
            ws = wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            header = list(header) if header else []
            if "自定义分类" not in header:
                continue  # 表中没有 自定义分类 列（旧格式数据），跳过
            idx = header.index("自定义分类")
            for row in rows:
                if row is None:
                    continue
                val = row[idx] if idx < len(row) else None
                sval = str(val).strip() if val is not None else ""
                if sval:
                    # 英文残留（旧版透传的 'animals pet supplies' 等）先映射
                    # 为中文大类再计数，与中文值合并，避免分裂成多值
                    key = get_cn_category_name(sval)
                    counts[key] = counts.get(key, 0) + 1
        finally:
            wb.close()
    if not counts:
        return "", {}
    # 一个网站的自定义分类只能有一个（同一份导出数据的分配，全表一致）；
    # 出现多个值（历史残留）时取行数最多者，行数相同取字典序首位
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[0][0], counts


def repair_row_major_category(site_folder, row) -> bool:
    """为网站信息行写入/刷新「网站大类」（聚合数据表 自定义分类 列）

    旧版 网站信息.xlsx 没有网站大类列（读取时该列为空），从该网站的数据
    表格聚合自定义分类值补写（数据的自定义分类就是网站的大类）。行已带
    值但与当前聚合结果不同（如早期回填的混合值）时刷新为唯一值——该列
    是派生数据，始终以数据表实际内容为准。
    返回是否修改（无数据表/聚合为空/已一致时不修改）。
    """
    value, _counts = resolve_site_major_category(site_folder)
    if not value:
        return False
    if str(row.get("网站大类") or "").strip() == value:
        return False
    row["网站大类"] = value
    return True


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
    # 网站文件夹路径可能超过 Windows 260 字符，用扩展长度前缀写出
    with pd.ExcelWriter(winpath.long_path(out_path), engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="网站信息", index=False)
        ws = writer.book["网站信息"]
        for i, col in enumerate(INFO_COLUMNS, start=1):
            width = max(len(col) * 2 + 2, 14)
            if col in ("标题", "描述", "关键词", "地址", "主类目"):
                width = 40
            if col in ("主数据ID", "补充数据ID"):
                # 多个 ID 逗号连接可能较长，加宽便于查看
                width = 30
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    return out_path


def _norm_id_cell(value) -> str:
    """数据ID 单元格值归一为字符串

    纯数字 ID（如 "301"）经 pandas 写 Excel 会被转为数值，读回是
    int/float（301 / 301.0）；多 ID 逗号串（"301,302"）保持字符串。
    统一还原为字符串，保证展示/回写/JSON 输出形态稳定。
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def read_site_info_excel(path) -> list[dict]:
    """读取 网站信息.xlsx，还原网站信息行（每行一个网站）

    供测试与后续建站流程使用；返回 [{列名: 值, ...}, ...]。
    主数据ID/补充数据ID 列归一为字符串（数字型 ID 不带小数点）。
    """
    import pandas as pd

    path = Path(path)
    if not winpath.is_file(path):
        raise FileNotFoundError(f"网站信息表不存在: {path.name}")

    df = pd.read_excel(winpath.long_path(path), sheet_name="网站信息", engine="openpyxl")
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in INFO_COLUMNS:
            val = row.get(col)
            if val is None or str(val).lower() == "nan":
                item[col] = ""
            elif col in ("主数据ID", "补充数据ID"):
                item[col] = _norm_id_cell(val)
            else:
                item[col] = val
        if item.get("网站（文件夹）"):
            rows.append(item)
    return rows


def build_site_info_prompt(stats: dict, folder_name: str,
                           main_category: str = "",
                           direction: dict = None) -> str:
    """根据分类统计构建网站信息生成提示词（含反模板化创意方向）

    结构（主类目优先，避免被补充数据的大量其他分类淹没）：
    - STORE SPECIALTY：主类目名 + 主类目相关分类（网站主打）
    - SUPPLEMENTARY mix：其余分类按产品数取前 N（仅作商品广度背景，
      明确指示不得主导品牌信息）
    - CREATIVE DIRECTION：该网站专属的品牌声线/命名风格/文案角度/城市，
      各网站互不相同，避免整批站点呈现同一套措辞与命名习惯（站群指纹）
    - Anti-fingerprint rules：禁用套路化域名后缀/标题句式/描述开头/陈词滥调

    main_category 优先用调用方传入的原始分类值（含 ||| 层级分隔符，
    与导出表格「分类」列完全一致）；为空时从分类统计反查还原
    （文件夹名 == sanitize(分类)），仍未命中回退清洗后的文件夹名。
    direction 为空时按文件夹名确定性抽取（供直接调用/测试复现）。
    """
    categories = stats.get("categories") or []
    summary = stats.get("summary") or {}
    total_products = summary.get("产品总数")
    if total_products in (None, "", "nan"):
        total_products = sum(c.get("count", 0) for c in categories)

    main_cat = (str(main_category or "").strip()
                or resolve_main_category(folder_name, categories)
                or _clean_main_category(folder_name))

    d = direction or _site_creative_direction(folder_name)

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

    return f"""You are a seasoned e-commerce branding consultant. Create the brand identity of ONE independent niche English e-commerce website targeting customers in the United States.

Context: this store belongs to a portfolio of separately-founded stores. Anyone comparing the portfolio will look for stores that read like mass-produced siblings — identical naming habits, phrasing and sentence shapes. Your job is to make THIS store feel like it was started by different people than the rest.

STORE SPECIALTY (main category): {main_cat or "(unknown)"}
Catalog: {total_products} products across {len(clean_cats)} categories
({core_products} in the specialty below, {extra_products} supplementary items in other categories).

SPECIALTY categories ('A|||B' means B is a subcategory of A; the store is built around these — highest priority):
{_lines(core, MAX_CORE_CATEGORIES)}

SUPPLEMENTARY product mix (also carried, for breadth only — do NOT let these dominate the branding):
{_lines(extra, MAX_EXTRA_CATEGORIES)}

CREATIVE DIRECTION assigned to this particular store (its siblings got different ones — follow yours, do not average back toward generic):
- Brand persona: {d["voice"]}
- Writing tone: {d["tone"]}
- Domain naming style: {d["domain_style"]}
- Title style: {d["title_style"]} ({d["title_len"]} characters)
- Description angle: {d["desc_angle"]} ({d["desc_len"]} characters)
- Keywords: {d["keyword_recipe"]}
- Store location: in or around {d["city"]}, {d["state"]} — a plausible US street address ({d["street_hint"]}), format "Street, City, STATE ZIP", with a ZIP that is plausible for that state.

Generate (all in English, for US customers), following the creative direction above:
1. "domain": a brandable .com domain derived from the STORE SPECIALTY, in the naming style assigned above. Lowercase, short and memorable, no www, no scheme, at most 3 words. Do NOT end it with any of these tired suffixes: "pro", "hub", "central", "mart", "store", "shop", "online", "usa", "365", "deals", "best", "top", "direct".
2. "theme": a short natural English phrase naming what the store sells, matching the specialty (a plain description, not a slogan).
3. "title": homepage title containing the main specialty keyword. Never use the patterns "<keyword> Store", "<keyword> Shop", "<keyword> Online", "<keyword> - Buy <keyword> Online".
4. "description": homepage meta description per the assigned angle and length. Do NOT open with "Shop", "Discover", "Find", "Looking for", "Welcome to" or "Explore" (the most common bulk-generated openings), and do not repeat the title verbatim inside it.
5. "address": follow the store location line above.
6. "keywords": lowercase English SEO keywords about the SPECIALTY, most important first, per the assigned keyword recipe. No duplicates, no city names.

Never use these clichés anywhere: "one-stop shop", "go-to destination", "look no further", "elevate your", "wide range of high-quality", "unbeatable prices", "shop with confidence", "your journey starts here", "curated for you".

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


def _call_site_info_llm(config: dict, api_key: str, prompt: str, log_fn=None,
                        temperature: float = 0.85) -> dict:
    """调用 LLM 生成网站信息：3 次重试 + 格式警告 + max_tokens 降级

    temperature 默认 0.85（高于常规取值）：配合每站不同的创意方向，
    降低整批站点输出趋同的概率；调用方可按方向微调（0.7-0.95）。
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
                    temperature=temperature, max_completion_tokens=_LLM_MAX_TOKENS,
                    top_p=0.95, timeout=_LLM_TIMEOUT)
            except Exception as e:
                err = str(e).lower()
                if "max_completion_tokens" in err:
                    # 网关不支持 max_completion_tokens，降级为 max_tokens
                    completion = client.chat.completions.create(
                        model=config["model_id"], messages=messages,
                        temperature=temperature, max_tokens=_LLM_MAX_TOKENS,
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
                         log_fn, variant: int = 0) -> dict:
    """处理单个网站文件夹：读/生成分类统计 -> 调用 LLM -> 返回表格行

    每次只处理一个网站的分类结构（批量任务按顺序逐个调用本函数），
    返回 INFO_COLUMNS 结构的一行，由调用方汇总写入 网站信息.xlsx。
    抛出异常表示该网站生成失败（由调用方决定是否继续下一个）。

    variant 用于域名冲突重试：变化创意方向的随机种子（换一套品牌声线/
    命名风格/城市），常规生成恒为 0，同一 (task_id, folder) 内重试幂等。
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

    # ── 主类目：还原为表格原始分类值（含 ||| 层级分隔符）──
    # 文件夹名是 sanitize 后的分类（||| 已转下划线，Windows 文件名不能
    # 含 |），从分类统计反查原始值；未命中回退清洗后的文件夹名
    main_cat = (resolve_main_category(folder, categories)
                or _clean_main_category(folder.name))

    # ── 网站大类：聚合数据表 自定义分类 列（数据的自定义分类就是网站的大类）──
    major_cat, major_counts = resolve_site_major_category(
        folder, log_fn=log_fn,
        stop_check=lambda: task_manager.is_stopped(task_id))
    if major_cat:
        if len(major_counts) > 1:
            # 一个网站的自定义分类只能有一个；多值说明数据表里有历史残留
            detail = "，".join(f"{k}×{v}" for k, v in sorted(
                major_counts.items(), key=lambda kv: (-kv[1], kv[0])))
            log_fn(f"[{folder.name}] ⚠ 数据表 自定义分类 列存在多个值"
                   f"（{detail}），网站大类取行数最多者: {major_cat}", "warning")
        else:
            log_fn(f"[{folder.name}] 网站大类: {major_cat}")
    else:
        log_fn(f"[{folder.name}] 未识别到网站大类（数据表 自定义分类 列均为空"
               "或缺失）", "warning")

    # ── 创意方向（反模板化）：task_id 含时间戳，重跑批次方向组合会变化 ──
    direction = _site_creative_direction(f"{task_id}|{folder.name}|v{variant}")

    # ── 调用 LLM（只带这一个网站的分类结构 + 专属创意方向） ──
    prompt = build_site_info_prompt(stats, folder.name, main_category=main_cat,
                                    direction=direction)
    info = _call_site_info_llm(config, api_key, prompt, log_fn=log_fn,
                               temperature=direction["temperature"])

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
        "主类目": main_cat,
        "网站大类": major_cat,
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


def _scandir_entries(folder: Path) -> list[tuple[str, bool, bool]]:
    """os.scandir 枚举目录项（超长路径安全）

    返回 [(名字, is_file, is_dir), ...]；目录或文件路径超过 Windows
    MAX_PATH(260) 时 Path.iterdir/is_file 会静默失败，scandir 配合
    扩展长度前缀可正常工作。
    """
    import os as _os

    out: list[tuple[str, bool, bool]] = []
    try:
        with _os.scandir(winpath.long_path(folder)) as it:
            for e in it:
                try:
                    out.append((e.name, e.is_file(), e.is_dir()))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _has_site_xlsx(folder: Path) -> bool:
    """文件夹内（不含子文件夹）是否有 .xlsx 文件（数据表或统计表均可）

    统计表 分类统计.xlsx 也算：数据上传后源表格可能被清理，
    只剩统计表的文件夹依然是网站数据文件夹。
    """
    for name, is_file, _is_dir in _scandir_entries(folder):
        if (is_file and name.lower().endswith(".xlsx")
                and not name.startswith("~$")):
            return True
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
    if not winpath.is_dir(root):
        return []

    # os.scandir 枚举（超长路径安全；分类文件夹路径可能超 260 字符）
    subdirs = sorted((Path(root) / n for n, _f, is_d in _scandir_entries(root)
                      if is_d), key=lambda p: str(p).lower())
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
        used_domains: set[str] = set()  # 批内域名去重（站群最明显的指纹之一）
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
                if row["域名"] in used_domains:
                    # 域名与已生成网站重复：换一套创意方向重试一次
                    _log(f"[{i + 1}/{len(sites)}] ⚠ {site.name} 域名 "
                         f"{row['域名']} 与已生成网站重复，更换创意方向重新生成",
                         "warning")
                    row = _process_site_folder(task_id, site, config, api_key,
                                               _log, variant=1)
                    if row["域名"] in used_domains:
                        raise ValueError(
                            f"域名 {row['域名']} 与其他网站重复（两次生成均冲突）")
                used_domains.add(row["域名"])
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
