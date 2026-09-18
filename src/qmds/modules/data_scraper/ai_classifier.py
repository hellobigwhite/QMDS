"""AI 分类器 - 基于 LLM（MiMo）对 Shopify 站点进行类目分类

从源项目 Index_URL_Shopify/classify_shopify.py 和 run_shopify_discovery.py 移植：
- build_prompt: 21 个 Google 分类 + 黑五类 + 综合站提示词
- extract_page_info: 从 HTML 提取 title/meta/nav/shop_name 等信息
- fetch_and_clean: 抓取首页并清洗（走 QMDS HttpClient 代理）
- detect_language / is_non_english: 语言检测（CJK + langdetect）
- classify_store: 调用 MiMo LLM 分类，3 次重试
- google_to_qmds_category: Google 分类名 -> QMDS 简化名反向映射
- classify_batch: 并发分类
"""

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests as _requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from qmds.config import settings
from qmds.config.categories import SHOPIFY_TO_GOOGLE_CATEGORY
from qmds.config.llm_models import (
    get_llm_model_config,
    get_llm_api_key,
    has_llm_api_key,
    get_llm_extra_body,
    get_llm_system_message,
    get_llm_default_headers,
    extract_llm_text,
    chat_completion_with_fallback,
)
from qmds.core.exceptions import ProxyError, RateLimitError
from qmds.utils.logger import get_logger

log = get_logger("ai_classifier")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# langdetect 为强制依赖（requirements.txt 已声明）
try:
    from langdetect import detect, DetectorFactory, LangDetectException
    DetectorFactory.seed = 0
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False
    log.warning("未安装 langdetect，将仅依赖 html_lang 字段判断语言")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    log.warning("未安装 openai，AI 分类功能不可用")


MIN_TEXT_LEN_FOR_DETECT = 10

# Google 分类名 -> QMDS 简化名 反向映射
_GOOGLE_TO_QMDS: dict[str, str] = {v: k for k, v in SHOPIFY_TO_GOOGLE_CATEGORY.items()}


def google_to_qmds_category(google_name: str) -> Optional[str]:
    """将 Google Taxonomy 一级分类名转换为 QMDS 简化分类名

    示例:
        "Hardware" -> "hardware"
        "Animals & Pet Supplies" -> "animals_pet_supplies"
        未知分类返回 None
    """
    if not google_name:
        return None
    name = google_name.strip()
    return _GOOGLE_TO_QMDS.get(name)


# =========================
# LLM 客户端（懒加载）
# =========================

_glm_client: Optional["OpenAI"] = None
_glm_client_config: Optional[dict] = None


def _resolve_runtime_config(site_db=None) -> dict:
    """解析当前生效的模型配置（site_db 的 llm_model 设置优先于 settings）"""
    model_value = settings.llm_model
    if site_db is not None:
        model_value = site_db.get_setting("llm_model", "") or model_value
    return get_llm_model_config(model_value, site_db)


def _get_glm_client(config: dict, site_db=None) -> "OpenAI":
    """获取 LLM 客户端（懒加载，复用连接）

    从统一配置 llm_models.py 读取 base_url / api_key / model_id，
    支持 MiMo 和 Ark 两种 provider。

    缓存命中判定只看 (provider, base_url, model_id)：
    - 同模型复用客户端（避免 MiMo key 轮换导致每次重建）
    - 切换模型自动重建，保证 Web 配置页切换后立即生效，
      不会出现「Ark 配置 + MiMo 旧客户端」的错配
    - 同模型更换 API Key 时由 config_routes 保存动作触发
      reset_glm_client() 强制刷新
    """
    global _glm_client, _glm_client_config
    if not HAS_OPENAI:
        raise RuntimeError("openai 未安装，无法创建 LLM 客户端")

    if (_glm_client is not None and _glm_client_config is not None
            and _glm_client_config.get("provider") == config.get("provider")
            and _glm_client_config.get("base_url") == config.get("base_url")
            and _glm_client_config.get("model_id") == config.get("model_id")):
        return _glm_client

    if _glm_client is not None and _glm_client_config is not None:
        log.info(f"LLM 模型配置变更，重建客户端: "
                 f"{_glm_client_config['model_id']} -> {config['model_id']}")

    api_key = get_llm_api_key(config, site_db)
    if not api_key:
        raise RuntimeError(f"LLM API Key 未配置（provider={config['provider']}）")

    _glm_client = OpenAI(
        base_url=config["base_url"],
        api_key=api_key,
        default_headers=get_llm_default_headers(config),
    )
    _glm_client_config = config
    log.info(f"LLM 客户端已创建: {config['label']} (provider={config['provider']}, model={config['model_id']})")
    return _glm_client


