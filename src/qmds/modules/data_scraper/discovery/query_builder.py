"""Shopify 店铺发现 — 搜索变体构建与噪声过滤

基于 2026-07 ~ 2026-09 三份生产日志（约 2.2 万次搜索调用、10 万+ 次平台检测）的实证分析：

- 旧三变体（{kw} inurl:collections/all / - page 123 / - page 88）单次调用产出接近
  （约 7.6~7.8 URL/次），但整体 Shopify 命中率仅约 11.5%，88.5% 的平台检测消耗在
  非 Shopify 域名上；
- 噪声主要来自两类：① 社媒/电商巨头/图库/资讯站（youtube、facebook、amazon、
  pinterest、reddit 等头部域名）；② Google 在 dork 结果耗尽后会对查询"放宽"
  （relaxation），深翻页（实测 27~38 页仍返回 5~10 条）混入大量纯关键词匹配页面。

变体设计（用户需求：变体应为"用户购买商品时常用的长尾词"）：
1. Shopify 指纹变体：inurl:collections/all（URL 指纹，实证有效）与
   "powered by shopify"（页脚文案指纹），保证搜索结果的 Shopify 占比；
2. 购物长尾词变体：围绕用户购买意图（best / cheap / for sale / buy online /
   online store / near me）生成的长尾表达，叠加指纹扩大店铺召回——
   不同长尾词命中的店铺集合差异大，比机械拼接排除符更能发现新店；
3. 查询保持干净：不在查询里拼接 -site: 排除串（Google 词数上限、可读性）；
   噪声域名由本地黑名单 NOISE_DOMAINS 在结果层面兜底过滤（filter_urls），
   零误杀（这些域名不可能承载 Shopify 店铺）；
4. Exa 语义查询用自然语言长尾词（build_exa_variants），不识别 Google 运算符。
"""

import re


# ── 本地噪声域名黑名单（filter_urls 使用，零误杀：这些域名不可能承载 Shopify 店铺）──
# 匹配规则：精确域名或子域名后缀（endswith(".{noise}")）。
NOISE_DOMAINS = (
    # 社媒
    "youtube.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "tiktok.com", "pinterest.com", "reddit.com",
    "quora.com", "linkedin.com", "medium.com", "weibo.com", "twitch.tv",
    # 电商巨头 / 零售
    "etsy.com", "target.com", "homedepot.com", "lowes.com",
    "aliexpress.com", "alibaba.com", "bestbuy.com", "shein.com", "temu.com",
    # 图库 / 设计素材
    "shutterstock.com", "alamy.com", "gettyimages.com", "istockphoto.com",
    "dreamstime.com", "vecteezy.com", "adobe.com", "freepik.com",
    # 资讯 / 百科 / 文档 / 问答
    "wikipedia.org", "merriam-webster.com", "scribd.com", "nytimes.com",
    "github.com", "stackoverflow.com", "researchgate.net",
    "sciencedirect.com", "nih.gov", "fda.gov", "usa.gov", "un.org",
    # 应用商店 / 平台自身 / 店铺目录聚合站
    "apple.com", "shopify.com", "storeleads.app", "builtwith.com",
    "xpareto.com", "similarweb.com", "myip.ms", "wappalyzer.com",
)

# 多 TLD 零售/搜索巨头：任意 TLD 子域都不可能是店铺
NOISE_DOMAIN_PREFIXES = ("amazon.", "ebay.", "walmart.", "google.")


def is_noise_domain(domain: str) -> bool:
    """判断域名是否属于噪声黑名单（精确或子域名后缀匹配）

    例: youtube.com / m.youtube.com / us.amazon.com / google.co.uk 均为噪声；
        acmestore.com / notyoutube.com 不是噪声。
    """
    d = (domain or "").lower().strip().lstrip(".")
    if not d:
        return False
    # 多 TLD 巨头：amazon./ebay./walmart./google. 的任意 TLD 及其任意子域
    # （如 us.amazon.com / amazon.co.uk / play.google.com）
    for p in NOISE_DOMAIN_PREFIXES:
        core = p.rstrip(".")
        if d.startswith(p) or f".{core}." in d:
            return True
    return any(d == n or d.endswith("." + n) for n in NOISE_DOMAINS)


# ── 购物长尾词模板 ──────────────────────────────────────────
# (模板, 跳过条件)：关键词已包含全部跳过词时不再生成该模板（避免重复表达，
# 例如关键词已含 "for sale" 时不再生成 "{kw} for sale"）。
# 按通用性排序，前缀型优先（不与原关键词重复）。
LONGTAIL_PATTERNS = (
    ("best {kw}", ("best",)),
    ("cheap {kw}", ("cheap",)),
    ("{kw} for sale", ("for", "sale")),
    ("buy {kw} online", ("buy",)),
    ("{kw} online store", ("online", "store")),
    ("{kw} near me", ("near", "me")),
)

