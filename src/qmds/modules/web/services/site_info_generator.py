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
     - description 网站描述（SEO meta 描述，约 300 字符的 2-3 句短文：主类目词为主，
       顺带 1 个相邻品类的词以覆盖更多关键词）
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
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

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
    DOMAIN_STATS_FILE_NAME,
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

# ── 域名占用检查（西部数码 whois）──────────────────────────
# 生成的域名必须先确认没被别人注册，否则建站时域名已被占用。
# 查询 https://whois.west.cn/{domain}，按实测标记判断（各采样 4 个域名验证）：
#   已注册 → 页面有英文 whois 原文 Sponsorning Registrar / Registry Domain ID
#            （实测 4/4 命中，未注册页 0/4）
#   未注册 → 页面有「查询能否注册」提示（实测 4/4 命中，已注册页 0/4）
# 注意：不能用「该域名可能尚未注册」判断——这段文案内嵌在页面 JS 模板里
# （把 "No match for" 渲染成中文提示用），已注册页同样包含它，会全部误判成可注册。
# 所以判定顺序是先「已注册」标记，再「未注册」标记，都没有则视为无法判断。
DOMAIN_WHOIS_URL = "https://whois.west.cn/{domain}"
DOMAIN_WHOIS_TIMEOUT = 20
DOMAIN_WHOIS_TRIES = 2          # 单次查询的网络重试次数
DOMAIN_REGEN_RETRIES = 3        # 域名已被注册时重新生成的最大次数
_DOMAIN_TAKEN_MARKERS = ("Sponsoring Registrar", "Registry Domain ID")
_DOMAIN_FREE_MARKERS = ("查询能否注册",)
_DOMAIN_WHOIS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

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

# 标题风格：一律「主类目关键词打头」，差异体现在关键词之后的部分，
# 保证整批站点的标题都把主类目顶到最前面（用户要求：标题突出主类目）
_TITLE_STYLES = (
    "main specialty keyword first, then a brand word after a dash",
    "main specialty keyword first, then a specific differentiator after a colon",
    "main specialty keyword first, then a short benefit phrase with no separator punctuation",
    "main specialty keyword plus one concrete qualifier (material, use or audience) in a compact phrase",
    "main specialty keyword first, then a location or service hint after a comma",
    "main specialty keyword and one adjacent specialty term joined by 'and', then a short tail",
)

_TITLE_LENGTHS = ("35-50", "40-60", "45-65")

# 人称与句法硬规则（注入提示词，适用于所有输出字段）。
# 生成的是「网站文案」，一旦出现 We/Our/You 就变成店主自述或对读者喊话，
# 观感廉价且与站群其它站雷同；统一改成以商店/商品做主语的第三人称陈述。
# 注意 "warm and conversational" 之类的语气词最容易诱发第一人称，所以这里
# 明确「语气只影响用词与节奏，不得引入人称」。
_PERSON_RULES = """WRITING VOICE - applies to EVERY field (title, theme, description, keywords):
- Write in the THIRD PERSON only. Never use first-person pronouns (I, we, us, our, ours, my, mine) or second-person pronouns (you, your, yours, yourself), including contractions (we're, we've, we'll, you're, you'll, you'd) and possessives.
- Never address the reader. Imperative calls to action ("Order today", "Browse the range", "Shop now", "Get yours") imply "you" and are equally forbidden.
- Make a product, material, use, audience or the range the subject of every sentence.
  BAD:  "We carry yoga mats in cork and rubber. Order today and we'll ship fast."
  GOOD: "Cork, jute and natural rubber yoga mats make up the core range. Orders ship from Fargo within two days."
  BAD:  "You'll find the perfect gift for every occasion."
  GOOD: "Baptism, confirmation and housewarming gifts fill out the religious line."
- The assigned writing tone shapes word choice and rhythm only; it must never turn into a conversational address to the reader.
- Every sentence must contain a verb and stand on its own. Never write noun fragments, e.g. "Adjacent fitness mats for strength training." or "Also carry hand tools for the job." - write "Fitness mats for strength training round out the range." instead. Do not begin a sentence with "Adjacent", "Also," or "Plus,".
- Prefer concrete nouns (materials, product types, uses) over vague praise adjectives ("high-quality", "amazing", "premium").
- VARY THE SENTENCE LINKING TOO. Never fall back on stock connectives or closing phrases; these make a batch of descriptions read as one template: "round out the range", "round out the selection", "round out the lineup", "join the lineup", "complete the collection", "complement the range", "complement the selection", "complement the lineup", "in this specialty range", "make up the core range", "form the core of this", "for every maker", "catering to every need". Name the adjacent products directly and let them be the subject of their own sentence.
  BAD:  "Wrenches and pipe cutters round out the range."
  GOOD: "Wrenches and pipe cutters cover the repair side."
  BAD:  "Throw pillows and tabletop accents join the lineup."
  GOOD: "Throw pillows and tabletop accents finish the room setting."
- VARY THE OPENING. Never begin the description with "The catalog", "This catalog", "Our catalog", "The store", "The shop", "The range", "The collection", "The selection", "The inventory", "The assortment", "The lineup" or with "Discover", "Shop", "Find", "Looking for", "Welcome to", "Explore". Store-word subjects at the start of the sentence are the single clearest bulk-generation fingerprint - start with the products, the material, the use, the audience or the setting instead."""

# 描述角度：SEO meta 描述式短文（约 300 字符 = 2-3 句），推进结构统一为
# 「立主类目 -> 顺带 1 个相邻品类 -> 一句服务收尾」，差异体现在起句与落点，
# 保证短文也能覆盖主类目之外的少量品类关键词
_DESC_ANGLES = (
    "name the specialty and its flagship product types in the first sentence, add one adjacent range the store also carries, end on a practical service note",
    "lead with who the specialty is for and what it does for them, name the main product types, add one related range, close on ordering",
    "lead with the depth of the specialty catalog and its most specific product families, then one adjacent range, close on shipping or support",
    "open on one concrete specialty item as an example, widen to the range it belongs to, mention one adjacent range, close on the store's practical advantage",
    "open on the customer the specialty serves, name what the catalog covers for them, add one related range, close on service",
)