def reset_glm_client():
    """重置 LLM 客户端（测试用 / 模型切换时调用）"""
    global _glm_client, _glm_client_config
    _glm_client = None
    _glm_client_config = None


# =========================
# HTML 解析与抓取
# =========================

_BROWSER_HEADERS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
]

HEADERS = _BROWSER_HEADERS[0]  # 保持向后兼容，实际使用 _get_random_headers()


def _get_random_headers() -> dict:
    return random.choice(_BROWSER_HEADERS).copy()


_fallback_session: Optional[_requests.Session] = None


def _get_fallback_session() -> _requests.Session:
    global _fallback_session
    if _fallback_session is not None:
        return _fallback_session
    _fallback_session = _requests.Session()
    retry_strategy = Retry(
        total=1,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=60)
    _fallback_session.mount("http://", adapter)
    _fallback_session.mount("https://", adapter)
    return _fallback_session


# 远程代理服务（第1级降级）- 地址与 key 来自 settings（PROXY_SERVICE_URL / PROXY_SERVICE_KEY 可覆盖）
_PROXY_SERVICE_URL = settings.proxy_service_url
_PROXY_SERVICE_KEY = settings.proxy_service_key
_PROXY_SERVICE_TIMEOUT = 60

# 远程代理服务熔断：该服务不可用时逐个域名重试会各等满 _PROXY_SERVICE_TIMEOUT 秒，
# 连续失败达阈值后进入冷却期，冷却期内直接返回 None 走第2/3级降级。
_PROXY_SERVICE_FAILURE_THRESHOLD = 3
_PROXY_SERVICE_COOLDOWN = 300.0
_proxy_service_lock = threading.Lock()
_proxy_service_failures = 0
_proxy_service_down_until = 0.0

# 直连降级（第3级降级）- 限制并发避免本机 IP 被封
_direct_semaphore = threading.Semaphore(5)
_direct_session: Optional[_requests.Session] = None


def _get_direct_session() -> _requests.Session:
    """直连专用 session（无代理），独立于 fallback session 避免代理参数污染"""
    global _direct_session
    if _direct_session is not None:
        return _direct_session
    _direct_session = _requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
    _direct_session.mount("http://", adapter)
    _direct_session.mount("https://", adapter)
    return _direct_session


def _record_proxy_service_failure():
    """记录一次远程代理服务故障，达到阈值后熔断冷却"""
    global _proxy_service_failures, _proxy_service_down_until
    with _proxy_service_lock:
        _proxy_service_failures += 1
        if (_proxy_service_failures >= _PROXY_SERVICE_FAILURE_THRESHOLD
                and time.time() >= _proxy_service_down_until):
            _proxy_service_down_until = time.time() + _PROXY_SERVICE_COOLDOWN
            log.warning(
                f"远程代理服务连续失败 {_proxy_service_failures} 次，"
                f"熔断 {_PROXY_SERVICE_COOLDOWN:.0f} 秒（期间直接走第2/3级降级）"
            )


def _record_proxy_service_success():
    global _proxy_service_failures
    with _proxy_service_lock:
        _proxy_service_failures = 0


