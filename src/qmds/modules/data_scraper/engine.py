"""数据爬取模块引擎 - 模块入口"""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

from qmds.config import settings
from qmds.core.base import ScrapeResult
from qmds.db.mongodb import MongoDBClient
from qmds.modules.data_scraper.discovery import GoogleShopifySearcher
from qmds.modules.data_scraper.discovery.google_search import clean_url, extract_domain, filter_urls
from qmds.modules.data_scraper.detection import PlatformDetector
from qmds.modules.data_scraper.extraction import ShopifyExtractor
from qmds.modules.data_scraper.models.schemas import Product, ScrapeTask, TaskStatus
from qmds.modules.data_scraper.pipeline import ProductFilter, ProductProcessor
from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger
from qmds.utils.proxy_manager import ProxyManager

log = get_logger("data_scraper")


class DataScraperModule:
    """数据爬取模块 - 统一入口"""

    def __init__(self, http_client: Optional[HttpClient] = None):
        proxies = settings.load_proxies()
        log.info(f"代理文件: {settings.proxies_file}, 加载到 {len(proxies)} 个代理")
        pm = ProxyManager(proxies) if proxies else None
        self.http = http_client or HttpClient(proxy_manager=pm)
        self.extractor = ShopifyExtractor(self.http)
        self.filter = ProductFilter()
        self.processor = ProductProcessor()
        self.searcher = GoogleShopifySearcher()
        self.detector = PlatformDetector(proxy_manager=pm)
        if pm:
            log.info(f"代理池已启用: {pm.available_count}/{pm.total_count} 个可用")
        else:
            log.info("代理池未启用（无代理配置）")

    def discover_stores(self, query: Optional[str] = None, max_pages: int = 3) -> ScrapeResult:
        """阶段1：发现店铺 URL"""
        log.info(f"开始发现店铺 (query={query or 'default'}, pages={max_pages})")
        return self.searcher.scrape(query=query, max_pages=max_pages)

    def detect_platform(self, url: str):
        """阶段2：检测平台类型"""
        return self.detector.detect(url)

    def extract_products(self, domain: str, max_pages: int = 10) -> ScrapeResult:
        """阶段3：提取商品数据"""
        log.info(f"开始提取商品: {domain}")
        raw_result = self.extractor.scrape(domain=domain, max_pages=max_pages)

        import json
        products = [Product(**d) if isinstance(d, dict) else d for d in raw_result.data]

        products = self.processor.process_all(products)
        products = self.processor.deduplicate_by_title(products)
        products = self.filter.filter(products)

        filtered_products = [p for p in products if not self.filter.has_prohibited_content(p)]
        raw_result.data = [p.__dict__ for p in filtered_products]
        raw_result.total_scraped = len(filtered_products)

        log.info(f"{domain}: 提取 {raw_result.total_scraped} 个有效商品 (原始 {len(products)} 个，去重后 {len(filtered_products)} 个)")
        return raw_result

    def run_pipeline(self, query: str, max_pages: int = 3, max_product_pages: int = 10) -> ScrapeResult:
        """完整流水线：发现 → 检测 → 提取 → 清洗"""
        stores = self.discover_stores(query, max_pages)
        all_products = ScrapeResult(source=f"pipeline:{query}")

        for item in stores.data:
            url = item["url"]
            detection = self.detect_platform(url)
            if not detection:
                continue

            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            products = self.extract_products(domain, max_product_pages)
            all_products.data.extend(products.data)
            all_products.total_scraped += products.total_scraped

        all_products.total_found = stores.total_found
        return all_products

    def save_to_mongodb(self, stores: list[dict], category: str) -> int:
        """将店铺结果保存到 MongoDB {category}_unfiltered（按 domain upsert）"""
        try:
            db = MongoDBClient()
            count = db.save_unfiltered(category, stores)
            db.close()
            return count
        except Exception as e:
            log.error(f"MongoDB 写入失败: {e}")
            return 0

    def export_to_excel(self, stores: list[dict], category: str) -> Optional[Path]:
        """将店铺结果导出到 Excel（追加/去重合并）——与 YSQD 一致"""
        try:
            data_dir = settings.data_dir / category
            data_dir.mkdir(parents=True, exist_ok=True)
            filepath = data_dir / f"{category}.xlsx"

            new_df = pd.DataFrame(stores)
            if "Timestamp" not in new_df.columns:
                new_df["Timestamp"] = datetime.now().isoformat()

            if filepath.exists():
                try:
                    old_df = pd.read_excel(filepath, engine="openpyxl")
                    df = pd.concat([old_df, new_df], ignore_index=True)
                    df["Standardized_Domain"] = df["domain"].astype(str).str.lower()
                    df = df.drop_duplicates(subset=["Standardized_Domain"], keep="last")
                    df = df.drop(columns=["Standardized_Domain"])
                    df = df.sort_values(by="Timestamp", na_position="last")
                except Exception:
                    df = new_df
            else:
                df = new_df

            df.to_excel(filepath, index=False, engine="openpyxl")
            log.info(f"Excel 导出: {filepath} ({len(df)} 条)")
            return filepath
        except Exception as e:
            log.error(f"Excel 导出失败: {e}")
            return None

    def fetch_shopify_urls(self, category: str, keyword: str, max_pages: int = 2, min_products: int = 0,
                          workers: int = 10, save_mongo: bool = False, save_excel: bool = False,
                          provider_name: str = "") -> dict:
        """按类目搜索店铺 URL（搜索 → 清洗 → 存储）

        参数:
            category: 类目名称（用于集合命名和存储）
            keyword: 搜索关键词（用于 Google 搜索）
            max_pages: Google 搜索页数
            min_products: 未使用（已跳过平台检测）
            save_mongo: 是否保存到 MongoDB
            save_excel: 是否导出到 Excel

        返回:
            {
                "category": str,
                "keyword": str,
                "total_raw": int,
                "total_after_filter": int,
                "total_shopify": int,
                "stores": [...],
                "saved_mongodb": int | None,
                "saved_excel": str | None,
                "proxy_available": int,
            }
        """
        # 支持逗号/分号分隔的多个关键词（与 YSQD 一致）
        keywords = [kw.strip() for kw in keyword.replace(";", "\n").replace(",", "\n").splitlines() if kw.strip()]
        log.info(f"fetch_shopify_urls: category={category!r} keywords={keywords}")

        proxy_count = 0
        if self.http.proxy_manager:
            proxy_count = self.http.proxy_manager.available_count

        all_raw_urls = []
        url_query_map: dict[str, str] = {}
        for kw in keywords:
            # 与 YSQD 一致：关键词 + inurl:collections/all
            variants = [
                f"{kw} inurl:collections/all",
                f"{kw} inurl:collections/all - page 123",
                f"{kw} inurl:collections/all - page 88",
            ]
            for query in variants:
                log.info(f"搜索: {query!r}")
                raw_result = self.searcher.scrape(query=query, max_pages=max_pages, provider_name=provider_name)
                for item in raw_result.data:
                    all_raw_urls.append(item["url"])
                    url_query_map[item["url"]] = query

        log.info(f"原始搜索到 {len(all_raw_urls)} 个 URL（去重前）")

        cleaned_urls, url_map = filter_urls(all_raw_urls)
        log.info(f"清洗去重后剩余 {len(cleaned_urls)} 个 URL")

        stores = []
        for url in cleaned_urls:
            detect_url = url_map.get(url) or url
            search_query = url_query_map.get(url, "")
            domain = extract_domain(url)
            stores.append({
                "url": detect_url,
                "domain": domain,
                "platform": "",
                "product_count": 0,
                "store_name": "",
                "currency": "USD",
                "category": category,
                "search_query": search_query,
                "source": "google_search",
            })

        log.info(f"共 {len(stores)} 个店铺 URL")

        result = {
            "category": category,
            "keyword": keyword,
            "total_raw": len(all_raw_urls),
            "total_after_filter": len(cleaned_urls),
            "total_shopify": len(stores),
            "stores": stores,
            "saved_mongodb": None,
            "saved_excel": None,
            "proxy_available": proxy_count,
        }

        if save_mongo and stores:
            result["saved_mongodb"] = self.save_to_mongodb(stores, category)

        if save_excel and stores:
            path = self.export_to_excel(stores, category)
            if path:
                result["saved_excel"] = str(path)

        return result