# 描述起句方式池（反模板化）：模型极容易把所有站的描述都写成
# "The catalog supplies/opens/includes/spans ..." 同一句式，因为上面 5 个角度
# 都在讲「先点出主类目」，而 WRITING VOICE 的正面示例又恰好是 "The catalog spans"。
# 这里把「第一句的语法主语」也变成随方向抽取的变量，拉开句式分布。
_DESC_OPENINGS = (
    "start with the flagship product type itself as the grammatical subject",
    "start with the material or construction that defines the specialty",
    "start with the activity, room or setting the products are used in",
    "start with the trade, hobby or audience the specialty serves",
    "start with one concrete product example, then widen to the whole range",
    "start with the range of sizes, styles or finishes on offer",
    "start with what these products are built to withstand or replace",
    "start with the everyday occasion or season the products fit",
    "start with the craft tradition or origin behind the specialty",
    "start with a specific product family and its most useful variant",
)

# 描述长度按「字符」计（用户要求约 300 字符，即 SEO meta 描述式短文 ≈ 45-55 词、
# 2-3 句）。三档贴着 300 做小幅差异（反模板化）。
# 注意：早期版本误按「词」理解成 300 词长文，生成出来是 1700-2400 字符，
# 比要求长 6 倍；这里统一按字符，并有 _cap_description_length 兜底。
_DESC_LENGTHS = ("280-320 characters", "260-300 characters", "290-330 characters")

_KEYWORD_RECIPES = (
    "8-11 keywords, leaning toward long-tail multi-word phrases",
    "10-14 keywords, head terms first then long-tail",
    "6-9 keywords, only the highest-intent phrases",
    "12-16 keywords, covering the main subcategories",
)

# 城市池：美国经济规模靠前的主要城市 + 各州经济中心（用户要求：地址落在
# 经济发展靠前的城市）。同时覆盖 50 州，避免整批站点地址挤在同几个州
# （反模板化）。旧版本刻意避开大城市，与用户要求相反，已替换。
_ADDRESS_CITIES = (
    # ── 全国经济规模靠前的主要都市 ──
    ("New York", "NY"), ("Los Angeles", "CA"), ("Chicago", "IL"),
    ("San Francisco", "CA"), ("San Jose", "CA"), ("San Diego", "CA"),
    ("Sacramento", "CA"), ("Fresno", "CA"),
    ("Dallas", "TX"), ("Houston", "TX"), ("Austin", "TX"),
    ("San Antonio", "TX"), ("Fort Worth", "TX"),
    ("Boston", "MA"), ("Philadelphia", "PA"), ("Pittsburgh", "PA"),
    ("Seattle", "WA"), ("Atlanta", "GA"), ("Miami", "FL"),
    ("Orlando", "FL"), ("Tampa", "FL"), ("Jacksonville", "FL"),
    ("Phoenix", "AZ"), ("Tucson", "AZ"),
    ("Minneapolis", "MN"), ("Detroit", "MI"), ("Denver", "CO"),
    ("Portland", "OR"), ("Charlotte", "NC"), ("Raleigh", "NC"),
    ("Nashville", "TN"), ("St. Louis", "MO"), ("Kansas City", "MO"),
    ("Baltimore", "MD"), ("Indianapolis", "IN"), ("Columbus", "OH"),
    ("Cleveland", "OH"), ("Cincinnati", "OH"), ("Salt Lake City", "UT"),
    ("Las Vegas", "NV"), ("Milwaukee", "WI"), ("Oklahoma City", "OK"),
    ("Louisville", "KY"), ("Richmond", "VA"), ("New Orleans", "LA"),
    ("Hartford", "CT"), ("Providence", "RI"), ("Birmingham", "AL"),
    ("Omaha", "NE"), ("Des Moines", "IA"), ("Boise", "ID"),
    ("Charleston", "SC"), ("Little Rock", "AR"), ("Jackson", "MS"),
    ("Albuquerque", "NM"), ("Wichita", "KS"), ("Newark", "NJ"),
    ("Honolulu", "HI"), ("Buffalo", "NY"), ("Rochester", "NY"),
    ("Grand Rapids", "MI"), ("Colorado Springs", "CO"), ("Spokane", "WA"),
    ("Reno", "NV"), ("Madison", "WI"), ("Tulsa", "OK"),
    ("Lexington", "KY"), ("Virginia Beach", "VA"), ("Columbia", "SC"),
    ("Knoxville", "TN"), ("Worcester", "MA"), ("Fort Wayne", "IN"),
    # ── 其余各州的经济中心（补齐 50 州覆盖）──
    ("Anchorage", "AK"), ("Wilmington", "DE"), ("Portland", "ME"),
    ("Billings", "MT"), ("Fargo", "ND"), ("Manchester", "NH"),
    ("Sioux Falls", "SD"), ("Burlington", "VT"), ("Charleston", "WV"),
    ("Cheyenne", "WY"),
)

# ── 真实地址库 ────────────────────────────────────────
# 用户要求：地址必须对应真实存在的房屋（能在地图上查到），不能让模型编造。
# 地址来源是 scripts/collect_us_addresses.py 从 OpenStreetMap 采集的
# 「带门牌号的住宅建筑」地址，落库到 data/us_addresses.json：
#   {"Denver|CO": [["2300 Court Pl", "Denver", "80205"], ...], ...}
# 生成时直接抽取并原样写进提示词，模型不得改写。
_US_ADDRESS_PATH = Path(__file__).resolve().parents[5] / "data" / "us_addresses.json"
# 数据太少的城市（采集不完整）先不用，避免整批站点地址来回重复
_ADDRESS_MIN_PER_CITY = 10

