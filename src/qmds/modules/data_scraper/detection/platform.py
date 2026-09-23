"""电商平台检测器 — 分层漏斗式检测（快速、简洁、防误杀）

检测顺序按"成本从低到高"分层，任一层命中即返回：

    0. DNS 预检    域名解析进 Shopify 边缘 IP 段（23.227.38.0/24）
                   → 零请求、零成本、不可伪造的平台级证据
    1. 直连 meta.json    200 + JSON 即 Shopify
    2. 远程代理 meta.json 仅直连被拦时使用；502/403/429 一律视为
                         "无法确定"，绝不作为否定证据（防误杀真店）
    3. DNS 命中兜底      域名指向 Shopify 边缘 → 收录（confidence 0.85）
    4. 首页强指纹        响应头 x-shopid / HTML 含 cdn.shopify. / /cdn/shop/
                         → Shopify（0.9，headless 店也在此层被抓住）
    5. cloudscraper      解 Cloudflare JS 挑战后复检
    6. myshopify 回退    url_map 提供的 myshopify 域名

判定语义：
    - "确认非 Shopify" 只有唯一出口：直连首页 200、非挑战页、
      无任何 Shopify 强指纹。
    - meta.json 404 不是否定证据：headless Shopify（如 gymshark.com）
      会移除 meta.json，但页面仍引用平台 CDN。
    - 拦截/网关错误（403/429/5xx/超时/挑战页）一律 inconclusive，
      交由上层二轮复检，防止把被拦的真店永久误杀。
"""

import ipaddress
import json
import random
import socket
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

# ── 检测通道配置 ──────────────────────────────────────────
# Shopify 边缘节点 IP 段（官方公布）：自定义域名 A 记录指向这里。
# DNS 命中是平台级强证据（零请求、零成本、不可伪造）；
# Cloudflare 前置的店铺解析到 CF IP 会漏检，因此只作正向信号、无假阳性。
SHOPIFY_IP_NETWORKS = [ipaddress.ip_network("23.227.38.0/24")]

# Shopify 首页强指纹：命中任一即可独立确认 Shopify。
# Shopify 主题/资产必然引用平台 CDN；弱特征（window.shopify 等）不收录，防误报。
SHOPIFY_STRONG_HTML_INDICATORS = ("cdn.shopify.", "/cdn/shop/")

# Shopify 响应头特征（平台自动附加）
SHOPIFY_HEADER_INDICATORS = ("x-shopid", "x-sorting-hat-shopid")

# Cloudflare 挑战/拦截页特征（命中即视为"被拦截"，不作否定证据）
CHALLENGE_MARKERS = (
    "just a moment", "challenge-platform", "cf-chl",
    "enable javascript and cookies", "attention required",
)

# 检测超时（秒）：批量吞吐优先，远低于旧版的 15-30s
DETECT_TIMEOUT_DIRECT = 10   # 直连 meta.json / 首页
DETECT_TIMEOUT_PROXY = 15    # 远程代理服务 meta.json
DETECT_TIMEOUT_OTHER = 8     # 其他平台（Woo/Magento/BigCommerce）


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


