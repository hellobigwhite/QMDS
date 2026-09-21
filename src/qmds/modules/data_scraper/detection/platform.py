"""电商平台检测器（完全仿照 YSQD 实现）"""

import json
import random
import threading
import time
import urllib3
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from qmds.modules.data_scraper.models.schemas import Platform
from qmds.utils import cloudflare_client
from qmds.utils.logger import get_logger
from qmds.utils.proxy_manager import ProxyManager

log = get_logger("detection")

SCRAPERAPI_FETCH_URL = "https://api.scraperapi.com/"
CRAWLBASE_API_URL = "https://api.crawlbase.com/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
]

# Shopify 首页 HTML 强特征（仅用于 meta.json 失败后的兜底，避免误报不收录弱特征）
SHOPIFY_HTML_INDICATORS = [
    "cdn.shopify.",
    "/cdn/shop/",
    "window.shopify",
    "shopify.theme",
    "shopify_payments",
]

# Shopify 响应头特征
SHOPIFY_HEADER_INDICATORS = ("x-shopid", "x-sorting-hat-shopid")

# 通用电商指标（在排除 Shopify 之后才判定；shopify_payments 已移入 Shopify 正向特征）
GENERIC_ECOMMERCE_INDICATORS = [
    "js.stripe.com", "stripe.js", "paypal.com/sdk",
    "paypalobjects.com", "klarna.com", "squareup.com",
    "afterpay.com",
    '<meta name="generator" content="prestashop">', "opencart",
]


@dataclass
class DetectionResult:
    platform: Platform
    product_count: int = 0
    store_name: str = ""
    currency: str = "USD"
    confidence: float = 0.0
    raw: dict = None
    page_text: str = ""  # 存储页面文本用于语言检测
    inconclusive: bool = False  # True=网络失败/被拦截导致无法确认（区别于"确认非该平台"）

    def __bool__(self):
        return self.platform != Platform.UNKNOWN


def get_random_user_agent():
    return random.choice(USER_AGENTS)


def get_browser_headers():
    return {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


class ResponseAdapter:
    """适配 ScraperAPI / Crawlbase 的响应格式"""

    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = int(status_code)
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}: {self.text[:200]}")


def _load_json_maybe(value):
    if isinstance(value, (dict, list)):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def crawlbase_request(url, api_key, scraper=None, timeout=120):
    """通过 Crawlbase 代理获取 URL 内容"""
    if not api_key:
        raise ValueError("Crawlbase token is required")
    params = {"token": api_key, "url": url, "format": "json"}
    if scraper:
        params["scraper"] = scraper
    response = requests.get(CRAWLBASE_API_URL, params=params, timeout=timeout,
                            proxies={"http": None, "https": None})
    response.raise_for_status()
    payload = response.json()

    body = payload.get("body", payload)
    json_body = _load_json_maybe(body)
    if isinstance(body, str):
        text_body = body
    else:
        text_body = json.dumps(body, ensure_ascii=False)

    original_status = (payload.get("original_status") or payload.get("pc_status")
                       or response.headers.get("original_status") or response.headers.get("pc_status")
                       or response.status_code)
    try:
        original_status = int(str(original_status))
    except Exception:
        original_status = response.status_code

    return ResponseAdapter(
        status_code=original_status,
        text=text_body,
        json_data=json_body,
        headers=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
    )


def request_with_mode(url, proxy_manager=None, method="GET", headers=None, timeout=30, **kwargs):
    """按代理模式发送请求"""
    headers = headers or {}
    proxy = proxy_manager.get_proxy() if proxy_manager else None

    if method.upper() == "GET":
        return requests.get(url, headers=headers, timeout=timeout, proxies=proxy, **kwargs)
    return requests.post(url, headers=headers, timeout=timeout, proxies=proxy, **kwargs)


