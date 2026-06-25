"""数据爬取模块引擎 - 模块入口"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
from pymongo import UpdateOne, InsertOne

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

    def __init__(self, http_client: Optional[HttpClient] = None, max_workers: int = 10):
        proxies = settings.load_proxies()
        log.info(f"代理文件: {settings.proxies_file}, 加载到 {len(proxies)} 个代理")
        pm = ProxyManager(proxies) if proxies else None
        self.http = http_client or HttpClient(proxy_manager=pm)
        self.extractor = ShopifyExtractor(self.http)
        self.filter = ProductFilter()
        self.processor = ProductProcessor()
        self.searcher = GoogleShopifySearcher()
        self.detector = PlatformDetector(proxy_manager=pm)
        
        # 全局线程池
        self._max_workers = max_workers
        self._executor: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        
        if pm:
            log.info(f"代理池已启用: {pm.available_count}/{pm.total_count} 个可用")
        else:
            log.info("代理池未启用（无代理配置）")

    @property
    def executor(self) -> ThreadPoolExecutor:
        """获取全局线程池（懒加载）"""
        if self._executor is None:
            with self._lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix="qmds_worker"
                    )
                    log.info(f"全局线程池已创建: {self._max_workers} 线程")
        return self._executor

    def shutdown(self):
        """关闭线程池"""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
            log.info("全局线程池已关闭")

    def __del__(self):
        """析构时关闭线程池"""
        self.shutdown()

    def discover_stores(self, query: Optional[str] = None, max_pages: int = 0) -> ScrapeResult:
        """阶段1：发现店铺 URL

        参数:
            query: 搜索查询，为 None 时使用默认查询
            max_pages: 最大搜索页数，0 表示遍历全部页面（直到无结果）
        """
        log.info(f"开始发现店铺 (query={query or 'default'}, pages={'全部' if max_pages == 0 else max_pages})")
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

    def run_pipeline(self, query: str, max_pages: int = 0, max_product_pages: int = 10) -> ScrapeResult:
        """完整流水线：发现 → 检测 → 提取 → 清洗

        参数:
            query: 搜索关键词
            max_pages: 最大搜索页数，0 表示遍历全部页面（直到无结果）
            max_product_pages: 最大商品提取页数
        """
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

    def fetch_shopify_urls(self, category: str, keyword: str, max_pages: int = 0, min_products: int = 0,
                          workers: int = 10, save_mongo: bool = False, save_excel: bool = False,
                          provider_name: str = "") -> dict:
        """按类目搜索店铺 URL（搜索 → 清洗 → 平台检测 → 存储）

        参数:
            category: 类目名称（用于集合命名和存储）
            keyword: 搜索关键词（用于 Google 搜索）
            max_pages: Google 搜索页数，0 表示遍历全部页面（直到无结果）
            min_products: 最低商品数（过滤条件）
            workers: 平台检测并发数
            save_mongo: 是否保存到 MongoDB
            save_excel: 是否导出到 Excel

        返回:
            {
                "category": str,
                "keyword": str,
                "total_raw": int,
                "total_after_filter": int,
                "total_shopify": int,
                "not_shopify": int,
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
        log.info(f"清洗去重后剩余 {len(cleaned_urls)} 个 URL，启动平台检测（{workers} 线程）")

        # 多线程平台检测
        detector = PlatformDetector(proxy_manager=self.http.proxy_manager)
        detection_results: dict[str, dict] = {}
        not_shopify_count = 0

        def _detect_single(url: str) -> tuple[str, Optional[dict]]:
            detect_url = url_map.get(url) or url
            try:
                result = detector.detect(detect_url, url_map=url_map)
                if result and result.platform.value == "shopify":
                    return url, {
                        "platform": "Shopify",
                        "product_count": result.product_count,
                        "store_name": result.store_name,
                        "currency": result.currency,
                    }
                return url, None
            except Exception as e:
                log.debug(f"检测失败 {url}: {e}")
                return url, None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_detect_single, url): url for url in cleaned_urls}
            done_count = 0
            total_count = len(futures)
            for future in as_completed(futures):
                done_count += 1
                url, result = future.result()
                if result:
                    detection_results[url] = result
                    domain = extract_domain(url)
                    log.info(f"[{done_count}/{total_count}] Shopify: {domain} ({result['product_count']} 商品)")
                else:
                    not_shopify_count += 1
                    log.info(f"[{done_count}/{total_count}] 非 Shopify: {extract_domain(url)}")

        # 构建最终结果（仅保留 Shopify 店铺）
        stores = []
        for url in cleaned_urls:
            if url not in detection_results:
                continue
            detect_info = detection_results[url]
            detect_url = url_map.get(url) or url
            search_query = url_query_map.get(url, "")
            domain = extract_domain(url)
            product_count = detect_info.get("product_count", 0)
            # 过滤低于最低商品数的店铺
            if min_products > 0 and product_count < min_products:
                log.info(f"跳过 {domain}: 商品数 {product_count} < {min_products}")
                continue
            stores.append({
                "url": detect_url,
                "domain": domain,
                "platform": detect_info.get("platform", "Shopify"),
                "product_count": product_count,
                "store_name": detect_info.get("store_name", ""),
                "currency": detect_info.get("currency", "USD"),
                "category": category,
                "search_query": search_query,
                "source": "google_search",
            })

        log.info(f"检测完成: Shopify {len(stores)} 个，非 Shopify {not_shopify_count} 个")

        result = {
            "category": category,
            "keyword": keyword,
            "total_raw": len(all_raw_urls),
            "total_after_filter": len(cleaned_urls),
            "total_shopify": len(stores),
            "not_shopify": not_shopify_count,
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

    def fetch_shopify_urls_by_keyword(self, category: str, keyword: str, max_pages: int = 0,
                                       min_products: int = 0, workers: int = 10,
                                       keyword_workers: int = 1,
                                       save_mongo: bool = True, save_excel: bool = False,
                                       provider_name: str = "") -> dict:
        """按关键词处理：搜索 → 平台检测 → 去重 → 存入数据库

        与 fetch_shopify_urls 的区别：每处理完一个关键词立即存储到数据库，
        避免程序中断导致数据丢失，同时实时检测数据库中是否已存在该网站。

        参数:
            category: 类目名称（用于集合命名和存储）
            keyword: 搜索关键词（支持逗号/分号/换行分隔多个关键词）
            max_pages: Google 搜索页数，0 表示遍历全部页面
            min_products: 最低商品数（过滤条件）
            workers: 平台检测并发数（每个关键词内部）
            keyword_workers: 关键词并行处理数（1=串行，>1=多线程并行处理多个关键词）
            save_mongo: 是否保存到 MongoDB（默认 True）
            save_excel: 是否导出到 Excel
            provider_name: 指定搜索 API 提供者

        返回:
            {
                "category": str,
                "keywords": list[str],
                "total_keywords": int,
                "processed_keywords": int,
                "total_raw": int,
                "total_shopify": int,
                "not_shopify": int,
                "new_stores": int,
                "updated_stores": int,
                "skipped_keywords": list[str],
                "results_per_keyword": list[dict],
            }
        """
        keywords = [kw.strip() for kw in keyword.replace(";", "\n").replace(",", "\n").splitlines() if kw.strip()]
        log.info(f"fetch_shopify_urls_by_keyword: category={category!r} keywords={keywords} keyword_workers={keyword_workers}")

        # 初始化数据库客户端
        db = MongoDBClient() if save_mongo else None

        # 获取已有域名（用于去重）
        existing_domains: set[str] = set()
        if db:
            try:
                existing_domains = self._get_existing_domains(db, category)
                log.info(f"数据库中已有 {len(existing_domains)} 个域名")
            except Exception as e:
                log.warning(f"获取已有域名失败: {e}")

        # 线程安全锁
        lock = self._lock

        # 统计信息（线程安全）
        stats = {
            "total_raw": 0,
            "total_shopify": 0,
            "not_shopify_count": 0,
            "new_stores": 0,
            "updated_stores": 0,
        }
        skipped_keywords = []
        results_per_keyword = []

        def _process_single_keyword(idx: int, kw: str) -> dict:
            """处理单个关键词"""
            thread_name = threading.current_thread().name
            log.info(f"[{thread_name}] 开始处理关键词 [{idx}/{len(keywords)}]: {kw!r}")

            keyword_result = {
                "keyword": kw,
                "raw_count": 0,
                "shopify_count": 0,
                "not_shopify_count": 0,
                "new_count": 0,
                "updated_count": 0,
                "error": None,
            }

            try:
                # 1. 并发搜索该关键词的 URL
                variants = [
                    f"{kw} inurl:collections/all",
                    f"{kw} inurl:collections/all - page 123",
                    f"{kw} inurl:collections/all - page 88",
                ]

                all_raw_urls = []
                variant_results = {}  # 存储每个变体的结果

                def _search_variant(query: str, variant_idx: int) -> tuple[int, str, list[str]]:
                    t_name = threading.current_thread().name
                    log.info(f"[{t_name}] >>> 变体{variant_idx}开始: {query!r}")
                    raw_result = self.searcher.scrape(query=query, max_pages=max_pages, provider_name=provider_name)
                    urls = [item["url"] for item in raw_result.data]
                    log.info(f"[{t_name}] <<< 变体{variant_idx}完成: {query!r} -> {len(urls)} 个URL")
                    return variant_idx, query, urls

                # 使用全局线程池并发搜索
                log.info(f"[{thread_name}] === 并发搜索 {len(variants)} 个变体 ===")
                search_futures = {}
                for i, q in enumerate(variants, 1):
                    future = self.executor.submit(_search_variant, q, i)
                    search_futures[future] = (i, q)
                    log.info(f"[{thread_name}] 提交变体{i}到线程池: {q!r}")

                log.info(f"[{thread_name}] 等待 {len(search_futures)} 个变体完成...")
                for future in as_completed(search_futures):
                    try:
                        variant_idx, query, urls = future.result()
                        all_raw_urls.extend(urls)
                        variant_results[variant_idx] = {"query": query, "count": len(urls)}
                        log.info(f"[{thread_name}] 变体{variant_idx}已完成，当前总URL数: {len(all_raw_urls)}")
                    except Exception as e:
                        i, q = search_futures[future]
                        log.warning(f"[{thread_name}] 变体{i}失败 {q}: {e}")

                # 输出变体汇总
                for i, info in sorted(variant_results.items()):
                    log.info(f"[{thread_name}] 变体{i}结果: {info['query']!r} -> {info['count']} 个URL")
                log.info(f"[{thread_name}] === 关键词 {kw!r} 搜索完成: 共 {len(all_raw_urls)} 个URL ===")

                with lock:
                    stats["total_raw"] += len(all_raw_urls)

                if not all_raw_urls:
                    log.info(f"[{thread_name}] 关键词 {kw!r} 未搜索到任何 URL，跳过")
                    return keyword_result

                # 2. 清洗去重（与数据库已有域名去重）
                with lock:
                    cleaned_urls, url_map = filter_urls(all_raw_urls, existing_domains=existing_domains)
                log.info(f"[{thread_name}] 关键词 {kw!r} 清洗去重后剩余 {len(cleaned_urls)} 个 URL")

                if not cleaned_urls:
                    log.info(f"[{thread_name}] 关键词 {kw!r} 去重后无新 URL，跳过")
                    return keyword_result

                # 3. 平台检测
                log.info(f"[{thread_name}] 关键词 {kw!r} 开始平台检测: {len(cleaned_urls)} 个URL")
                detection_results = self._detect_platforms(cleaned_urls, url_map, workers)
                log.info(f"[{thread_name}] 关键词 {kw!r} 平台检测完成: {len(detection_results)} 个Shopify")

                # 4. 构建店铺数据并存储
                stores_to_save = []
                for url in cleaned_urls:
                    if url not in detection_results:
                        with lock:
                            stats["not_shopify_count"] += 1
                        keyword_result["not_shopify_count"] += 1
                        continue

                    detect_info = detection_results[url]
                    detect_url = url_map.get(url) or url
                    domain = extract_domain(url)
                    product_count = detect_info.get("product_count", 0)

                    # 过滤低于最低商品数的店铺
                    if min_products > 0 and product_count < min_products:
                        log.info(f"[{thread_name}] 跳过 {domain}: 商品数 {product_count} < {min_products}")
                        with lock:
                            stats["not_shopify_count"] += 1
                        keyword_result["not_shopify_count"] += 1
                        continue

                    store_data = {
                        "url": detect_url,
                        "domain": domain,
                        "platform": detect_info.get("platform", "Shopify"),
                        "product_count": product_count,
                        "store_name": detect_info.get("store_name", ""),
                        "currency": detect_info.get("currency", "USD"),
                        "category": category,
                        "search_query": kw,
                        "source": "google_search",
                    }
                    stores_to_save.append(store_data)

                    with lock:
                        stats["total_shopify"] += 1
                        existing_domains.add(domain)
                    keyword_result["shopify_count"] += 1

                # 5. 存入数据库
                if db and stores_to_save:
                    log.info(f"[{thread_name}] 关键词 {kw!r} 写入数据库: {len(stores_to_save)} 条")
                    saved_count, new_count, updated_count = self._save_and_check_duplicates(
                        db, category, stores_to_save
                    )
                    with lock:
                        stats["new_stores"] += new_count
                        stats["updated_stores"] += updated_count
                    keyword_result["new_count"] = new_count
                    keyword_result["updated_count"] = updated_count
                    log.info(f"[{thread_name}] 关键词 {kw!r} 存储完成: 新增 {new_count}, 更新 {updated_count}")

                # 6. 导出 Excel（可选）
                if save_excel and stores_to_save:
                    with lock:
                        self.export_to_excel(stores_to_save, category)

                log.info(f"[{thread_name}] 关键词 {kw!r} 处理完成")

            except Exception as e:
                log.error(f"[{thread_name}] 处理关键词 {kw!r} 失败: {e}")
                keyword_result["error"] = str(e)

            return keyword_result

        # 根据 keyword_workers 决定串行或并行处理关键词
        if keyword_workers > 1 and len(keywords) > 1:
            log.info(f"启用关键词并行处理: {keyword_workers} 线程")
            # 使用全局线程池
            futures = {
                self.executor.submit(_process_single_keyword, idx, kw): (idx, kw)
                for idx, kw in enumerate(keywords, 1)
            }
            for future in as_completed(futures):
                idx, kw = futures[future]
                try:
                    keyword_result = future.result()
                    results_per_keyword.append(keyword_result)
                    if keyword_result.get("error"):
                        skipped_keywords.append(kw)
                except Exception as e:
                    log.error(f"关键词 {kw!r} 执行异常: {e}")
                    skipped_keywords.append(kw)
                    results_per_keyword.append({
                        "keyword": kw, "raw_count": 0, "shopify_count": 0,
                        "not_shopify_count": 0, "new_count": 0, "updated_count": 0,
                        "error": str(e),
                    })
        else:
            log.info("串行处理关键词")
            for idx, kw in enumerate(keywords, 1):
                keyword_result = _process_single_keyword(idx, kw)
                results_per_keyword.append(keyword_result)
                if keyword_result.get("error"):
                    skipped_keywords.append(kw)

        # 关闭数据库连接
        if db:
            db.close()

        # 按原始关键词顺序排序结果
        kw_order = {kw: i for i, kw in enumerate(keywords)}
        results_per_keyword.sort(key=lambda x: kw_order.get(x["keyword"], 999))

        result = {
            "category": category,
            "keywords": keywords,
            "total_keywords": len(keywords),
            "processed_keywords": len(keywords) - len(skipped_keywords),
            "total_raw": stats["total_raw"],
            "total_shopify": stats["total_shopify"],
            "not_shopify": stats["not_shopify_count"],
            "new_stores": stats["new_stores"],
            "updated_stores": stats["updated_stores"],
            "skipped_keywords": skipped_keywords,
            "results_per_keyword": results_per_keyword,
        }

        log.info(f"全部处理完成: {result['processed_keywords']}/{result['total_keywords']} 个关键词, "
                 f"Shopify {stats['total_shopify']} 个, 新增 {stats['new_stores']}, 更新 {stats['updated_stores']}")

        return result

    def _get_existing_domains(self, db: MongoDBClient, category: str) -> set[str]:
        """获取数据库中已有的域名集合"""
        col = db.unfiltered_col(category)
        domains = set()
        for doc in col.find({}, {"domain": 1}):
            if "domain" in doc:
                domains.add(doc["domain"])
        return domains

    def _detect_platforms(self, urls: list[str], url_map: dict, workers: int) -> dict[str, dict]:
        """多线程平台检测（使用全局线程池）"""
        detection_results: dict[str, dict] = {}

        def _detect_single(url: str) -> tuple[str, Optional[dict]]:
            thread_name = threading.current_thread().name
            detect_url = url_map.get(url) or url
            try:
                result = self.detector.detect(detect_url, url_map=url_map)
                if result and result.platform.value == "shopify":
                    log.debug(f"[{thread_name}] 检测 Shopify: {extract_domain(url)} ({result.product_count} 商品)")
                    return url, {
                        "platform": "Shopify",
                        "product_count": result.product_count,
                        "store_name": result.store_name,
                        "currency": result.currency,
                    }
                return url, None
            except Exception as e:
                log.debug(f"[{thread_name}] 检测失败 {url}: {e}")
                return url, None

        # 使用全局线程池
        log.info(f"启动平台检测: {len(urls)} 个URL")
        futures = {self.executor.submit(_detect_single, url): url for url in urls}
        done_count = 0
        shopify_count = 0
        total_count = len(futures)
        for future in as_completed(futures):
            done_count += 1
            url, result = future.result()
            if result:
                detection_results[url] = result
                shopify_count += 1
                domain = extract_domain(url)
                log.info(f"[{done_count}/{total_count}] Shopify: {domain} ({result['product_count']} 商品)")
            else:
                log.info(f"[{done_count}/{total_count}] 非 Shopify: {extract_domain(url)}")

        log.info(f"平台检测完成: {shopify_count}/{total_count} 个 Shopify")
        return detection_results

    def _save_and_check_duplicates(self, db: MongoDBClient, category: str,
                                    stores: list[dict]) -> tuple[int, int, int]:
        """保存店铺数据并检测重复（批量写入优化）

        返回:
            (saved_count, new_count, updated_count)
        """
        if not stores:
            return 0, 0, 0

        col = db.unfiltered_col(category)
        ts = datetime.utcnow().isoformat()

        # 批量查询已存在的域名
        domains = [store["domain"] for store in stores]
        existing_docs = col.find({"domain": {"$in": domains}}, {"domain": 1})
        existing_domains = {doc["domain"] for doc in existing_docs}

        # 构建批量操作
        operations = []
        new_count = 0
        update_count = 0

        for store in stores:
            domain = store["domain"]
            common_fields = {
                "url": store["url"],
                "platform": store["platform"],
                "product_count": store.get("product_count", 0),
                "store_name": store.get("store_name", ""),
                "currency": store.get("currency", "USD"),
                "category": category,
                "search_query": store.get("search_query", ""),
                "source": store.get("source", "google_search"),
                "updated_at": ts,
            }

            if domain in existing_domains:
                # 已存在，更新
                operations.append(UpdateOne(
                    {"domain": domain},
                    {"$set": common_fields}
                ))
                update_count += 1
            else:
                # 不存在，插入
                common_fields["domain"] = domain
                common_fields["created_at"] = ts
                operations.append(InsertOne(common_fields))
                new_count += 1

        # 执行批量写入
        saved_count = 0
        if operations:
            try:
                result = col.bulk_write(operations, ordered=False)
                saved_count = (result.inserted_count or 0) + (result.modified_count or 0)
                log.debug(f"批量写入完成: 插入 {result.inserted_count}, 更新 {result.modified_count}")
            except Exception as e:
                log.error(f"批量写入异常: {e}")
                # 降级为逐条写入
                saved_count = self._save_fallback(col, stores, ts)

        return saved_count, new_count, update_count

    def _save_fallback(self, col, stores: list[dict], ts: str) -> int:
        """降级逐条写入（bulk_write 失败时）"""
        saved_count = 0
        for store in stores:
            try:
                result = col.update_one(
                    {"domain": store["domain"]},
                    {"$set": {
                        "url": store["url"],
                        "platform": store["platform"],
                        "product_count": store.get("product_count", 0),
                        "store_name": store.get("store_name", ""),
                        "currency": store.get("currency", "USD"),
                        "category": store.get("category", ""),
                        "search_query": store.get("search_query", ""),
                        "source": store.get("source", "google_search"),
                        "updated_at": ts,
                    }, "$setOnInsert": {
                        "domain": store["domain"],
                        "created_at": ts,
                    }},
                    upsert=True
                )
                if result.upserted_id or result.modified_count > 0:
                    saved_count += 1
            except Exception as e:
                log.warning(f"写入失败 {store['domain']}: {e}")
        return saved_count