def _looks_like_challenge(text: str) -> bool:
    """响应体是否是 Cloudflare 挑战/拦截页（命中即视为被拦截，不作否定证据）"""
    if not text:
        return False
    head = text[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


class PlatformDetector:
    """电商平台检测器（完全仿照 YSQD）"""

    def __init__(self, proxy_manager: Optional[ProxyManager] = None):
        self._proxy_manager = proxy_manager
        # 商品数据爬取使用的代理服务客户端（懒加载，meta.json 被 403 拦截时复检用）
        self._proxy_service = None
        self._proxy_service_failed = False
        # cloudscraper 复检开关（Cloudflare JS 挑战兜底；测试中可关闭以避免真实请求）
        self.cloudflare_fallback_enabled = True
        # DNS 预检开关（Shopify IP 段匹配；测试中可关闭以避免真实解析）
        self.dns_check_enabled = True
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
        # 检测是高频批量场景，用短超时避免单个站点拖慢整个批次
        data, status = client.fetch(meta_url, timeout=DETECT_TIMEOUT_PROXY)
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
    def _meta_from_response(response) -> Optional[dict]:
        """从 meta.json 响应提取店铺信息；非 200 / 非 JSON / 缺关键字段返回 None"""
        if response is None or response.status_code != 200:
            return None
        try:
            data = response.json()
        except Exception:
            return None
        if isinstance(data, dict) and "published_products_count" in data:
            return data
        return None

    @staticmethod
    def _strong_shopify_fingerprint(response) -> bool:
        """首页强指纹：响应头 x-shopid 系 或 HTML 含平台 CDN 引用（可独立确认）"""
        if response is None:
            return False
        try:
            headers = {(k or "").lower(): str(v) for k, v in dict(response.headers or {}).items()}
            if any(name in headers for name in SHOPIFY_HEADER_INDICATORS):
                return True
        except Exception:
            pass
        text = getattr(response, "text", "") or ""
        if not text:
            return False
        lowered = text.lower()
        return any(indicator in lowered for indicator in SHOPIFY_STRONG_HTML_INDICATORS)

    def _dns_points_to_shopify(self, domain: str) -> bool:
        """域名是否解析到 Shopify 边缘 IP 段（23.227.38.0/24）

        自定义域名 A 记录指向 Shopify 是平台级强证据；
        Cloudflare 前置的店铺解析到 CF IP 会漏检（无假阳性，只作正向信号）。
        """
        host = domain.split("@")[-1].split(":")[0].strip("[]").strip()
        if not host:
            return False
        try:
            infos = socket.getaddrinfo(host, 443)
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if any(ip in net for net in SHOPIFY_IP_NETWORKS):
                return True
        return False

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
        """分层漏斗检测（顺序与语义见模块 docstring）

        blocked 汇总一切"无法确定"信号（拦截/网关错误/超时/挑战页），
        最终以 inconclusive 返回，交由上层二轮复检，绝不误杀真店。

        trace 记录每层通道的结论，在"非 Shopify / 其他平台 / 无法确认"
        出口输出日志，便于从日志直接定位每个站是在哪一步被判定的。
        """
        headers = get_browser_headers()
        domain = urlparse(url).netloc.lower()
        meta_url = f"{url}meta.json"
        blocked = False
        trace: list[str] = []  # 如 "直连meta:HTTP404无此端点 → 首页:200无Shopify指纹"

        # ── 0. DNS 预检：域名是否指向 Shopify 边缘（零请求、零成本）──
        dns_shopify = self._dns_points_to_shopify(domain) if self.dns_check_enabled else False

        # ── 1. 直连 meta.json（多数站一次请求即出结论）──────────
        response = _request_with_retry(meta_url, proxy_manager=self._proxy_manager,
                                       headers=headers, timeout=DETECT_TIMEOUT_DIRECT, max_retries=2)
        meta_data = self._meta_from_response(response)
        if meta_data is not None:
            log.info(f"直连 meta.json 确认 Shopify: {url}")
            return self._shopify_result_from_meta(meta_data, confidence=1.0)

        status = response.status_code if response is not None else 0
        meta_absent = False  # 源站可达且明确无 meta.json（404/410/200 非 JSON）
        if status == 0:
            blocked = True
            trace.append("直连meta:无响应")
        elif _is_retryable_status(status):
            blocked = True
            trace.append(f"直连meta:HTTP{status}被拦")
        elif status in (404, 410):
            # headless Shopify（如 gymshark.com）会移除 meta.json，
            # 不能据此否定，继续走首页指纹
            meta_absent = True
            trace.append(f"直连meta:HTTP{status}无此端点")
        elif status == 200:
            if _looks_like_challenge(getattr(response, "text", "") or ""):
                blocked = True
                trace.append("直连meta:200挑战页")
            else:
                meta_absent = True  # 200 但内容不是 meta.json
                trace.append("直连meta:200非JSON")
        else:
            blocked = True
            trace.append(f"直连meta:HTTP{status}")

        # ── 2. 远程代理服务 meta.json（仅直连未出结论时）─────────
        #    502/403/429/5xx/0/-1/200 无内容：代理侧或拦截信号，
        #    一律视为"无法确定"，绝不作为否定证据（防误杀真店）
        if not meta_absent:
            remote_result, remote_status = self._detect_shopify_via_proxy_service(meta_url)
            if remote_result is not None:
                return remote_result
            if remote_status in (404, 410):
                meta_absent = True
                trace.append(f"代理meta:HTTP{remote_status}无此端点")
            else:
                blocked = True
                if remote_status == 0:
                    trace.append("代理meta:超时")
                elif remote_status == -1:
                    trace.append("代理meta:不可用")
                elif remote_status == 200:
                    trace.append("代理meta:200无内容")
                else:
                    trace.append(f"代理meta:HTTP{remote_status}")
        else:
            trace.append("代理meta:跳过(源站已无meta)")

        # ── 3. DNS 命中兜底：域名指向 Shopify 边缘，收录 ─────────
        if dns_shopify:
            log.info(f"DNS 命中 Shopify 边缘 IP，收录: {domain}")
            return DetectionResult(platform=Platform.SHOPIFY, confidence=0.85)

        # ── 4. 首页强指纹（响应头 x-shopid / cdn.shopify. / /cdn/shop/）──
        #    headless Shopify 移除了 meta.json，但页面通常仍引用平台 CDN
        homepage = _request_with_retry(url, proxy_manager=self._proxy_manager,
                                       headers=headers, timeout=DETECT_TIMEOUT_DIRECT, max_retries=1)
        if homepage is not None:
            hp_text = getattr(homepage, "text", "") or ""
            hp_status = homepage.status_code
            if self._strong_shopify_fingerprint(homepage):
                log.info(f"首页强指纹确认 Shopify: {url}")
                return DetectionResult(platform=Platform.SHOPIFY, confidence=0.9, page_text=hp_text)
            if hp_status == 200 and not _looks_like_challenge(hp_text):
                # 唯一的确定性否定出口：直连干净首页、无任何 Shopify 强指纹；
                # 顺带识别其他平台（WooCommerce / Magento / BigCommerce）
                other = self._detect_other_platforms(url, headers)
                if other is not None:
                    trace.append(f"其他平台:{other.platform.value}")
                    log.info(f"判定为 {other.platform.value}（非 Shopify）: {url} | 通道: {' → '.join(trace)}")
                    return other
                trace.append("首页:200无Shopify指纹")
                log.info(f"判定非 Shopify: {url} | 通道: {' → '.join(trace)}")
                return DetectionResult(platform=Platform.UNKNOWN, page_text=hp_text)
            blocked = True
            if hp_status == 0:
                trace.append("首页:无响应")
            elif _is_retryable_status(hp_status):
                trace.append(f"首页:HTTP{hp_status}被拦")
            else:
                trace.append(f"首页:HTTP{hp_status}")
        else:
            blocked = True
            trace.append("首页:无响应")

        # ── 5. cloudscraper 兜底（Cloudflare JS 挑战）────────────
        result = self._detect_shopify_via_cloudscraper(meta_url)
        if result is not None:
            return result
        page = self._fetch_homepage_via_cloudscraper(url)
        if page is not None and self._strong_shopify_fingerprint(page):
            log.info(f"cloudscraper 首页指纹确认 Shopify: {url}")
            return DetectionResult(platform=Platform.SHOPIFY, confidence=0.9,
                                   page_text=getattr(page, "text", "") or "")
        trace.append("cloudscraper:未确认")

        # ── 6. myshopify URL 回退（url_map 提供的 myshopify 域名）──
        myshopify_url = url_map.get(url.rstrip("/"))
        if myshopify_url:
            if not myshopify_url.endswith("/"):
                myshopify_url += "/"
            myshopify_meta_url = f"{myshopify_url}meta.json"
            myshopify_result, _ = self._detect_shopify_via_proxy_service(myshopify_meta_url)
            if myshopify_result is None:
                try:
                    response = _request_with_retry(myshopify_meta_url, proxy_manager=self._proxy_manager,
                                                   headers=headers, timeout=DETECT_TIMEOUT_DIRECT)
                    meta_data = self._meta_from_response(response)
                    if meta_data is not None:
                        myshopify_result = self._shopify_result_from_meta(meta_data, confidence=1.0)
                except Exception:
                    pass
            if myshopify_result is not None:
                return myshopify_result
            trace.append("myshopify回退:未确认")

        log.info(f"无法确认是否 Shopify（交二轮复检）: {url} | 通道: {' → '.join(trace)}")
        return DetectionResult(platform=Platform.UNKNOWN, inconclusive=blocked)

    def _detect_other_platforms(self, url: str, headers: dict) -> Optional[DetectionResult]:
        """已确认非 Shopify 后的其他平台识别（短超时，仅供信息标注）"""
        checks = [
            ("WooCommerce", f"{url}wp-json/wc/v3/products?per_page=1", lambda r: isinstance(r.json(), list)),
            ("Magento", f"{url}magento_version", lambda r: "Magento" in r.text),
            ("Magento", f"{url}static/version", lambda r: _is_bare_version_text(r.text)),
            ("BigCommerce", url, lambda r: "BigCommerce" in r.text),
        ]
        for platform_name, check_url, predicate in checks:
            try:
                response = _request_with_retry(check_url, proxy_manager=self._proxy_manager,
                                               headers=headers, timeout=DETECT_TIMEOUT_OTHER, max_retries=1)
            except Exception:
                response = None
            if response is None:
                continue
            try:
                matched = response.status_code == 200 and predicate(response)
            except Exception:
                matched = False
            if matched:
                return self._to_result(platform_name, url, response)
        return None

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