def _request_with_retry(url, proxy_manager=None, headers=None, timeout=15, max_retries=2):
    """带重试的请求，失败时标记代理并轮换"""
    for attempt in range(max_retries):
        proxy = proxy_manager.get_proxy() if proxy_manager else None
        try:
            response = request_with_mode(url, proxy_manager=proxy_manager, method="GET",
                                         headers=headers, timeout=timeout)
            if response.status_code != 0:
                return response
        except requests.exceptions.SSLError:
            try:
                response = request_with_mode(url, proxy_manager=proxy_manager, method="GET",
                                             headers=headers, timeout=timeout, verify=False)
                if response.status_code != 0:
                    return response
            except Exception:
                if proxy and proxy_manager:
                    proxy_manager.mark_bad(proxy)
        except Exception:
            if proxy and proxy_manager:
                proxy_manager.mark_bad(proxy)
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
    return None


def _is_retryable_status(status_code: int) -> bool:
    """403/429/5xx 表示被拦截或临时故障，不能作为"非该平台"的否定证据，应复检"""
    return status_code in (403, 429) or status_code >= 500


def _is_bare_version_text(text: str) -> bool:
    """/static/version 应返回裸版本号；catch-all 重定向返回整页 HTML，不能据此判定 Magento"""
    t = (text or "").strip()
    if not t or len(t) > 64:
        return False
    lowered = t.lower()
    return not lowered.startswith(("<!doctype", "<html"))