def _proxy_service_fetch(url: str) -> Optional[_requests.Response]:
    """通过远程代理服务请求 URL，返回 Response 或 None

    熔断冷却期内不再发起请求，避免每个域名都等满 60 秒超时。
    """
    with _proxy_service_lock:
        if time.time() < _proxy_service_down_until:
            return None
    try:
        resp = _requests.get(
            _PROXY_SERVICE_URL,
            params={"key": _PROXY_SERVICE_KEY, "url": url},
            timeout=_PROXY_SERVICE_TIMEOUT,
        )
    except Exception as e:
        _record_proxy_service_failure()
        log.debug(f"远程代理服务失败 {url}: {type(e).__name__}: {e}")
        return None
    if resp.status_code == 200 and len(resp.text) > 100:
        _record_proxy_service_success()
        return resp
    # 服务端故障（5xx/403/429）计入熔断；其余状态码是目标站点响应，不该拖垮服务本身
    if resp.status_code >= 500 or resp.status_code in (403, 429):
        _record_proxy_service_failure()
    log.debug(f"远程代理服务返回无效 {url}: status={resp.status_code} len={len(resp.text)}")
    return None


def _clean_html(text: str) -> str:
    """清洗 HTML 为纯文本摘要（不发起 HTTP 请求，仅处理文本）

    从 fetch_and_clean 中抽取的清洗逻辑，供 fetch_page_info 复用同一份 HTML。

    Args:
        text: 原始 HTML 文本

    Returns:
        清洗后的纯文本（最多 2000 字符），空字符串如果输入为空
    """
    if not text:
        return ""
    html = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'\s+', ' ', html).strip()
    return html[:2000]


def fetch_and_clean(domain: str, http_client=None) -> str:
    """抓取首页并清洗，返回纯文本摘要

    Args:
        domain: 店铺域名（不含 scheme）
        http_client: QMDS HttpClient 实例（走代理）；为 None 时用 requests 直连

    Returns:
        清洗后的首页文本（最多 2000 字符），失败返回空字符串
    """
    try:
        url = f"https://{domain}/"
        if http_client is not None:
            resp = http_client.get(url, timeout=10)
            if resp.status_code != 200:
                return ""
            text = resp.text
        else:
            resp = _get_fallback_session().get(
                url, headers=_get_random_headers(), timeout=settings.request_timeout,
                allow_redirects=True, verify=False,
            )
            if resp.status_code != 200:
                return ""
            text = resp.text

        return _clean_html(text)
    except Exception as e:
        log.debug(f"fetch_and_clean 失败 {domain}: {e}")
        return ""


def extract_page_info(html: str) -> dict:
    """从 HTML 提取页面信息（title/meta/nav/shop_name 等）

    移植自 run_shopify_discovery.py:533-586
    """
    info = {}

    m = re.search(r'<html[^>]*\blang\s*=\s*["\']([a-zA-Z\-]+)["\']', html, re.IGNORECASE)
    if m:
        info["html_lang"] = m.group(1).lower()

    m = re.search(r'"shop"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', html)
    if m:
        info["shop_name"] = m.group(1).strip()

    clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'<link[^>]*>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'<!--.*?-->', '', clean, flags=re.DOTALL)
    # 防御 1：折叠连续空白。个别超大首页（如 broadwaylifestyle.com，约 7.8MB）清洗后仍残留
    # 数万字符的连续空白段，会让下方 \s+ 的贪婪回溯呈 O(L^2) 膨胀；re 在 C 层执行期间不释放
    # GIL，会以 100% 单核 CPU 卡死整个进程（Web 无响应、任务停摆、日志停止写入）。
    clean = re.sub(r'\s{2,}', ' ', clean)
    # 防御 2：属性值限定 300 字符内且引号成对，避免未闭合引号引发跨兆字节的回溯扫描
    clean = re.sub(
        r'\s+(class|id|style|data-[\w.-]+|on\w+|itemprop|itemscope|itemtype|role|tabindex|aria-[\w-]+|datetime)=("[^"]{0,300}"|\'[^\']{0,300}\')',
        '', clean,
    )

    m = re.search(r'<title>(.*?)</title>', clean, flags=re.DOTALL)
    if m:
        info["title"] = m.group(1).strip()

    m = re.search(r'<meta[^>]*name\s*=\s*["\']description["\'][^>]*content\s*=\s*["\']([^"\']*)["\']', clean)
    if not m:
        m = re.search(r'<meta[^>]*content\s*=\s*["\']([^"\']*)["\'][^>]*name\s*=\s*["\']description["\']', clean)
    if m:
        info["meta_description"] = m.group(1).strip()

    for prop in ["og:site_name", "og:title", "og:description"]:
        esc = prop.replace(":", r"\:")
        m = re.search(
            r'<meta[^>]*property\s*=\s*["\']' + esc + r'["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
            clean,
        )
        if not m:
            m = re.search(
                r'<meta[^>]*content\s*=\s*["\']([^"\']*)["\'][^>]*property\s*=\s*["\']' + esc + r'["\']',
                clean,
            )
        if m:
            info[prop.replace(":", "_")] = m.group(1).strip()

    nav_m = re.search(r'<nav[^>]*>(.*?)</nav>', clean, flags=re.DOTALL)
    if nav_m:
        links = re.findall(r'<a[^>]*>(.*?)</a>', nav_m.group(1), flags=re.DOTALL)
        cats = []
        for link in links:
            text = re.sub(r'<[^>]+>', '', link).strip()
            if text and len(text) < 50 and text not in cats:
                cats.append(text)
        if cats:
            info["nav_categories"] = cats[:30]

    keywords = []
    for pattern in ["collections", "categories", "products"]:
        if re.search(r'href\s*=\s*["\'][^"\']*' + pattern, clean):
            keywords.append(pattern)
    if keywords:
        info["url_keywords"] = keywords

    return info


