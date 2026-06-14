"""Shopify 商品数据提取器"""

import json
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from qmds.core.base import BaseScraper, ScrapeResult
from qmds.core.exceptions import ExtractionError, RateLimitError
from qmds.modules.data_scraper.models.schemas import Product
from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger

log = get_logger("shopify_extractor")

NON_ENGLISH_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\u0400-\u04ff\u0600-\u06ff\u0e00-\u0e7f\u0e80-\u0eff]")


class ShopifyExtractor(BaseScraper):
    """从 Shopify 店铺提取商品数据"""

    def __init__(self, http_client: Optional[HttpClient] = None):
        super().__init__("ShopifyExtractor")
        self.http = http_client or HttpClient()
        self._session_products_urls: dict[str, str] = {}

    def scrape(self, domain: str, max_pages: int = 10, **kwargs) -> ScrapeResult:
        result = ScrapeResult(source=f"shopify:{domain}")

        for page in range(1, max_pages + 1):
            url = f"https://{domain}/products.json?limit=200&page={page}"
            try:
                resp = self.http.get(url, timeout=30)
                data = resp.json()
            except RateLimitError:
                log.warning(f"{domain} rate limited at page {page}")
                break
            except Exception as e:
                log.error(f"{domain} page {page} failed: {e}")
                break

            products = data.get("products", [])
            if not products:
                break

            for p in products:
                try:
                    product = self._parse_product(p, domain)
                    result.data.append(product.__dict__)
                    result.total_scraped += 1
                except Exception as e:
                    result.errors.append(f"parse error: {p.get('handle', '')} - {e}")

            time.sleep(2)

        return result

    def _parse_product(self, raw: dict, domain: str) -> Product:
        title = raw.get("title", "")
        handle = raw.get("handle", "")
        tags = raw.get("tags", "")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        variants = raw.get("variants", [])
        first_var = variants[0] if variants else {}
        price = float(first_var.get("price", 0) or 0)
        compare_at = first_var.get("compare_at_price")
        compare_at = float(compare_at) if compare_at else None

        images = [img.get("src", "") for img in raw.get("images", []) if img.get("src")]

        return Product(
            source_url=f"https://{domain}/products/{handle}",
            title=title,
            handle=handle,
            sku=first_var.get("sku", ""),
            price=price,
            compare_at_price=compare_at,
            currency="USD",
            body_html=raw.get("body_html", ""),
            tags=tags,
            images=images,
            product_type=raw.get("product_type", ""),
            variants=[{k: v for k, v in v.items() if isinstance(v, (str, int, float, bool))} for v in variants],
            raw=raw,
        )
