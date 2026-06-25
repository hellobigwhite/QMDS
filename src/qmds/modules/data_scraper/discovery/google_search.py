"""通过 Google 搜索发现 Shopify 店铺（多 API 自动切换）"""

import threading
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse

from qmds.config.search_providers import SearchManager, ScrapeProviderError
from qmds.core.base import BaseScraper, ScrapeResult
from qmds.core.exceptions import ScrapeError
from qmds.utils.logger import get_logger

log = get_logger("google_search")

SHOPIFY_QUERIES = [
    'inurl:collections/all',
    'inurl:products "shopify"',
    'site:myshopify.com',
]


# ── URL 清洗工具 ──────────────────────────────────────────

def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            return ""
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.lower()
    except Exception:
        return ""


def clean_url(url: str) -> tuple[str, Optional[str]]:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            return "", None
        myshopify_url = None
        if ".myshopify.com" in domain:
            myshopify_url = urlunparse(("https", domain, "", "", "", ""))
            domain = domain.replace(".myshopify.com", ".com")
        if domain.startswith("www."):
            domain = domain[4:]
        cleaned = urlunparse(("https", domain, "", "", "", ""))
        return cleaned, myshopify_url
    except Exception:
        return "", None


def is_translate_url(url: str) -> bool:
    return "translate.google.com" in url


def filter_urls(urls: list[str], existing_domains: set | None = None) -> tuple[list[str], dict]:
    existing_domains = existing_domains or set()
    existing_domains = {d.lower() for d in existing_domains}
    seen_domains: set[str] = set()
    cleaned_urls: list[str] = []
    url_map: dict[str, str] = {}

    for url in urls:
        cleaned, myshopify = clean_url(url)
        domain = extract_domain(cleaned)
        if not cleaned or not domain:
            continue
        if is_translate_url(cleaned):
            continue
        if domain in existing_domains or domain in seen_domains:
            continue
        cleaned_urls.append(cleaned)
        seen_domains.add(domain)
        if myshopify:
            url_map[cleaned] = myshopify

    return cleaned_urls, url_map


# ── GoogleShopifySearcher ──────────────────────────────────

_search_manager: Optional[SearchManager] = None


def _get_search_manager() -> SearchManager:
    global _search_manager
    if _search_manager is None:
        _search_manager = SearchManager()
    return _search_manager


class GoogleShopifySearcher(BaseScraper):
    """Google 搜索发现 Shopify 店铺（多 API 自动切换）"""

    def __init__(self):
        super().__init__("GoogleShopifySearcher")
        self._manager = _get_search_manager()
        status = self._manager.get_status()
        names = [f"{s['name']}({s['available_keys']})" for s in status if s['enabled']]
        log.info(f"搜索初始化: {', '.join(names) if names else '无可用 API'}")

    def _search_page(self, query: str, page: int = 1, provider_name: str = "") -> list[str]:
        """搜索一页，可指定或自动切换 API"""
        try:
            result = self._manager.search(query, page, provider_name=provider_name)
            return result.urls
        except ScrapeProviderError as e:
            log.error(str(e))
            return []

    def scrape(self, query: Optional[str] = None, max_pages: int = 0, provider_name: str = "") -> ScrapeResult:
        """搜索 Google 结果

        参数:
            query: 搜索查询，为 None 时使用默认 SHOPIFY_QUERIES
            max_pages: 最大搜索页数，0 表示遍历全部页面（直到无结果）
            provider_name: 指定搜索 API 提供者名称
        """
        thread_name = threading.current_thread().name
        queries = [query] if query else SHOPIFY_QUERIES
        result = ScrapeResult(source="multi_api")

        for q in queries:
            page = 1
            consecutive_empty = 0
            while True:
                try:
                    urls = self._search_page(q, page, provider_name=provider_name)
                    if not urls:
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            log.info(f"[{thread_name}] 查询 {q!r}: 连续 {consecutive_empty} 页无结果，停止搜索")
                            break
                    else:
                        consecutive_empty = 0
                        result.data.extend({"url": u, "query": q, "source": provider_name or "multi_api"} for u in urls)
                        result.total_found += len(urls)
                        log.info(f"[{thread_name}] 查询 {q!r} 第 {page} 页: {len(urls)} 个结果")
                except Exception as e:
                    log.error(f"[{thread_name}] 搜索异常: {e}")
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break

                if max_pages > 0 and page >= max_pages:
                    log.info(f"[{thread_name}] 查询 {q!r}: 已达到最大页数 {max_pages}，停止搜索")
                    break

                page += 1
                time.sleep(1)

        result.data = list({d["url"]: d for d in result.data}.values())
        return result

    @property
    def ready(self) -> bool:
        return len(self._manager.available_providers) > 0

    @staticmethod
    def get_api_status() -> list[dict]:
        return _get_search_manager().get_status()

    @staticmethod
    def reset_keys():
        global _search_manager
        _search_manager = None