# =========================
# 完整网站信息获取（仿照 run_shopify_discovery.py）
# =========================

def fetch_page_info(domain: str, http_client=None, proxies=None, proxy_manager=None) -> dict:
    """获取网站的完整 page_info（仿照 run_shopify_discovery.py 的 verify_shopify 逻辑）

    流程：
    1. 请求首页原始 HTML（含 <title>/<meta>/<nav> 标签），用 extract_page_info 提取结构化信息
    2. 同一份 HTML 清洗为 homepage_content（供 LLM 兜底），不重复请求
    3. 请求 /collections.json?limit=100 获取 collection_titles
    4. 合并返回完整 page_info

    首页请求采用三级降级策略：
    - 第1级：远程代理服务（66.154.112.62:8000）- 独立 IP，不被 Cloudflare 限流
    - 第2级：本地代理池（proxies.txt / HttpClient）- 数据中心 IP，可能被 429
    - 第3级：直连本机 IP - 限流 5 并发，避免被封

    /collections.json 只走第2级（本地代理），失败即跳过，不降级。

    Args:
        domain: 店铺域名（不含 scheme）
        http_client: QMDS HttpClient 实例（走本地代理池）
        proxies: 代理字典如 {"http": "...", "https": "..."}，仅在 http_client=None 时生效
        proxy_manager: ProxyManager 实例，用于 429 时标记坏代理

    Returns:
        page_info dict，包含:
        - title, meta_description, nav_categories, shop_name, url_keywords, html_lang
        - collection_titles: list[str]
        - homepage_content: str（清洗后的纯文本摘要，供 LLM 兜底）
        抓取失败时返回空 dict {}
    """
    base = f"https://{domain}"
    page_info: dict = {}

    _timeout = (5, 15)

    # --- 第2级：本地代理池请求函数 ---
    _fallback = _get_fallback_session()

    def _do_get_proxy(url):
        """通过本地代理池请求，429/异常返回 None"""
        if http_client is not None:
            try:
                return http_client.get(url, timeout=_timeout, verify=False)
            except RateLimitError:
                return None
            except ProxyError as e:
                # 代理池不可用（如 402 欠费整池熔断）时不再中断降级链，交由第3级直连
                log.info(f"本地代理池失败 {domain}，降级直连: {str(e)[:160]}")
                return None
        else:
            try:
                return _fallback.get(
                    url, headers=_get_random_headers(), timeout=_timeout,
                    allow_redirects=True, proxies=proxies, verify=False,
                )
            except Exception:
                return None

    def _mark_proxy_bad():
        """标记当前代理为坏（429 时调用）"""
        if proxy_manager and http_client is None and proxies:
            proxy_manager.mark_bad(proxies, cooldown=120.0)

    # --- 第3级：直连请求函数（限流 5 并发） ---
    _direct = _get_direct_session()

    def _do_get_direct(url):
        """直连本机 IP 请求（限流 5 并发）"""
        with _direct_semaphore:
            return _direct.get(
                url, headers=_get_random_headers(), timeout=_timeout,
                allow_redirects=True, verify=False,
            )

    # --- 三级降级：远程代理 -> 本地代理池 -> 直连 ---
    def _do_get_with_fallback(url):
        """首页请求：三级降级"""
        # 第1级：远程代理服务
        resp = _proxy_service_fetch(url)
        if resp is not None and resp.status_code == 200:
            return resp

        # 第2级：本地代理池
        resp = _do_get_proxy(url)
        if resp is not None and resp.status_code != 429:
            return resp
        if resp is not None and resp.status_code == 429:
            _mark_proxy_bad()
            log.info(f"本地代理 429 限流 {domain}，降级直连")

        # 第3级：直连降级
        try:
            return _do_get_direct(url)
        except Exception as e:
            log.info(f"直连也失败 {domain}: {type(e).__name__}: {e}")
            return None

    # 1. 请求首页原始 HTML -- 走三级降级
    raw_html = ""
    try:
        resp = _do_get_with_fallback(f"{base}/")
        if resp and resp.status_code == 200:
            raw_html = resp.text
        if raw_html:
            page_info = extract_page_info(raw_html)
    except Exception as e:
        log.info(f"fetch_page_info 首页请求失败 {domain}: {type(e).__name__}: {e}")

    # 2. 复用同一份 HTML 清洗为 homepage_content（不重复请求首页）
    page_info["homepage_content"] = _clean_html(raw_html) if raw_html else ""

    # 3. 请求 /collections.json -- 只走本地代理池，不降级
    try:
        resp = _do_get_proxy(f"{base}/collections.json?limit=100")
        if resp and resp.status_code == 429:
            _mark_proxy_bad()
        text = resp.text if resp and resp.status_code == 200 else ""
        if text and '"collections"' in text:
            data = json.loads(text)
            titles = [
                c["title"] for c in data.get("collections", [])
                if c.get("title")
            ]
            if titles:
                page_info["collection_titles"] = titles
    except Exception as e:
        log.debug(f"fetch_page_info collections.json 请求失败 {domain}: {type(e).__name__}: {e}")

    if "collection_titles" not in page_info:
        page_info["collection_titles"] = []

    return page_info


