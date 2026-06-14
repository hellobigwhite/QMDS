"""电商平台检测器（使用 ScraperAPI 代理模式）"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from qmds.config.search_providers import SearchManager
from qmds.modules.data_scraper.models.schemas import Platform
from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger

log = get_logger("detection")

SCRAPERAPI_FETCH_URL = "http://api.scraperapi.com/"


@dataclass
class DetectionResult:
    platform: Platform
    product_count: int = 0
    store_name: str = ""
    currency: str = "USD"
    confidence: float = 0.0
    raw: dict = None

    def __bool__(self):
        return self.platform != Platform.UNKNOWN


SHOPIFY_META_PATH = "/meta.json"
WOOCOMMERCE_API_PATH = "/wp-json/wc/v3/products?per_page=1"
MAGENTO_PATHS = ["/magento_version", "/static/version"]


def _get_scraperapi_key() -> Optional[str]:
    """从 SearchManager 获取一个可用的 ScraperAPI key"""
    from qmds.modules.data_scraper.discovery.google_search import _get_search_manager
    manager = _get_search_manager()
    for p in manager._providers:
        if p.name == "scraperapi" and p.is_available():
            return p.key_pool.get_key()
    return None


class PlatformDetector:
    """检测目标站点使用的电商平台"""

    def __init__(self, http_client: Optional[HttpClient] = None):
        self.http = http_client or HttpClient()

    def _fetch(self, url: str, timeout: int = 30) -> Optional[object]:
        """通过 ScraperAPI 代理模式获取 URL 内容"""
        api_key = _get_scraperapi_key()
        if not api_key:
            log.debug("ScraperAPI key 不可用，跳过平台检测")
            return None
        try:
            params = {"api_key": api_key, "url": url}
            resp = self.http.get(SCRAPERAPI_FETCH_URL, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log.debug(f"ScraperAPI 返回 {resp.status_code} for {url}")
            return None
        except Exception as e:
            log.debug(f"ScraperAPI 请求失败 {url}: {e}")
            return None

    def detect(self, url: str) -> DetectionResult:
        domain = urlparse(url).netloc or urlparse(url).path
        domain = domain.strip("/")

        result = self._check_shopify(domain)
        if result:
            return result

        return DetectionResult(platform=Platform.UNKNOWN)

    def _check_shopify(self, domain: str) -> Optional[DetectionResult]:
        try:
            resp = self._fetch(f"https://{domain}{SHOPIFY_META_PATH}")
            if resp:
                data = resp.json()
                count = data.get("published_products_count", 0)
                if count is not None:
                    return DetectionResult(
                        platform=Platform.SHOPIFY,
                        product_count=int(count),
                        store_name=data.get("name", ""),
                        currency=data.get("currency", "USD"),
                        confidence=1.0,
                        raw=data,
                    )
        except Exception:
            pass
        return None

    def _check_woocommerce(self, domain: str) -> Optional[DetectionResult]:
        try:
            resp = self._fetch(f"https://{domain}{WOOCOMMERCE_API_PATH}")
            if resp and resp.status_code == 200:
                return DetectionResult(platform=Platform.WOOCOMMERCE, confidence=0.9)
        except Exception:
            pass
        return None

    def _check_magento(self, domain: str) -> Optional[DetectionResult]:
        for path in MAGENTO_PATHS:
            try:
                resp = self._fetch(f"https://{domain}{path}")
                if resp and resp.status_code == 200:
                    return DetectionResult(platform=Platform.MAGENTO, confidence=0.8)
            except Exception:
                continue
        return None

    def _check_bigcommerce(self, domain: str) -> Optional[DetectionResult]:
        try:
            resp = self._fetch(f"https://{domain}/")
            if resp and "BigCommerce" in resp.text:
                return DetectionResult(platform=Platform.BIGCOMMERCE, confidence=0.7)
        except Exception:
            pass
        return None