class PlatformDetector:
    """电商平台检测器（完全仿照 YSQD）"""

    def __init__(self, proxy_manager: Optional[ProxyManager] = None):
        self._proxy_manager = proxy_manager
        # 商品数据爬取使用的代理服务客户端（懒加载，meta.json 被 403 拦截时复检用）
        self._proxy_service = None
        self._proxy_service_failed = False
        # cloudscraper 复检开关（Cloudflare JS 挑战兜底；测试中可关闭以避免真实请求）
        self.cloudflare_fallback_enabled = True
        # 同域名互斥锁：避免并发检测轰击同一站点触发限流
        self._domain_locks = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _get_domain_lock(self, domain: str) -> threading.Lock:
        with self._locks_guard:
            return self._domain_locks[domain]

    def _get_proxy_service(self):
        """懒加载代理服务客户端（与 ProductCrawler 共用同一服务），失败后永久停用"""
        if self._proxy_service_failed:
            return None
        if self._proxy_service is None:
            try:
                from qmds.modules.data_scraper.product_crawler import ProxyServiceClient
                # 平台检测关闭熔断：每个 URL 都真实请求远程代理，
                # 避免连续失败进入冷却后把真实 Shopify 站点漏判。
                self._proxy_service = ProxyServiceClient(breaker_enabled=False)
            except Exception as e:
                log.warning(f"代理服务客户端初始化失败，已停用: {e}")
                self._proxy_service_failed = True
                return None
        return self._proxy_service

    @staticmethod
    def _shopify_result_from_meta(data: dict, confidence: float) -> DetectionResult:
        """由 meta.json 内容构建 Shopify 检测结果"""
        return DetectionResult(
            platform=Platform.SHOPIFY,
            product_count=int(data.get("published_products_count", 0) or 0),
            store_name=data.get("name", ""),
            currency=data.get("currency", "USD"),
            confidence=confidence,
            raw=data,
        )

    def _detect_shopify_via_proxy_service(self, meta_url: str) -> Tuple[Optional[DetectionResult], int]:
        """通过远程代理服务直接访问 meta.json（Shopify 判定首选路径）

        返回 (result, status)：
        - result 非 None：远程代理确认是 Shopify；
        - status 为明确的 HTTP 码（404/403/5xx 等）：meta.json 不可访问 → 非 Shopify；
        - status 为 0（超时）/ -1（熔断跳过）/ 客户端不可用：无法确定，
          调用方应退回本地直连兜底，避免服务抖动时漏判真实 Shopify 站点。
        """
        client = self._get_proxy_service()
        if not client:
            # STATUS_SKIPPED = -1
            return None, -1
        log.info(f"远程代理服务访问 meta.json: {meta_url}")
        # 检测是高频批量场景，用固定 30s 超时避免单个站点拖慢整个批次
        data, status = client.fetch(meta_url, timeout=30)
        if status == 200 and isinstance(data, dict) and "published_products_count" in data:
            log.info(f"远程代理服务确认 Shopify: {meta_url}")
            return self._shopify_result_from_meta(data, confidence=0.95), status
        return None, status

    def _detect_shopify_via_cloudscraper(self, meta_url: str) -> Optional[DetectionResult]:
        """meta.json 被 Cloudflare 挑战拦截时，用 cloudscraper 解挑战后复检"""
        if not self.cloudflare_fallback_enabled or not cloudflare_client.is_available():
            return None
        response = cloudflare_client.get(meta_url, timeout=20)
        if response is None or response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict) and "published_products_count" in data:
            log.info(f"cloudscraper 复检成功确认 Shopify: {meta_url}")
            return self._shopify_result_from_meta(data, confidence=0.95)
        return None

    def _fetch_homepage_via_cloudscraper(self, url: str):
        """首页被拦截/请求失败时用 cloudscraper 重取（解 Cloudflare JS 挑战）"""
        if not self.cloudflare_fallback_enabled or not cloudflare_client.is_available():
            return None
        return cloudflare_client.get(url, timeout=20)

    @staticmethod
    def _matches_shopify_fingerprint(html: str, response=None) -> bool:
        """首页 HTML / 响应头的 Shopify 特征匹配（第二判据）"""
        if response is not None:
            try:
                headers = {(k or "").lower(): str(v) for k, v in dict(response.headers or {}).items()}
                if any(name in headers for name in SHOPIFY_HEADER_INDICATORS):
                    return True
            except Exception:
                pass
        if not html:
            return False
        lowered = html.lower()
        return any(indicator in lowered for indicator in SHOPIFY_HTML_INDICATORS)

    def _fetch_page_text(self, url: str, headers: dict) -> str:
        """获取页面 HTML 用于语言检测等后续处理"""
        try:
            response = _request_with_retry(url, proxy_manager=self._proxy_manager,
                                           headers=headers, timeout=15)
            if response and response.status_code == 200:
                return response.text
        except Exception:
            pass
        return ""

    def detect(self, url: str, url_map: dict = None) -> DetectionResult:
        """检测电商平台（与 YSQD detect_ecommerce_platform 一致）"""
        url_map = url_map or {}
        try:
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            if not url.endswith("/"):
                url += "/"

            domain = urlparse(url).netloc.lower()
            with self._get_domain_lock(domain):
                return self._detect_locked(url, url_map)
        except Exception:
            # 未预见异常不构成平台否定证据，标记 inconclusive 交由上层复检
            return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)

    def _detect_locked(self, url: str, url_map: dict) -> DetectionResult:
        headers = get_browser_headers()
        network_failure = False
        meta_json_blocked = False
        meta_url = f"{url}meta.json"

        # 1. Shopify 判定：直接使用远程代理服务访问 meta.json（能访问即 Shopify）。
        #    远程代理可绕过 Cloudflare / 地域拦截，命中率远高于本地直连，
        #    因此作为首选，而不再是"本地失败后的复检"。
        remote_result, remote_status = self._detect_shopify_via_proxy_service(meta_url)
        if remote_result is not None:
            remote_result.page_text = self._fetch_page_text(url, headers)
            return remote_result

        # 2. 远程代理"无法确定"（超时=0 / 熔断跳过=-1 / 客户端不可用）时：
        #    本地直连逐项检测兜底（Shopify 仍以 meta.json 为准，
        #    另测 WooCommerce / Magento / BigCommerce）。
        #    远程代理明确返回 4xx/5xx 等 HTTP 码时，其判定即为最终结果
        #    （meta.json 无法访问 → 非 Shopify），不再重复本地检测，
        #    避免每个非 Shopify 站点多花一轮直连请求。
        if remote_status in (0, -1):
            checks = [
                ("Shopify", meta_url, lambda r: "published_products_count" in r.json(), 20, 3),
                ("WooCommerce", f"{url}wp-json/wc/v3/products?per_page=1", lambda r: isinstance(r.json(), list), 15, 2),
                ("Magento", f"{url}magento_version", lambda r: "Magento" in r.text, 15, 2),
                ("Magento", f"{url}static/version", lambda r: _is_bare_version_text(r.text), 15, 2),
                ("BigCommerce", url, lambda r: "BigCommerce" in r.text, 15, 2),
            ]

            for platform_name, check_url, predicate, timeout, retries in checks:
                try:
                    response = _request_with_retry(check_url, proxy_manager=self._proxy_manager,
                                                   headers=headers, timeout=timeout, max_retries=retries)
                except Exception:
                    response = None
                if response is None:
                    if platform_name == "Shopify":
                        network_failure = True
                    continue
                try:
                    matched = response.status_code == 200 and predicate(response)
                except Exception:
                    # 200 但内容非预期（如返回 HTML 密码页导致 JSON 解析失败）
                    matched = False
                if matched:
                    result = self._to_result(platform_name, url, response)
                    result.page_text = self._fetch_page_text(url, headers)
                    return result
                if platform_name == "Shopify" and _is_retryable_status(response.status_code):
                    meta_json_blocked = True

            # 3. meta.json 被本地拦截（403/429/5xx）→ cloudscraper 复检
            if meta_json_blocked:
                result = self._detect_shopify_via_cloudscraper(meta_url)
                if result:
                    result.page_text = self._fetch_page_text(url, headers)
                    return result

        # 4. Shopify 只以 meta.json 为判断标准。
        # 不使用首页 HTML、响应头等弱特征，避免把非 Shopify 站点误判为 Shopify。
        page_text = ""

        # 5. myshopify URL 回退（同样优先走远程代理服务，失败再本地直连）
        myshopify_url = url_map.get(url.rstrip("/"))
        if myshopify_url:
            if not myshopify_url.endswith("/"):
                myshopify_url += "/"
            myshopify_meta_url = f"{myshopify_url}meta.json"
            myshopify_result, _ = self._detect_shopify_via_proxy_service(myshopify_meta_url)
            if myshopify_result is None:
                try:
                    response = _request_with_retry(myshopify_meta_url, proxy_manager=self._proxy_manager,
                                                   headers=headers, timeout=15)
                    if response and response.status_code == 200 and "published_products_count" in response.json():
                        myshopify_result = self._to_result("Shopify", myshopify_url, response)
                except Exception:
                    pass
            if myshopify_result:
                myshopify_result.page_text = self._fetch_page_text(myshopify_url, headers)
                return myshopify_result

        # 6. 通用电商指标（已排除 Shopify 后才判定；命中仅说明是其他电商平台）
        if page_text:
            html_content = page_text.lower()
            if any(indicator in html_content for indicator in GENERIC_ECOMMERCE_INDICATORS):
                return DetectionResult(platform=Platform.UNKNOWN, confidence=0.3)

        return DetectionResult(platform=Platform.UNKNOWN,
                               inconclusive=network_failure or meta_json_blocked)

    def _to_result(self, platform_name: str, url: str, response) -> DetectionResult:
        """将平台名称和响应转换为 DetectionResult"""
        if platform_name == "Shopify":
            try:
                return self._shopify_result_from_meta(response.json(), confidence=1.0)
            except Exception:
                return DetectionResult(platform=Platform.SHOPIFY, confidence=0.9)
        elif platform_name == "WooCommerce":
            return DetectionResult(platform=Platform.WOOCOMMERCE, confidence=0.9)
        elif platform_name == "Magento":
            return DetectionResult(platform=Platform.MAGENTO, confidence=0.8)
        elif platform_name == "BigCommerce":
            return DetectionResult(platform=Platform.BIGCOMMERCE, confidence=0.7)
        return DetectionResult(platform=Platform.UNKNOWN)