_address_pool_cache = None
_address_pool_mtime = None
_address_pool_lock = threading.Lock()


def _load_address_pool() -> dict:
    """加载真实地址库（按文件 mtime 感知更新）

    地址库由 scripts/collect_us_addresses.py 持续采集，城市会不断增加，所以
    不能只加载一次就永久缓存——那样后台采集的新城市要重启服务才生效。
    这里按文件 mtime 判断：没变就用缓存（只花一次 stat），变了就重新加载。

    Returns:
        {"Denver|CO": [["2300 Court Pl", "Denver", "80205"], ...], ...}
        文件缺失或损坏时返回空 dict，调用方回退到旧的地址生成方式
    """
    global _address_pool_cache, _address_pool_mtime
    with _address_pool_lock:
        try:
            mtime = _US_ADDRESS_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if _address_pool_cache is not None and mtime == _address_pool_mtime:
            return _address_pool_cache
        try:
            raw = json.loads(_US_ADDRESS_PATH.read_text(encoding="utf-8"))
            pool = {k: v for k, v in raw.items()
                    if isinstance(v, list) and len(v) >= _ADDRESS_MIN_PER_CITY}
            _address_pool_cache = pool
            _address_pool_mtime = mtime
            if pool:
                total = sum(len(v) for v in pool.values())
                log.info(f"真实地址库已加载: {len(pool)} 个城市 / {total} 条")
            else:
                log.warning(f"真实地址库为空或数据不足: {_US_ADDRESS_PATH}")
        except Exception as e:
            # 采集脚本可能正好在写文件，读到半个 JSON：保留上一次可用数据
            if _address_pool_cache:
                log.warning(f"真实地址库重载失败，继续用上一次的数据: {e}")
            else:
                log.warning(f"真实地址库加载失败（{_US_ADDRESS_PATH}）: {e}")
                _address_pool_cache = {}
            _address_pool_mtime = mtime      # 避免每次调用都重试同一个坏文件
        return _address_pool_cache


def _city_of(address: str) -> str:
    """从 "412 Oak St, Denver, CO 80205" 里取出城市名（取不到时返回空串）"""
    parts = [p.strip() for p in str(address or "").split(",")]
    return parts[-2] if len(parts) >= 2 else ""


def _pick_real_address(rng, exclude_cities=None) -> tuple:
    """从真实地址库里随机抽一条

    Args:
        exclude_cities: 本次不要选的城市名集合。同一个类目的网站要落到不同城市
            （一批同类目站挤在同一个城市甚至同一条街，是站群最明显的指纹之一）。

    Returns:
        (完整地址, 城市, 州) 或 ("", "", "")（地址库不可用时）
    """
    pool = _load_address_pool()
    if not pool:
        return "", "", ""
    keys = sorted(pool)
    if exclude_cities:
        available = [k for k in keys if k.split("|")[0] not in exclude_cities]
        # 城市都被该类目用完了（地址库城市数 < 该类目网站数）时退回全量，
        # 宁可重复城市也要保证地址真实存在
        keys = available or keys
    city_key = rng.choice(keys)
    street, osm_city, zipcode = rng.choice(pool[city_key])
    # "10811-10819 S Racine Ave" 是整排房屋的号段，取起始号才是单个房屋地址
    num, _, rest = street.partition(" ")
    if "-" in num and rest:
        street = f"{num.split('-')[0]} {rest}"
    state = city_key.split("|")[-1]
    return f"{street}, {osm_city}, {state} {zipcode}", osm_city, state


# 街道名池：全部为住宅街道（树种/自然名 + 住宅路型），用户要求「房屋地址、
# 不要大马路」。注意刻意不含 Road/Highway/Boulevard/Parkway/Commerce 等
# 商业干道味的路型。
# 门牌号与街道名由代码随机生成后写进地址（而不是给模型示例让它自己编）：
# 实测给完整示例时模型会直接照抄（"733 Sunset Way"、"2215 Birch Court, Apt 4B"
# 在多条结果里重复出现），导致整批站点地址雷同。
_STREET_NAMES = (
    "Oak Street", "Maple Avenue", "Cedar Lane", "Birch Court", "Willow Drive",
    "Elm Street", "Pine Avenue", "Spruce Lane", "Aspen Court", "Juniper Drive",
    "Magnolia Avenue", "Sycamore Lane", "Chestnut Street", "Walnut Avenue",
    "Hickory Lane", "Laurel Court", "Poplar Street", "Dogwood Drive",
    "Hawthorne Avenue", "Jasmine Lane", "Rosemary Court", "Clover Drive",
    "Sunset Way", "Sunrise Lane", "Meadow Court", "Brookside Drive",
    "Fairview Terrace", "Highland Avenue", "Ridge Circle", "Valley Lane",
    "Lakeview Drive", "Hillcrest Drive", "Stonebridge Lane", "Windermere Court",
    "Foxglove Lane", "Bluebell Court", "Wren Drive", "Robin Lane",
    "Sparrow Court", "Heron Drive", "Autumn Lane", "Summerfield Court",
    "Winterberry Lane", "Springview Drive", "Orchard Lane", "Grove Street",
)

# 单元号：多数住宅地址没有单元号，所以空串占多数
_STREET_UNITS = ("", "", "", "", "Apt 2A", "Apt 4B", "Apt 12", "Unit 3",
                 "Unit 12", "Unit 204")