# =========================
# 语言检测
# =========================

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def detect_language(page_info: dict) -> Optional[str]:
    """返回语言代码（如 'zh-cn','ja','en'），无法判定时返回 None。

    优先级：html_lang 属性 > CJK 字符兜底 > langdetect 文本检测
    移植自 classify_shopify.py:124-154
    """
    html_lang = (page_info.get("html_lang") or "").lower().strip()
    if html_lang:
        return html_lang

    texts = [
        page_info.get("title", ""),
        page_info.get("meta_description", ""),
        page_info.get("shop_name", ""),
        " ".join(page_info.get("nav_categories", []) or []),
        " ".join(page_info.get("collection_titles", []) or []),
    ]
    text = " ".join(t for t in texts if t).strip()
    if len(text) < MIN_TEXT_LEN_FOR_DETECT:
        return None

    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    if cjk_count >= 3:
        kana_count = sum(1 for ch in text if 0x3040 <= ord(ch) <= 0x30FF)
        return "ja" if kana_count >= 2 else "zh"

    if HAS_LANGDETECT:
        try:
            return detect(text)
        except LangDetectException:
            return None
    return None


def is_non_english(page_info: dict) -> tuple[bool, Optional[str]]:
    """返回 (是否非英文, 语言代码)

    移植自 classify_shopify.py:157-164
    """
    lang = detect_language(page_info)
    if lang is None:
        return False, None
    if lang.startswith("en"):
        return False, lang
    return True, lang


