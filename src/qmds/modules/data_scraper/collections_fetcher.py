"""Shopify collections.json 抓取器：获取店铺的所有 collection 信息"""

from typing import Optional
from urllib.parse import urlparse

from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger

log = get_logger("collections_fetcher")


def fetch_collections(http: HttpClient, base_url: str, max_pages: int = 4) -> list[dict]:
    """从 Shopify 店铺的 /collections.json 获取所有 collection

    Args:
        http: HTTP 客户端
        base_url: 店铺 URL（如 https://store.com）
        max_pages: 最大分页数

    Returns:
        [{"title": "Speakers", "handle": "speakers"}, ...]
    """
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    all_collections: list[dict] = []

    for page in range(1, max_pages + 1):
        url = f"{base}/collections.json?limit=250&page={page}"
        try:
            resp = http.get(url, timeout=15)
            if resp.status_code != 200:
                if page == 1:
                    log.debug(f"collections.json 返回 {resp.status_code}: {base}")
                break
            data = resp.json()
            collections = data.get("collections", [])
            if not collections:
                break
            for c in collections:
                title = (c.get("title") or "").strip()
                handle = (c.get("handle") or "").strip()
                if title and handle:
                    all_collections.append({"title": title, "handle": handle})
        except Exception as e:
            if page == 1:
                log.debug(f"collections.json 请求失败: {base} - {e}")
            break

    return all_collections