def _site_creative_direction(key: str, exclude_cities=None) -> dict:
    """按稳定哈希种子为单个网站抽取创意方向（品牌声线/命名/文案/地理）

    种子取 key 的 SHA-256（Python 内建 hash 受 PYTHONHASHSEED 影响不稳定，
    不能用于跨进程可复现的方向）。同一 key 结果固定；key 中含任务 nonce
    （task_id 含时间戳）时，重跑批次会得到不同的方向组合。

    Args:
        exclude_cities: 不要选的城市名集合，用于把同一类目的网站分散到不同城市

    Returns:
        {"voice", "tone", "domain_style", "title_style", "title_len",
         "desc_angle", "desc_len", "keyword_recipe", "city", "state",
         "street", "address", "desc_opening", "temperature"} — 全部为提示词文本与采样参数
         （address 非空时是真实存在的住宅地址，优先使用；street 仅作回退）
    """
    seed = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    # 优先用真实地址库里抽出的住宅地址（100% 对应真实房屋）；
    # 地址库缺失时退回「代码生成街道 + 模型补 ZIP」的旧方式（不保证真实存在）
    real_address, real_city, real_state = _pick_real_address(rng, exclude_cities)
    if real_address:
        city, state = real_city, real_state
    else:
        city_pool = _ADDRESS_CITIES
        if exclude_cities:
            city_pool = ([c for c in _ADDRESS_CITIES if c[0] not in exclude_cities]
                         or _ADDRESS_CITIES)
        city, state = rng.choice(city_pool)
    # 街道行由代码生成（住宅街道 + 随机门牌号 + 可选单元号），保证每站不同、
    # 且不会出现大马路；模型只负责拼上城市/州/ZIP
    street = f"{rng.randint(3, 9899)} {rng.choice(_STREET_NAMES)}"
    unit = rng.choice(_STREET_UNITS)
    if unit:
        street = f"{street}, {unit}"
    return {
        "address": real_address,
        "voice": rng.choice(_BRAND_VOICES),
        "tone": rng.choice(_TONES),
        "domain_style": rng.choice(_DOMAIN_STYLES),
        "title_style": rng.choice(_TITLE_STYLES),
        "title_len": rng.choice(_TITLE_LENGTHS),
        "desc_angle": rng.choice(_DESC_ANGLES),
        "desc_opening": rng.choice(_DESC_OPENINGS),
        "desc_len": rng.choice(_DESC_LENGTHS),
        "keyword_recipe": rng.choice(_KEYWORD_RECIPES),
        "city": city,
        "state": state,
        "street": street,
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
                    if n.startswith("~$") or n in (STATS_FILE_NAME,
                                                    DOMAIN_STATS_FILE_NAME,
                                                    INFO_FILE_NAME):
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
                           direction: dict = None,
                           avoid_domains: list = None) -> str:
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
    avoid_domains 为已被占用（whois 查到已注册）的域名列表：重新生成时
    写进提示词明确禁止再次输出，避免模型反复给出同一个已注册域名。
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

    avoid_block = ""
    if avoid_domains:
        listed = "\n".join(f"- {str(d).strip()}" for d in avoid_domains[:20])
        avoid_block = (
            "\nALREADY TAKEN domains (verified as registered by someone else — "
            "do NOT output any of these, and do not output near-identical "
            "variations of them):\n"
            f"{listed}\n")

    # 地址规则：地址库命中时给出真实存在的住宅地址并要求逐字照抄；未命中
    # （地址库不可用）时退回「代码生成街道 + 模型补 ZIP」的旧方式
    if d.get("address"):
        address_rule = (
            f'- Store address: use EXACTLY this real, existing US residential address: '
            f'"{d["address"]}". Copy it character for character into the "address" field - '
            f'do not rename the street, do not change the house number, city, state or ZIP, '
            f'never append a unit number, and never substitute another address. '
            f'It is a real home address that has to stay findable on a map.')
        address_field = ('5. "address": copy the real store address given above EXACTLY '
                         '(same characters, same ZIP, nothing appended).')
    else:
        address_rule = (
            f'- Store location: {d["city"]}, {d["state"]}. The address MUST be '
            f'"{d["street"]}, {d["city"]}, {d["state"]} <ZIP>" — copy that street line EXACTLY '
            f'as given (do not rename the street, do not change the house number, do not drop '
            f'the unit number), and choose a ZIP that is plausible for {d["city"]}. This is a '
            f'private residential home address: it must not look like a business park, a '
            f'warehouse or a highway address.')
        address_field = '5. "address": follow the store location line above.'

    return f"""You are a seasoned e-commerce branding consultant. Create the brand identity of ONE independent niche English e-commerce website targeting customers in the United States.

Context: this store belongs to a portfolio of separately-founded stores. Anyone comparing the portfolio will look for stores that read like mass-produced siblings — identical naming habits, phrasing and sentence shapes. Your job is to make THIS store feel like it was started by different people than the rest.

STORE SPECIALTY (main category): {main_cat or "(unknown)"}
Catalog: {total_products} products across {len(clean_cats)} categories
({core_products} in the specialty below, {extra_products} supplementary items in other categories).

SPECIALTY categories ('A|||B' means B is a subcategory of A; the store is built around these — highest priority):
{_lines(core, MAX_CORE_CATEGORIES)}

SUPPLEMENTARY product mix (also carried by the same store; do NOT let these dominate the branding or the title, but DO name them inside the description as adjacent ranges the store also carries):
{_lines(extra, MAX_EXTRA_CATEGORIES)}

CREATIVE DIRECTION assigned to this particular store (its siblings got different ones — follow yours, do not average back toward generic):
- Brand persona: {d["voice"]}
- Writing tone: {d["tone"]}
- Domain naming style: {d["domain_style"]}
- Title style: {d["title_style"]} ({d["title_len"]} characters)
- Description angle: {d["desc_angle"]}
- Description opening: {d["desc_opening"]} — the first sentence must actually follow this, and it must NOT start with a store word ("The catalog", "The store", "The range", "This collection"...).
- Description length: {d["desc_len"]}, written as ONE short paragraph of 2-3 sentences (no line breaks). Target the middle of the range (about 300 characters, 45-55 words): never exceed 330 characters and do not fall below 250.
- Keywords: {d["keyword_recipe"]}
{address_rule}

{_PERSON_RULES}

{avoid_block}
Generate (all in English, for US customers), following the creative direction above:
1. "domain": a brandable .com domain derived from the STORE SPECIALTY, in the naming style assigned above. Lowercase, short and memorable, no www, no scheme, at most 3 words. Do NOT end it with any of these tired suffixes: "pro", "hub", "central", "mart", "store", "shop", "online", "usa", "365", "deals", "best", "top", "direct".
2. "theme": a short natural English phrase naming what the store sells, matching the specialty (a plain description, not a slogan).
3. "title": homepage title that LEADS with the main specialty keyword: the specialty term must be the first thing in the title (or within the first two words) and clearly the dominant keyword, then continue per the assigned title style. Pick the most specific specialty term available from the SPECIALTY list, not a vague umbrella word. Never use the patterns "<keyword> Store", "<keyword> Shop", "<keyword> Online", "<keyword> - Buy <keyword> Online".
4. "description": homepage meta description of about 300 CHARACTERS (characters, NOT words) - one short paragraph of 2-3 sentences, roughly 45-55 words, following the assigned angle and length. LENGTH IS A HARD LIMIT: stay inside the assigned range and never exceed 330 characters - count as you write. Lead with the main specialty keyword and name its most concrete product types in plain customer language; if it fits naturally, mention ONE adjacent range the same store also carries (name the products, never a bare category list and never a "we also sell" dump). Every extra word costs budget, so pick the highest-value keywords instead of listing categories. Never cite product counts or catalog numbers (no "120 products", no "50+ styles") - the description is customer-facing copy, not a catalog summary. Follow the WRITING VOICE rules: make a product, material, use or audience the subject of each sentence (never open with a store word such as "The catalog"), never "we", "our" or "you", and EVERY sentence must contain a verb (no noun fragments such as "Leashes, collars and grooming tools also available."). Do NOT open with "Shop", "Discover", "Find", "Looking for", "Welcome to" or "Explore" (the most common bulk-generated openings), do not repeat the title verbatim inside it, and do not stuff keywords - every category mention must read like a real sentence.
{address_field}
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


# ── 文案人称校验 ──────────────────────────────────────
# 提示词已要求第三人称，但模型仍可能写成店主自述（"We carry..."）或对读者
# 喊话（"You'll find..."）。生成后扫一遍，命中就带针对性提示重试一次。
# "us" 单独用小写匹配：大写的 US 是国家缩写，不是人称代词。
_PERSON_PRONOUN_RE = re.compile(
    r"\b(?:i|we|our|ours|my|mine|you|your|yours|yourself|yourselves|"
    r"we're|we've|we'll|we'd|i'm|i've|i'll|i'd|you're|you've|you'll|you'd)\b",
    re.IGNORECASE)
_US_PRONOUN_RE = re.compile(r"\bus\b")          # 只认小写 us（US = 美国）
_PERSON_CHECK_FIELDS = ("title", "theme", "description")

# 模板化开头：描述以「商店/目录/范围」这类词作主语开头，是站群最明显的批量生成
# 指纹（"The catalog supplies ..."，一批站读起来一模一样）。生成后检测，命中就重试。
_TEMPLATE_OPENING_RE = re.compile(
    r"^\W*(?:the|this|our|its|a)\s+(?:\w+\s+)?"     # 允许一个插入词: "The plumbing catalog"
    r"(?:catalog|catalogue|store|shop|range|collection|selection|inventory|"
    r"assortment|lineup|line-up|line|company|site|website|marketplace)\b",
    re.IGNORECASE)
_TEMPLATE_OPENING_FIELDS = ("description",)

_QUALITY_RETRY_HINT = (
    "\n\n⚠️ 上次输出未通过质量校验，必须同时修正以下所有问题：\n"
    "- 不得出现第一/第二人称（we / our / us / you / your 及缩写），不得对读者喊话（如 \"Order today\"）；\n"
    "- description 的第一句不得以商店词作主语开头（The catalog / The store / The range / "
    "This collection / Our selection / The inventory 等），要用具体商品、材质、用途、人群或场景开头；\n"
    "- 句子必须完整（不得是名词碎片），description 控制在指定字符区间内。")


def _find_person_pronouns(text: str) -> list:
    """返回文案中出现的第一/第二人称代词（去重排序），用于生成后的质量校验"""
    if not text:
        return []
    found = {w.lower() for w in _PERSON_PRONOUN_RE.findall(str(text))}
    if _US_PRONOUN_RE.search(str(text)):
        found.add("us")
    return sorted(found)


def _find_template_opening(text: str) -> str:
    """返回描述开头命中的模板化短语（空串表示合格）"""
    m = _TEMPLATE_OPENING_RE.match(str(text or ""))
    return m.group(0).strip() if m else ""


def _find_quality_issues(info: dict) -> list:
    """生成后的文案质量校验，返回问题描述列表（空列表 = 通过）

    1) 第一/第二人称（提示词要求全篇第三人称）；
    2) 描述以「The catalog / The store / The range」等商店词开头——批量生成指纹，
       一批站读起来是同一个句式。
    """
    issues = []
    for field in _PERSON_CHECK_FIELDS:
        pronouns = _find_person_pronouns(info.get(field))
        if pronouns:
            issues.append(f"{field} 含第一/第二人称 {'/'.join(pronouns)}")
    for field in _TEMPLATE_OPENING_FIELDS:
        opening = _find_template_opening(info.get(field))
        if opening:
            issues.append(f"{field} 以模板化开头「{opening}」")
    return issues


def _regenerate_on_quality_issues(info: dict, config: dict, api_key: str,
                                  prompt: str, direction: dict, log_fn,
                                  folder_name: str) -> dict:
    """文案未通过质量校验（人称 / 模板化开头）时带针对性提示重新生成一次

    Returns:
        重试且合格的新结果；未违规 / 重试失败 / 重试后仍违规时返回原结果
    """
    issues = _find_quality_issues(info)
    if not issues:
        return info

    log_fn(f"[{folder_name}] ⚠ 文案未通过校验：{'；'.join(issues)}，重新生成", "warning")
    try:
        retry_info = _call_site_info_llm(
            config, api_key, prompt + _QUALITY_RETRY_HINT, log_fn=log_fn,
            temperature=direction["temperature"])
    except Exception as e:
        log_fn(f"[{folder_name}] 质量重试失败，沿用原结果: {e}", "warning")
        return info

    still = _find_quality_issues(retry_info)
    if still:
        log_fn(f"[{folder_name}] ⚠ 重试后仍未通过（{'；'.join(still)}），沿用原结果", "warning")
        return info
    log_fn(f"[{folder_name}] ✓ 重试后文案已修正")
    return retry_info


# ── 描述长度兜底 ──────────────────────────────────────
# 描述是 SEO meta 描述，要求约 300 字符（≤330）。模型偶尔写成几百词的段落，
# 超长时按完整句子截断，避免出现半句话，同时保证成品落在要求长度附近。
# 顺带把换行折叠成空格：meta 描述应当是一行。
_DESC_CHAR_CAP = 340


def _cap_description_length(desc: str, max_chars: int = _DESC_CHAR_CAP,
                            log_fn=None, folder_name: str = "") -> str:
    """描述超长时按完整句子截断（兜底，正常应靠提示词约束）

    Args:
        desc: 描述原文
        max_chars: 允许的最大字符数
        log_fn: 日志回调
        folder_name: 仅用于日志

    Returns:
        单行、且不超过 max_chars 的描述；超长时截断到最后一个完整句子
    """
    if not desc:
        return desc
    text = " ".join(str(desc).split())          # 折叠换行/多余空白
    if len(text) <= max_chars:
        return text

    # 按句末标点切分，尽量保留完整句子
    sentences = [s for s in re.findall(r"[^.!?]*[.!?]+\s*|[^.!?]+$", text) if s.strip()]
    kept: list = []
    count = 0
    for s in sentences:
        if kept and count + len(s) > max_chars:
            break
        kept.append(s)
        count += len(s)

    result = "".join(kept).strip() if kept else ""
    if not result or len(result) > max_chars:
        # 第一句就超长（或整段没有句末标点）：按字符硬截断到词边界
        result = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."

    if log_fn:
        log_fn(f"[{folder_name}] 描述超长（{len(text)} 字符 > {max_chars} 字符），"
               f"已截断到 {len(result)} 字符", "warning")
    return result


_domain_check_session = None
_domain_check_lock = threading.Lock()
_domain_check_cache: dict = {}   # {domain: True/False} 进程内缓存，避免重复查询


def _get_domain_check_session():
    """域名查询专用 Session（懒创建；trust_env=False 绕开系统代理绕行）"""
    global _domain_check_session
    with _domain_check_lock:
        if _domain_check_session is None:
            s = requests.Session()
            s.trust_env = False
            s.headers.update({"User-Agent": _DOMAIN_WHOIS_UA,
                              "Accept-Language": "zh-CN,zh;q=0.9"})
            _domain_check_session = s
        return _domain_check_session


def _parse_whois_page(html: str):
    """从 whois 页面判断域名状态

    Returns:
        True  未注册（可注册）
        False 已被注册
        None  无法判断（页面结构变化/内容异常）
    """
    text = html or ""
    # 先判已注册：英文 whois 原文只在真正查到注册信息时出现
    if any(m in text for m in _DOMAIN_TAKEN_MARKERS):
        return False
    if any(m in text for m in _DOMAIN_FREE_MARKERS):
        return True
    return None


def check_domain_available(domain: str, log_fn=None):
    """查询域名是否尚未被注册（西部数码 whois）

    Returns:
        True  可注册（页面显示尚未注册）
        False 已被别人注册（需要重新生成域名）
        None  查询失败或无法判断（网络异常/页面变化），调用方自行决定策略
               —— 不缓存失败结果，下次仍会重查
    """
    d = str(domain or "").strip().lower()
    if not d:
        return None
    with _domain_check_lock:
        if d in _domain_check_cache:
            return _domain_check_cache[d]

    result = None
    for attempt in range(DOMAIN_WHOIS_TRIES):
        try:
            resp = _get_domain_check_session().get(
                DOMAIN_WHOIS_URL.format(domain=d), timeout=DOMAIN_WHOIS_TIMEOUT)
            resp.encoding = "gb2312"
            result = _parse_whois_page(resp.text)
            if result is not None:
                break
            if log_fn:
                log_fn(f"域名 {d} 的 whois 页面无法判断注册状态（第 {attempt + 1} 次）",
                       "warning")
        except Exception as e:
            if log_fn:
                log_fn(f"域名 {d} 的 whois 查询失败（第 {attempt + 1} 次）: {e}", "warning")
        if attempt < DOMAIN_WHOIS_TRIES - 1:
            time.sleep(1)

    if result is not None:
        with _domain_check_lock:
            _domain_check_cache[d] = result
    return result


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
                         log_fn, variant: int = 0,
                         check_domain: bool = True,
                         exclude_cities=None) -> dict:
    """处理单个网站文件夹：读/生成分类统计 -> 调用 LLM -> 返回表格行

    每次只处理一个网站的分类结构（批量任务按顺序逐个调用本函数），
    返回 INFO_COLUMNS 结构的一行，由调用方汇总写入 网站信息.xlsx。
    抛出异常表示该网站生成失败（由调用方决定是否继续下一个）。

    variant 用于域名冲突重试：变化创意方向的随机种子（换一套品牌声线/
    命名风格/城市），常规生成恒为 0，同一 (task_id, folder) 内重试幂等。

    exclude_cities: 同一类目下已经用过的城市，本次不要再选，让同类目的网站
    分散到不同城市（站群指纹）；地址库城市不够时会自动退回允许重复。
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
    direction = _site_creative_direction(f"{task_id}|{folder.name}|v{variant}",
                                         exclude_cities=exclude_cities)

    # ── 调用 LLM（只带这一个网站的分类结构 + 专属创意方向） ──
    # 生成后到西部数码 whois 查该域名是否已被别人注册；已被注册就换一套
    # 创意方向重新生成（最多 DOMAIN_REGEN_RETRIES 次），并把已占用域名写进
    # 提示词禁止复用，避免模型反复给出同一个已注册域名。
    taken_domains: list = []
    domain_note = ""
    for gen_attempt in range(DOMAIN_REGEN_RETRIES + 1):
        if gen_attempt:
            direction = _site_creative_direction(
                f"{task_id}|{folder.name}|v{variant}|regen{gen_attempt}",
                exclude_cities=exclude_cities)
        prompt = build_site_info_prompt(
            stats, folder.name, main_category=main_cat, direction=direction,
            avoid_domains=list(taken_domains) or None)
        info = _call_site_info_llm(config, api_key, prompt, log_fn=log_fn,
                                   temperature=direction["temperature"])
        # 文案质量校验：出现 we/our/you 或模板化开头就重写一次
        # （在域名检查之前，确保查的是最终域名）
        info = _regenerate_on_quality_issues(
            info, config, api_key, prompt, direction, log_fn, folder.name)
        # 地址必须是地址库里那条真实地址：模型重写时若擅自改动，强制改回来
        want_addr = str(direction.get("address") or "").strip()
        if want_addr and str(info.get("address") or "").strip() != want_addr:
            log_fn(f"[{folder.name}] 模型改动了真实地址，已还原为 {want_addr}", "warning")
            info["address"] = want_addr
        if not check_domain:
            break

        domain = info["domain"]
        log_fn(f"[{folder.name}] 查询域名占用情况: {domain}")
        status = check_domain_available(domain, log_fn)
        if status is True:
            log_fn(f"[{folder.name}] ✓ 域名 {domain} 未被注册（可以购买）")
            break
        if status is None:
            # 查询失败不阻塞流程：保留域名，备注里标注未验证
            domain_note = f"域名占用查询失败，{domain} 未验证"
            log_fn(f"[{folder.name}] ⚠ 域名 {domain} 占用状态查询失败，"
                   f"保留该域名但未验证", "warning")
            break

        # status is False：域名已被别人注册
        taken_domains.append(domain)
        if gen_attempt >= DOMAIN_REGEN_RETRIES:
            raise ValueError(
                f"连续 {gen_attempt + 1} 次生成的域名均已被注册: "
                + ", ".join(taken_domains))
        log_fn(f"[{folder.name}] ⚠ 域名 {domain} 已被注册，换创意方向重新生成"
               f"（{gen_attempt + 1}/{DOMAIN_REGEN_RETRIES}）", "warning")

    # 描述长度兜底：模型偶尔写到 400+ 词，超过用户要求的「300 词左右」
    info["description"] = _cap_description_length(
        info["description"], log_fn=log_fn, folder_name=folder.name)

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
        "备注": domain_note,
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