# =========================
# 分类提示词
# =========================

def build_prompt(
    title: str,
    meta: str,
    nav: str,
    shop: str,
    url_kw: str,
    homepage_content: str = "",
    collection_titles: Optional[list] = None,
) -> str:
    """构造 LLM 分类提示词

    移植自 classify_shopify.py:172-229（21 个 Google 分类 + 黑五类 + 综合站）
    """
    live = ""
    if homepage_content:
        live = f'\n- Live homepage content: "{homepage_content}"'

    coll = ""
    if collection_titles:
        coll = f'\n- Collection names: [{", ".join(collection_titles)}]'

    return f"""Classify this Shopify store.

Store info:
- Title: "{title}"
- Description: "{meta}"
- Navigation menu items: [{nav}]
- Store name: "{shop}"
- URL path keywords: [{url_kw}]{coll}{live}

Categories to choose from (primary > allowed subcategories). The subcategory MUST be one of the items listed after ">" for the chosen primary category:

1. Apparel & Accessories > Clothing, Shoes, Jewelry, Handbags, Watches, Sunglasses
2. Electronics > Computers, Phones, Audio, Cameras, TVs, Gaming, Accessories
3. Home & Garden > Furniture, Decor, Kitchen, Bedding, Garden, Tools
4. Health & Beauty > Skincare, Makeup, Hair, Supplements, Personal Care, Fragrance
5. Food, Beverages & Tobacco > Coffee, Tea, Snacks, Wine, Beer, Grocery, Gourmet
6. Sporting Goods > Fitness, Outdoor, Cycling, Yoga, Camping, Hiking, Sports
7. Baby & Toddler > Clothing, Toys, Nursery, Strollers, Feeding, Maternity
8. Animals & Pet Supplies > Dog, Cat, Pet Food, Pet Accessories, Pet Care
9. Toys & Games > Board Games, Action Figures, Puzzles, Educational Toys
10. Business & Industrial > Office Supplies, Printing, Industrial Equipment
11. Media > Books, Movies, Music, Magazines, Digital Content
12. Arts & Entertainment > Art, Crafts, Party Supplies, collectibles,music instruments
13. Cameras & Optics > Cameras, Lenses, Binoculars, Photography Accessories
14. Furniture > Home Furniture, Office Furniture, Mattresses, Outdoor Furniture
15. Hardware > Tools, Hardware, Building Materials, Plumbing, Electrical
16. Luggage & Bags > Suitcases, Backpacks, Travel Bags, Wallets
17. Office Supplies > Paper, Pens, Office Equipment, Stationery
18. Software > Business Software, Education Software, Entertainment Software
19. Vehicles & Parts > Car Parts, Motorcycle Parts, Auto Accessories, Tires
20. Mature > Adult Toys, Lingerie, Adult Content, Adult Novelties, Adult Gifts
21. Religious & Ceremonial > Incense & Candles, Ritual Supplies, Worship Items, Ceremonial Objects, Religious Texts

IMPORTANT classification rules:
- The primary category MUST be one of the 21 specific categories listed above. Do NOT invent categories.
- First, determine if the store is a COMPREHENSIVE store (sells MULTIPLE unrelated categories, e.g. both electronics and clothing). If so, set category to "综合站" and list subcategories as an array.
- For a NON-comprehensive (specialty) store: choose the single best-matching primary category, and choose exactly ONE subcategory from the allowed list for that category. The subcategory MUST be copied VERBATIM from the list (including spaces, ampersands, and capitalization). Do NOT invent, translate, split, merge, or rephrase subcategories. Do NOT return multiple subcategories for a specialty store.
- If none of the listed subcategories fits, use "Other" as the subcategory.

ALSO check if this store belongs to a "black-five" (high-risk/prohibited) category:
- Weapons/Guns/Ammunition: firearms, ammunition, tactical gear, self-defense weapons
- Drugs/Controlled substances: CBD, kratom, vape, e-cigarettes, nicotine, delta-8, steroids, prescription drugs
- Black magic/Occult/Superstition: black magic, occult, spells, witchcraft, hex, curses
- Counterfeit/Piracy/Gray market: replicas, counterfeit goods, fake IDs, hacking tools, unlock services

NOTE: Legitimate religious/ceremonial supplies and adult products (Mature category) are NOT black-five. Only classify as black-five if the store explicitly involves black magic, witchcraft, or counterfeit/illegal goods.

Return ONLY valid JSON without markdown:
- Specialty store (single category, single subcategory): {{"category": "Primary", "subcategory": "Primary > Subcategory", "is_black_five": false}}
- Comprehensive store (multiple unrelated categories): {{"category": "综合站", "subcategories": ["Cat1 > Sub1", "Cat2 > Sub2"], "is_black_five": false}}
- Black-five store: {{"category": "黑五类", "subcategory": "黑五类 > <specific type>", "is_black_five": true, "black_five_type": "<type>"}}"""


