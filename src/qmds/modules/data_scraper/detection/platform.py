"""电商平台检测器（完全仿照 YSQD 实现）"""

import json
import random
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from qmds.modules.data_scraper.models.schemas import Platform
from qmds.utils.logger import get_logger

log = get_logger("detection")

SCRAPERAPI_FETCH_URL = "https://api.scraperapi.com/"
CRAWLBASE_API_URL = "https://api.crawlbase.com/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
]


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


def request_with_mode(url, api_key=None, method="GET", headers=None, timeout=30, **kwargs):
    """按 API 模式发送请求（与 YSQD 一致）"""
    headers = headers or {}

    # ScraperAPI 模式
    if api_key:
        params = {"api_key": api_key, "url": url, "keep_headers": "true"}
        if method.upper() == "GET":
            return requests.get(SCRAPERAPI_FETCH_URL, params=params, headers=headers,
                                timeout=timeout, proxies={"http": None, "https": None}, **kwargs)
        return requests.post(SCRAPERAPI_FETCH_URL, params=params, headers=headers,
                             timeout=timeout, proxies={"http": None, "https": None}, **kwargs)

    # 直连模式（走系统代理或无代理）
    if method.upper() == "GET":
        return requests.get(url, headers=headers, timeout=timeout, **kwargs)
    return requests.post(url, headers=headers, timeout=timeout, **kwargs)


def _request_with_retry(url, api_key=None, headers=None, timeout=15, max_retries=2):
    """带重试的请求（与 YSQD 一致）"""
    for attempt in range(max_retries):
        try:
            response = request_with_mode(url, api_key=api_key, method="GET",
                                         headers=headers, timeout=timeout)
            if response.status_code != 0:
                return response
        except requests.exceptions.SSLError:
            try:
                response = request_with_mode(url, api_key=api_key, method="GET",
                                             headers=headers, timeout=timeout, verify=False)
                if response.status_code != 0:
                    return response
            except Exception:
                pass
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
    return None


class PlatformDetector:
    """电商平台检测器（完全仿照 YSQD）"""

    def __init__(self, scraperapi_key: str = ""):
        self._api_key = scraperapi_key

    def detect(self, url: str, url_map: dict = None) -> DetectionResult:
        """检测电商平台（与 YSQD detect_ecommerce_platform 一致）"""
        url_map = url_map or {}
        try:
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            if not url.endswith("/"):
                url += "/"

            headers = get_browser_headers()

            # 1. Shopify / WooCommerce / Magento / BigCommerce 检测
            checks = [
                ("Shopify", f"{url}meta.json", lambda r: "published_products_count" in r.json()),
                ("WooCommerce", f"{url}wp-json/wc/v3/products?per_page=1", lambda r: isinstance(r.json(), list)),
                ("Magento", f"{url}magento_version", lambda r: "Magento" in r.text),
                ("Magento", f"{url}static/version", lambda r: r.status_code == 200),
                ("BigCommerce", url, lambda r: "BigCommerce" in r.text),
            ]

            for platform_name, check_url, predicate in checks:
                try:
                    response = _request_with_retry(check_url, api_key=self._api_key,
                                                   headers=headers, timeout=15)
                    if response and response.status_code == 200 and predicate(response):
                        return self._to_result(platform_name, url, response)
                except Exception:
                    pass

            # 2. 通用电商指标（Stripe/PayPal/Klarna 等）
            try:
                response = _request_with_retry(url, api_key=self._api_key,
                                               headers=headers, timeout=15)
                if response and response.status_code == 200:
                    html_content = response.text.lower()
                    indicators = [
                        "js.stripe.com", "stripe.js", "paypal.com/sdk",
                        "paypalobjects.com", "klarna.com", "squareup.com",
                        "shopify_payments", "afterpay.com",
                        '<meta name="generator" content="prestashop">', "opencart",
                    ]
                    if any(indicator in html_content for indicator in indicators):
                        return DetectionResult(platform=Platform.UNKNOWN, confidence=0.3)
            except Exception:
                pass

            # 3. myshopify URL 回退
            myshopify_url = url_map.get(url.rstrip("/"))
            if myshopify_url:
                if not myshopify_url.endswith("/"):
                    myshopify_url += "/"
                try:
                    response = _request_with_retry(f"{myshopify_url}meta.json", api_key=self._api_key,
                                                   headers=headers, timeout=15)
                    if response and response.status_code == 200 and "published_products_count" in response.json():
                        return self._to_result("Shopify", myshopify_url, response)
                except Exception:
                    pass

            return DetectionResult(platform=Platform.UNKNOWN)
        except Exception:
            return DetectionResult(platform=Platform.UNKNOWN)

    def _to_result(self, platform_name: str, url: str, response) -> DetectionResult:
        """将平台名称和响应转换为 DetectionResult"""
        if platform_name == "Shopify":
            try:
                data = response.json()
                return DetectionResult(
                    platform=Platform.SHOPIFY,
                    product_count=int(data.get("published_products_count", 0)),
                    store_name=data.get("name", ""),
                    currency=data.get("currency", "USD"),
                    confidence=1.0,
                    raw=data,
                )
            except Exception:
                return DetectionResult(platform=Platform.SHOPIFY, confidence=0.9)
        elif platform_name == "WooCommerce":
            return DetectionResult(platform=Platform.WOOCOMMERCE, confidence=0.9)
        elif platform_name == "Magento":
            return DetectionResult(platform=Platform.MAGENTO, confidence=0.8)
        elif platform_name == "BigCommerce":
            return DetectionResult(platform=Platform.BIGCOMMERCE, confidence=0.7)
        return DetectionResult(platform=Platform.UNKNOWN)