def _store_row(rows: list, row_index: dict, site_name: str, row: dict) -> None:
    """把某网站的结果写进结果表：已有该站的行就原地替换，否则追加

    重跑任务时同一网站不能堆出多行（上次失败的行这次成功了要覆盖掉）。
    """
    if site_name in row_index:
        rows[row_index[site_name]] = row
    else:
        row_index[site_name] = len(rows)
        rows.append(row)


# 单个网站生成失败（LLM 报错 / 返回格式异常 / 域名冲突）时的自动重试次数。
# 每次重试都换一套创意方向（variant 递增），因此重试会得到不同的域名与文案。
_SITE_GEN_RETRIES = 3


def _category_key(folder_name: str) -> str:
    """文件夹名 -> 类目分组键

    同一个类目可能被分成多个网站文件夹（"Faucets_1"、"Faucets_2"），
    文件夹名不同但类目相同，所以要剥掉结尾的序号再分组，否则同一类目的
    网站会被当成不同类目、照样挤在同一个城市。
    """
    name = _clean_main_category(folder_name).lower()
    return re.sub(r"[\s_-]*\d+$", "", name).strip() or name


def _generate_site_row(task_id: str, site: Path, config: dict, api_key: str,
                       log_fn, used_domains: set, variant_start: int = 0,
                       exclude_cities=None) -> dict:
    """生成单个网站的网站信息，失败或域名重复时自动重试

    用户要求「生成失败的再次运行」：单站失败不再直接记一行失败，而是换一套
    创意方向重试若干次（含批内域名重复的情况），全部失败才记失败。

    Returns:
        该网站的信息行（含「域名」「标题」等列）

    Raises:
        InterruptedError: 任务被停止
        Exception: 重试用尽后的最后一次错误
    """
    last_error = None
    for attempt in range(_SITE_GEN_RETRIES):
        variant = variant_start + attempt
        try:
            row = _process_site_folder(task_id, site, config, api_key, log_fn,
                                       variant=variant,
                                       exclude_cities=exclude_cities)
        except InterruptedError:
            raise
        except Exception as e:
            last_error = e
            if attempt < _SITE_GEN_RETRIES - 1:
                delay = 3 * (attempt + 1)
                log_fn(f"[{site.name}] 第 {attempt + 1} 次生成失败: {e}；"
                       f"{delay}s 后自动重试（换创意方向）", "warning")
                time.sleep(delay)
                continue
            raise

        domain = str(row.get("域名") or "").strip()
        if domain and domain in used_domains:
            last_error = ValueError(f"域名 {domain} 与其他网站重复（已重试 {_SITE_GEN_RETRIES} 次）")
            if attempt < _SITE_GEN_RETRIES - 1:
                log_fn(f"[{site.name}] 域名 {domain} 与已生成网站重复，换创意方向重试",
                       "warning")
                continue
            raise last_error
        return row
    raise last_error or RuntimeError("网站信息生成失败")