# 每个关键词生成长尾变体数量上限（控制在 API 额度可接受范围）
LONGTAIL_VARIANT_LIMIT = 3


def _norm_kw(kw: str) -> str:
    """关键词去首尾/压缩空白"""
    return " ".join(kw.split())


def _longtail_variants(kw: str, limit: int = LONGTAIL_VARIANT_LIMIT) -> list[str]:
    """按购物意图模板生成长尾词变体（去重、跳过与关键词重复的表达）

    例: kw="vinyl records" -> ["best vinyl records", "cheap vinyl records",
                              "vinyl records for sale"]
        kw="vinyl records for sale near me" -> ["best vinyl records for sale near me",
                              "cheap vinyl records for sale near me",
                              "buy vinyl records for sale near me online"]
    """
    kw_lower = kw.lower()
    out = []
    for pattern, skip_words in LONGTAIL_PATTERNS:
        if len(out) >= limit:
            break
        if all(w in kw_lower for w in skip_words):
            continue
        candidate = pattern.format(kw=kw)
        if candidate.lower() == kw_lower:
            continue
        out.append(candidate)
    return out


def build_query_variants(kw: str) -> list[str]:
    """构建 Google 系 provider 的搜索变体：Shopify 指纹 + 用户购物长尾词

    每个关键词生成 5 条变体：
    1. {kw} inurl:collections/all —— Shopify 店铺 URL 指纹（实证有效）
    2. {kw} "powered by shopify" —— Shopify 页脚文案指纹（补充召回）
    3~5. 购物长尾词 × inurl:collections/all —— 覆盖用户购买商品时的常用
        表达（best / cheap / for sale / buy online / online store / near me），
        叠加指纹保证搜索结果的 Shopify 占比。

    说明：查询保持干净，不在查询里拼接 -site: 排除串；噪声域名由
    filter_urls 的本地黑名单在结果层面兜底过滤。
    """
    kw = _norm_kw(kw)
    if not kw:
        return ["inurl:collections/all"]
    variants = [
        f"{kw} inurl:collections/all",
        f'{kw} "powered by shopify"',
    ]
    for lt in _longtail_variants(kw):
        variants.append(f"{lt} inurl:collections/all")
    return variants


def build_exa_variants(kw: str) -> list[str]:
    """构建 Exa 语义搜索变体（自然语言长尾词，不含 Google 运算符）

    Exa 是语义搜索引擎，识别"意图"而非布尔语法：
    - "{kw} online store"：把语义检索偏向电商店铺页；
    - 首个购物长尾词（如 "best {kw}"）：覆盖用户购买表达，扩大召回。
    """
    kw = _norm_kw(kw)
    if not kw:
        return ["online store powered by shopify"]
    longtails = _longtail_variants(kw, limit=1)
    return [f"{kw} online store"] + longtails


# ── dork 命中判定（翻页提前停止用）────────────────────────────

_DORK_TOKEN_RE = re.compile(r"(?:inurl|site):(\S+)", re.IGNORECASE)


def extract_dork_tokens(query: str) -> list[str]:
    """从查询中提取 dork 运算符目标（inurl:/site:）

    例: "dog toys inurl:collections/all" -> ["collections/all"]
        "gloves site:*.myshopify.com" -> ["myshopify.com"]
    """
    tokens = []
    for m in _DORK_TOKEN_RE.finditer(query or ""):
        token = m.group(1).lower().lstrip("*.")
        if token:
            tokens.append(token)
    return tokens


def count_dork_hits(urls: list[str], query: str) -> int:
    """统计结果 URL 中仍匹配 dork 指纹的数量

    用于翻页提前停止：Google 在严格 dork 结果耗尽后会对查询放宽
    （relaxation），深翻页返回的大多是纯关键词匹配的噪声页面。
    当连续多页的 URL 都不再匹配 dork 指纹时，说明该查询已被放宽，
    继续翻页只会浪费 API 额度并灌入噪声。

    非 dork 查询（无 inurl:/site: 运算符）返回 len(urls)，永不触发停止。
    """
    tokens = extract_dork_tokens(query)
    if not tokens:
        return len(urls)
    hits = 0
    for u in urls:
        low = (u or "").lower()
        if ".myshopify.com" in low or any(t in low for t in tokens):
            hits += 1
    return hits