# =========================
# LLM 分类
# =========================

def classify_store(
    page_info: dict,
    domain: str,
    http_client=None,
    debug: bool = False,
    site_db=None,
) -> dict:
    """调用 LLM 对站点分类

    Args:
        page_info: 页面信息 dict（title/meta_description/nav_categories/shop_name/url_keywords/collection_titles）
        domain: 店铺域名（page_info 信息不全时抓首页兜底）
        http_client: QMDS HttpClient 实例（走代理）
        debug: 是否打印调试信息
        site_db: 可选的 SiteDB 实例（Web 模式下传入，读取数据库中的 llm_model 配置）

    Returns:
        {
            "category": str,           # Google 一级分类名（如 "Hardware"）或 "黑五类"/"综合站"/"无法识别"
            "subcategory": str,         # 子分类（如 "Hardware > Tools"），综合站时为逗号分隔串
            "subcategories": list,      # 综合站时的二级分类列表（专一站为空列表）
            "is_filtered": bool,        # 是否应被过滤（黑五类）
            "is_comprehensive": bool,   # 是否为综合站
            "black_five_type": str,     # 黑五类具体类型（仅黑五类有）
            "source_subcategory": str,  # 原始 subcategory 路径
        }

    移植自 classify_shopify.py:232-298
    """
    title = page_info.get("title", "")
    meta = page_info.get("meta_description", "")
    nav = ", ".join(page_info.get("nav_categories", []) or [])
    shop = page_info.get("shop_name", "")
    url_kw = ", ".join(page_info.get("url_keywords", []) or [])
    collection_titles = page_info.get("collection_titles", [])

    # 优先使用 page_info 中已有的 homepage_content（阶段2 无网络请求）
    homepage_content = page_info.get("homepage_content", "")
    needs_fetch = (not meta or not nav) and not homepage_content
    if needs_fetch:
        if not domain:
            return _unrecognized()
        homepage_content = fetch_and_clean(domain, http_client)
        if not homepage_content:
            return _unrecognized()

    prompt = build_prompt(title, meta, nav, shop, url_kw, homepage_content, collection_titles)

    if not HAS_OPENAI:
        log.error("openai 未安装，无法分类")
        return _unrecognized()

    config = _resolve_runtime_config(site_db)
    if not has_llm_api_key(config, site_db):
        log.error(f"LLM API Key 未配置（provider={config['provider']}），无法分类")
        return _unrecognized()
    try:
        client = _get_glm_client(config, site_db)
    except RuntimeError as e:
        log.error(f"创建 LLM 客户端失败: {e}")
        return _unrecognized()

    last_err = ""
    for attempt in range(3):
        try:
            completion = chat_completion_with_fallback(
                client,
                config=config,
                messages=[
                    {"role": "system", "content": get_llm_system_message(config)},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_completion_tokens=800,
                top_p=0.95,
                timeout=30,
            )
            content = extract_llm_text(completion.choices[0].message)
            if not content:
                raise ValueError("LLM 返回空内容（思考 token 耗尽或模型无输出）")
            result = json.loads(content)

            if result.get("is_black_five"):
                bf_type = result.get("black_five_type") or "未分类"
                return {
                    "category": "黑五类",
                    "subcategory": f"黑五类 > {bf_type}",
                    "subcategories": [],
                    "is_filtered": True,
                    "is_comprehensive": False,
                    "black_five_type": bf_type,
                    "source_subcategory": f"黑五类 > {bf_type}",
                }

            cat = result.get("category", "")
            if cat == "综合站":
                subs = result.get("subcategories", [])
                sub_str = ", ".join(subs) if subs else ""
                return {
                    "category": "综合站",
                    "subcategory": sub_str,
                    "subcategories": subs,
                    "is_filtered": False,
                    "is_comprehensive": True,
                    "black_five_type": "",
                    "source_subcategory": sub_str,
                }

            sub = result.get("subcategory", "")
            if cat and sub:
                return {
                    "category": cat,
                    "subcategory": sub,
                    "subcategories": [],
                    "is_filtered": False,
                    "is_comprehensive": False,
                    "black_five_type": "",
                    "source_subcategory": sub,
                }
            if debug:
                log.debug(f"LLM raw (incomplete): {content[:200]}")
        except Exception as e:
            last_err = str(e)
            if debug:
                log.debug(f"attempt {attempt + 1}: {e}")
            if attempt < 2:
                if "429" in str(e):
                    time.sleep(10)
                else:
                    time.sleep(1)
    log.warning(f"分类失败 {domain}: {last_err}")
    return _unrecognized()


def _unrecognized() -> dict:
    """返回无法识别的分类结果"""
    return {
        "category": "无法识别",
        "subcategory": "无法识别",
        "subcategories": [],
        "is_filtered": False,
        "is_comprehensive": False,
        "black_five_type": "",
        "source_subcategory": "",
    }


def classify_batch(
    stores: list[dict],
    http_client=None,
    stop_check=None,
    progress_callback=None,
) -> list[dict]:
    """并发分类多个站点

    Args:
        stores: [{"domain": str, "url": str, "page_info"?: dict}, ...]
        http_client: QMDS HttpClient 实例
        stop_check: fn() -> bool，返回 True 时停止
        progress_callback: fn(processed, total, message)

    Returns:
        [{"domain": str, "url": str, "result": dict, "is_non_english": bool, "language": str?}, ...]
    """
    total = len(stores)
    if total == 0:
        return []

    results: list[dict] = [None] * total
    processed = 0
    batch_size = max(1, settings.ai_batch_size)

    def _classify_one(idx: int, store: dict) -> dict:
        domain = store.get("domain", "")
        url = store.get("url", f"https://{domain}" if domain else "")
        page_info = store.get("page_info") or {}

        non_en, lang = is_non_english(page_info)
        if non_en:
            result = _unrecognized()
            result["category"] = "非英文站"
            result["subcategory"] = f"非英文站 > {lang}"
        else:
            result = classify_store(page_info, domain, http_client)

        return {
            "domain": domain,
            "url": url,
            "result": result,
            "is_non_english": non_en,
            "language": lang,
        }

    with ThreadPoolExecutor(max_workers=batch_size) as pool:
        futures = {
            pool.submit(_classify_one, i, store): i
            for i, store in enumerate(stores)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                store = stores[idx]
                domain = store.get("domain", "")
                log.error(f"分类异常 {domain}: {e}")
                results[idx] = {
                    "domain": domain,
                    "url": store.get("url", ""),
                    "result": _unrecognized(),
                    "is_non_english": False,
                    "language": None,
                }
            processed += 1
            if progress_callback and (processed % 10 == 0 or processed == total):
                progress_callback(processed, total, f"AI 分类进度: {processed}/{total}")
            if stop_check and stop_check():
                for f in futures:
                    f.cancel()
                break

    return [r for r in results if r is not None]