def run_batch_site_info_task(task_id: str, folder, model_value: str = "",
                             model_id_override: str = "", site_db=None,
                             only_empty: bool = True):
    """批量 AI 生成网站信息后台任务体

    遍历所选文件夹下所有「最后一层文件夹」（每个 = 一个网站的数据），
    按顺序逐个生成网站信息（每次只把当前一个网站的分类结构交给模型，
    不会一次性把所有网站的分类结构发给模型）：
    读取/生成该网站的分类统计 -> LLM 生成域名/标题/描述等 -> 汇总为一行，
    全部结果写入所选文件夹下的 网站信息.xlsx（每个网站一行）。
    每完成一个网站就落盘一次（中断也能保留已生成的行）；
    失败的网站会换创意方向自动重试，仍失败才以「备注」列记录原因，不影响其余网站。

    only_empty=True（默认）时是增量模式：已经有网站信息（域名列非空）的网站直接
    跳过、不覆盖；上次失败留下的行（域名列为空）仍算「网站信息为空」，所以重跑
    任务就是对失败项的再次运行。

    Args:
        task_id: 任务 ID
        folder: 父文件夹路径（如数据分配输出的分配文件夹）
        model_value / model_id_override / site_db: 同 run_site_info_task
        only_empty: 只生成网站信息为空的网站（默认 True）
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

        # ── 增量模式：读出已有结果，只生成「网站信息为空」的网站 ──
        out_path = folder / INFO_FILE_NAME
        rows: list[dict] = []
        if out_path.exists():
            try:
                rows = read_site_info_excel(out_path)
            except Exception as e:
                _log(f"读取已有 {INFO_FILE_NAME} 失败，将全部重新生成: {e}", "warning")
                rows = []
        # 网站名 -> 行下标：重跑时原地替换该站的行，避免同一网站堆出多行
        row_index = {str(r.get("网站（文件夹）") or "").strip(): i
                     for i, r in enumerate(rows)}
        # 域名列非空 = 这个网站的网站信息已生成过，跳过不覆盖；
        # 上次失败的行域名列为空，仍算「为空」，所以重跑即重试失败项
        done = {name for name, idx in row_index.items()
                if str(rows[idx].get("域名") or "").strip()}
        skipped = 0
        if only_empty and done:
            _log(f"增量模式：已有 {len(done)} 个网站信息，跳过它们，"
                 f"只生成信息为空的网站（共 {len(sites)} 个网站）")
        # 批内域名去重：已生成过的域名也要计入，避免新站撞上老站的域名
        used_domains: set[str] = {
            str(rows[idx].get("域名") or "").strip() for idx in row_index.values()
        } - {""}
        # 同一类目的网站要分到不同城市。先按已有结果回填该类目已用过的城市，
        # 否则重跑时新站会又落回老站的城市。
        cities_by_category: dict[str, set[str]] = {}
        for name, idx in row_index.items():
            city = _city_of(rows[idx].get("地址"))
            if name and city:
                cities_by_category.setdefault(
                    _category_key(name) or name, set()).add(city)

        # ── 按顺序逐个网站生成，逐行累积写入表格 ──
        succeeded: list[Path] = []
        failed: list[tuple[str, str]] = []
        for i, site in enumerate(sites):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            if only_empty and site.name in done:
                skipped += 1
                task_manager.update(
                    task_id, progress=int(i / len(sites) * 100),
                    message=f"[{i + 1}/{len(sites)}] 跳过（已有网站信息）: {site.name}")
                _log(f"[{i + 1}/{len(sites)}] 跳过（已有网站信息）: {site.name}")
                continue
            task_manager.update(
                task_id, progress=int(i / len(sites) * 100),
                message=f"[{i + 1}/{len(sites)}] 正在生成: {site.name}")
            try:
                _log(f"[{i + 1}/{len(sites)}] 开始生成网站信息: {site.name}")
                cat_key = _category_key(site.name) or site.name
                used_cities = cities_by_category.setdefault(cat_key, set())
                row = _generate_site_row(task_id, site, config, api_key, _log,
                                         used_domains, exclude_cities=used_cities)
                used_domains.add(str(row.get("域名") or "").strip())
                city = _city_of(row.get("地址"))
                if city:
                    used_cities.add(city)
                    _log(f"[{i + 1}/{len(sites)}] 类目「{cat_key}」已用城市 "
                         f"{len(used_cities)} 个，本站落在 {city}")
                _store_row(rows, row_index, site.name, row)
                succeeded.append(site)
                _log(f"[{i + 1}/{len(sites)}] ✓ {site.name} 完成: "
                     f"{row['域名']} | {row['标题']}")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
                return
            except Exception as e:
                failed.append((site.name, str(e)))
                log.error(f"网站 {site.name} 信息生成失败: {e}")
                _log(f"[{i + 1}/{len(sites)}] ✗ {site.name} 生成失败: {e}"
                     f"（已重试 {_SITE_GEN_RETRIES} 次，继续下一个）", "error")
                # 失败也占一行（备注列记录原因）；域名为空，重跑任务会再次尝试
                _store_row(rows, row_index, site.name,
                           {"网站（文件夹）": site.name, "备注": f"生成失败: {e}"})
            # 每完成一个网站就落盘，中断/失败也能保留已生成的行
            _write_info_excel(out_path, rows)

        # ── 汇总 ──
        parts = [f"完成: 批量生成 {len(succeeded)}/{len(sites)} 个网站信息"]
        if skipped:
            parts.append(f"（跳过已有 {skipped}）")
        if failed:
            parts.append(f"（失败 {len(failed)}: "
                         + ", ".join(n for n, _ in failed) + "；"
                         "重新运行任务会自动重试这些网站）")
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
