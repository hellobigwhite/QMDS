"""MongoDB 数据库客户端 - qmds_url_stores 专用

单一集合模式：
- 未筛选店铺 URL（一级分类维度）: {category} 集合（无 __ 分隔符），filter_status=unfiltered
- 已筛选 collection URL（二级分类维度）: {category}__{subcategory} 集合，用 filter_status / crawl_status 字段区分状态
"""

from datetime import datetime
from typing import Any, Optional

import time
import threading

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure
from pymongo.collection import Collection

from qmds.config import settings
from qmds.config.categories import (
    make_collection_prefix,
    parse_collection_prefix,
    normalize_subcategory,
    get_google_category_name,
)
from qmds.utils.logger import get_logger

log = get_logger("mongodb")


# 筛选状态
FILTER_STATUS_UNFILTERED = "unfiltered"  # 未筛选（店铺URL在 {category} 集合中）
FILTER_STATUS_FILTERED = "filtered"      # 已筛选（collection URL 在 {category}__{subcategory} 集合中）
FILTER_STATUS_FAILED = "filter_failed"   # 筛选失败
FILTER_STATUS_UNCERTAIN = "uncertain"    # 平台检测被拦截/无法确认，待人工复核（不按非 Shopify 误杀）
FILTER_STATUS_NOT_SHOPIFY = "not_shopify"  # 待确认重检后确认非 Shopify（从待确认中移除）

# 爬取状态
CRAWL_STATUS_UNCRAWLED = "uncrawled"    # 未爬取
CRAWL_STATUS_CRAWLED = "crawled"        # 已爬取
CRAWL_STATUS_FAILED = "crawl_failed"    # 爬取失败

# filtered 记录的人工审核状态（手动筛选精准类目：逐个浏览网站后的决策标记）
REVIEW_STATUS_PENDING = "pending"   # 未审核（默认；老数据无该字段同样视为未审核）
REVIEW_STATUS_KEPT = "kept"         # 人工浏览后确认保留

# filtered_failed 集合的过滤原因枚举
FILTER_FAIL_REASON_NON_ENGLISH = "non_english"            # 非英文站
FILTER_FAIL_REASON_BLACK_FIVE = "black_five"              # 黑五类
FILTER_FAIL_REASON_COMPREHENSIVE = "comprehensive"        # 综合站
FILTER_FAIL_REASON_CATEGORY_MISMATCH = "category_mismatch"  # 类目不匹配
FILTER_FAIL_REASON_UNRECOGNIZED = "unrecognized"          # 无法识别

# 统一的 filtered_failed 集合名（所有类目共用）
FILTERED_FAILED_COLLECTION = "filtered_failed"

# 综合站集合名（所有一级分类共用的综合站集合）
COMPREHENSIVE_COLLECTION = "comprehensive_stores"

# 数据来源标识：从 shopify_url 库读取并分类
SOURCE_SHOPIFY_URL = "shopify_url"

# 网站信息缓存状态
SITE_INFO_STATUS_PENDING = "pending"        # 待分类
SITE_INFO_STATUS_CLASSIFIED = "classified"  # 已分类

# 计数器集合名（存储各集合的状态计数，用 $inc 原子维护）
COUNTERS_COLLECTION = "_counters"


class _TTLCache:
    """简单的线程安全 TTL 缓存"""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._store.get(key)
            if item and item[1] > time.time():
                return item[0]
            self._store.pop(key, None)
            return None

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (value, time.time() + self._ttl)

    def invalidate(self, key: str = None):
        with self._lock:
            if key:
                self._store.pop(key, None)
            else:
                self._store.clear()


class MongoDBClient:
    """MongoDB 数据库客户端 - qmds_url_stores 专用（单一集合模式）

    集合结构:
    - {category}: 未筛选店铺 URL（一级分类维度），文档 filter_status=unfiltered
    - {category}__{subcategory}: 已筛选 collection URL（二级分类维度），
      文档通过 filter_status / crawl_status 字段区分状态
    """

    _stats_cache = _TTLCache(ttl_seconds=60)

    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self._uri = uri or settings.mongo_uri
        self._db_name = db_name or settings.mongo_db_url
        self._client: Optional[MongoClient] = None

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        return self._client

    @property
    def db(self):
        return self.client[self._db_name]

    def get_db(self, db_name: str):
        return self.client[db_name]

    def get_collection(self, db_name: str, collection: str) -> Collection:
        return self.client[db_name][collection]

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except ConnectionFailure:
            return False

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    # ── 集合命名 ──────────────────────────────────────────

    def unfiltered_col(self, category: str) -> Collection:
        """获取 {category} 集合（未筛选店铺URL，一级分类维度）"""
        return self.db[category]

    def filtered_col(self, category: str, subcategory: str = "") -> Collection:
        """获取 {category}__{subcategory} 单一集合（兼容旧接口）

        单一集合模式下，filtered/crawled 已合并到同一集合，用 crawl_status 字段区分。
        """
        prefix = make_collection_prefix(category, subcategory)
        return self.db[prefix]

    def crawled_col(self, category: str, subcategory: str = "") -> Collection:
        """获取 {category}__{subcategory} 单一集合（兼容旧接口，等同于 filtered_col）"""
        prefix = make_collection_prefix(category, subcategory)
        return self.db[prefix]

    def collection(self, category: str, subcategory: str = "") -> Collection:
        """获取 {category}__{subcategory} 单一集合"""
        prefix = make_collection_prefix(category, subcategory)
        return self.db[prefix]

    # ── 索引 ──────────────────────────────────────────────

    def ensure_indexes(self, category: str, subcategory: str = ""):
        """为指定集合创建索引（含 filter_status / crawl_status 索引）

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称（空字符串归入 "other"）；
                        为空时创建 {category} 集合索引（unfiltered）
        """
        if not subcategory:
            # unfiltered 集合（一级分类维度）
            uf = self.unfiltered_col(category)
            uf.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
            uf.create_index([("platform", ASCENDING)], name="idx_platform")
            uf.create_index([("created_at", ASCENDING)], name="idx_created_at")
            uf.create_index([("filter_status", ASCENDING)], name="idx_filter_status")
            log.info(f"索引已创建: {category} (unfiltered)")
            return

        prefix = make_collection_prefix(category, subcategory)
        col = self.collection(category, subcategory)

        # 删除旧的单字段 idx_domain 索引（如果存在）
        existing_indexes = {idx["name"]: idx for idx in col.list_indexes()}
        if "idx_domain" in existing_indexes:
            log.info(f"删除旧索引: {prefix}.idx_domain")
            col.drop_index("idx_domain")

        col.create_index([("domain", ASCENDING), ("collection_handle", ASCENDING)], unique=True, name="idx_domain_collection")
        col.create_index([("filtered_category", ASCENDING)], name="idx_filtered_category")
        col.create_index([("confidence", ASCENDING)], name="idx_confidence")
        col.create_index([("classified_from", ASCENDING)], name="idx_classified_from")
        col.create_index([("filter_status", ASCENDING)], name="idx_filter_status")
        col.create_index([("crawl_status", ASCENDING)], name="idx_crawl_status")
        col.create_index([("category", ASCENDING), ("subcategory", ASCENDING)], name="idx_category_subcategory")
        col.create_index([("created_at", ASCENDING)], name="idx_created_at")

        log.info(f"索引已创建: {prefix}")

    # ── 计数器（_counters 集合，用 $inc 原子维护各集合状态计数） ──

    def _counters_col(self) -> Collection:
        """获取 _counters 集合"""
        return self.db[COUNTERS_COLLECTION]

    def ensure_counters_indexes(self):
        """为 _counters 集合创建索引（_id 即集合名，天然唯一）"""
        col = self._counters_col()
        col.create_index([("collection_type", ASCENDING)], name="idx_collection_type")
        col.create_index([("category", ASCENDING)], name="idx_category")
        log.info(f"索引已创建: {COUNTERS_COLLECTION}")

    def _set_counter_type(self, collection_key: str, collection_type: str,
                          category: str = "", subcategory: str = ""):
        """设置计数器文档的元信息（collection_type/category/subcategory）

        在 $inc 之前调用 upsert 设置元信息，确保文档存在。
        """
        self._counters_col().update_one(
            {"_id": collection_key},
            {"$set": {
                "collection_type": collection_type,
                "category": category,
                "subcategory": subcategory,
                "updated_at": datetime.utcnow().isoformat(),
            }},
            upsert=True,
        )

    def _inc_counter(self, collection_key: str, field: str, delta: int,
                     doc_delta: int = 0):
        """原子增减单个状态计数

        Args:
            collection_key: 集合名（如 "hardware"、"hardware__tools"、"filtered_failed"）
            field: 状态字段名（如 "unfiltered"、"crawled"、"pending"）
            delta: 状态计数增减量（+1 或 -1）
            doc_delta: 集合文档总数增减量；纯状态迁移时保持为 0
        """
        inc_doc = {f"counts.{field}": delta}
        if doc_delta:
            inc_doc["total"] = doc_delta
        self._counters_col().update_one(
            {"_id": collection_key},
            {
                "$inc": inc_doc,
                "$set": {"updated_at": datetime.utcnow().isoformat()},
            },
            upsert=True,
        )

    def _inc_counters(self, collection_key: str, increments: dict, doc_delta: int = 0):
        """批量增减多个状态计数（单次 $inc 操作）

        Args:
            collection_key: 集合名
            increments: {"uncrawled": -1, "crawled": 1} -- 各状态的增减量
            doc_delta: 集合文档总数变化量。同一文档的状态迁移传 0（默认），
                       新增/删除文档时传 ±N，保证 total 与集合文档数一致
                       （与 rebuild_counters 的 total = count_documents({}) 对齐）
        """
        inc_doc = {f"counts.{k}": v for k, v in increments.items()}
        if doc_delta:
            inc_doc["total"] = doc_delta
        self._counters_col().update_one(
            {"_id": collection_key},
            {
                "$inc": inc_doc,
                "$set": {"updated_at": datetime.utcnow().isoformat()},
            },
            upsert=True,
        )

    def get_all_collection_counts(self) -> dict:
        """一次性返回所有集合的计数，供 API 使用

        Returns:
            按 collection_type 分组的计数:
            {
                "unfiltered": [{"_id": "hardware", "category": "hardware", "counts": {...}, "total": N}, ...],
                "filtered": [...],
                "filtered_failed": [...],
                "comprehensive_stores": [...],
                "shopify_url_info": [...],
            }
        """
        col = self._counters_col()
        docs = list(col.find({}, {"_id": 1, "collection_type": 1, "category": 1,
                                  "subcategory": 1, "counts": 1, "total": 1}))
        result: dict[str, list] = {}
        for doc in docs:
            ctype = doc.get("collection_type", "unknown")
            if ctype not in result:
                result[ctype] = []
            result[ctype].append(doc)
        return result

    def rebuild_counters(self, progress_callback=None) -> dict:
        """全量重建 _counters 集合，修复 $inc 漂移

        遍历所有集合，用 aggregate $group 统计各状态计数，覆盖写入 _counters。

        Args:
            progress_callback: 进度回调 fn(processed, total, message)，可选

        Returns:
            {"rebuilt": N, "errors": [...]} -- 重建的计数器数量和错误列表
        """
        from qmds.config.categories import parse_collection_prefix

        self.ensure_counters_indexes()

        # 预先收集目标集合，用于进度上报（先统计再清空，避免清空后进度异常）
        unfiltered_cats = self.list_categories()
        filtered_prefixes = self.list_filtered_categories()

        # 清空旧计数器
        self._counters_col().delete_many({})

        rebuilt = 0
        errors = []
        ts = datetime.utcnow().isoformat()
        step_total = len(unfiltered_cats) + len(filtered_prefixes) + 3
        step_done = 0

        def _report(msg: str):
            nonlocal step_done
            step_done += 1
            if progress_callback:
                # InterruptedError 向上传播以支持任务停止；其它回调异常忽略
                progress_callback(step_done, step_total, msg)

        # 1. unfiltered 集合（{category}，不含 __）
        for cat in unfiltered_cats:
            try:
                col = self.unfiltered_col(cat)
                counts = {}
                for status_val in [FILTER_STATUS_UNFILTERED, FILTER_STATUS_FILTERED, FILTER_STATUS_FAILED]:
                    counts[status_val] = col.count_documents({"filter_status": status_val})
                # site_info status
                counts["pending"] = col.count_documents({"status": SITE_INFO_STATUS_PENDING})
                counts["classified"] = col.count_documents({"status": SITE_INFO_STATUS_CLASSIFIED})
                total = col.count_documents({})
                self._counters_col().update_one(
                    {"_id": cat},
                    {"$set": {
                        "collection_type": "unfiltered",
                        "category": cat,
                        "subcategory": "",
                        "counts": counts,
                        "total": total,
                        "updated_at": ts,
                    }},
                    upsert=True,
                )
                rebuilt += 1
            except Exception as e:
                errors.append(f"unfiltered {cat}: {e}")
            _report(f"unfiltered: {cat}")

        # 2. filtered 集合（{category}__{subcategory}）
        for prefix in filtered_prefixes:
            try:
                cat, sub = parse_collection_prefix(prefix)
                col = self.db[prefix]
                counts = {}
                counts["filtered"] = col.count_documents({"filter_status": FILTER_STATUS_FILTERED})
                counts["uncrawled"] = col.count_documents({"crawl_status": CRAWL_STATUS_UNCRAWLED})
                counts["crawled"] = col.count_documents({"crawl_status": CRAWL_STATUS_CRAWLED})
                counts["crawl_failed"] = col.count_documents({"crawl_status": CRAWL_STATUS_FAILED})
                # 人工审核进度（手动筛选精准类目：逐个浏览后的保留标记）
                counts["kept"] = col.count_documents({"review_status": REVIEW_STATUS_KEPT})
                total = col.count_documents({})
                self._counters_col().update_one(
                    {"_id": prefix},
                    {"$set": {
                        "collection_type": "filtered",
                        "category": cat,
                        "subcategory": sub,
                        "counts": counts,
                        "total": total,
                        "updated_at": ts,
                    }},
                    upsert=True,
                )
                rebuilt += 1
            except Exception as e:
                errors.append(f"filtered {prefix}: {e}")
            _report(f"filtered: {prefix}")

        # 3. filtered_failed 集合
        try:
            col = self.filtered_failed_col()
            counts = {}
            for reason in [FILTER_FAIL_REASON_NON_ENGLISH, FILTER_FAIL_REASON_BLACK_FIVE,
                           FILTER_FAIL_REASON_COMPREHENSIVE, FILTER_FAIL_REASON_CATEGORY_MISMATCH,
                           FILTER_FAIL_REASON_UNRECOGNIZED]:
                counts[reason] = col.count_documents({"reason": reason})
            counts["filter_failed"] = col.count_documents({})
            total = col.count_documents({})
            self._counters_col().update_one(
                {"_id": FILTERED_FAILED_COLLECTION},
                {"$set": {
                    "collection_type": "filtered_failed",
                    "category": "",
                    "subcategory": "",
                    "counts": counts,
                    "total": total,
                    "updated_at": ts,
                }},
                upsert=True,
            )
            rebuilt += 1
        except Exception as e:
            errors.append(f"filtered_failed: {e}")
        _report("filtered_failed")

        # 4. comprehensive_stores 集合
        try:
            col = self.comprehensive_col()
            counts = {
                "filtered": col.count_documents({"filter_status": FILTER_STATUS_FILTERED}),
                "uncrawled": col.count_documents({"crawl_status": CRAWL_STATUS_UNCRAWLED}),
                "crawled": col.count_documents({"crawl_status": CRAWL_STATUS_CRAWLED}),
                "crawl_failed": col.count_documents({"crawl_status": CRAWL_STATUS_FAILED}),
            }
            total = col.count_documents({})
            self._counters_col().update_one(
                {"_id": settings.comprehensive_collection},
                {"$set": {
                    "collection_type": "comprehensive",
                    "category": "",
                    "subcategory": "",
                    "counts": counts,
                    "total": total,
                    "updated_at": ts,
                }},
                upsert=True,
            )
            rebuilt += 1
        except Exception as e:
            errors.append(f"comprehensive: {e}")
        _report("comprehensive")

        # 5. shopify_url_info 集合
        try:
            col = self.shopify_url_info_col()
            counts = {
                "pending": col.count_documents({"status": SITE_INFO_STATUS_PENDING}),
                "classified": col.count_documents({"status": SITE_INFO_STATUS_CLASSIFIED}),
            }
            total = col.count_documents({})
            self._counters_col().update_one(
                {"_id": settings.shopify_url_info_collection},
                {"$set": {
                    "collection_type": "info",
                    "category": "",
                    "subcategory": "",
                    "counts": counts,
                    "total": total,
                    "updated_at": ts,
                }},
                upsert=True,
            )
            rebuilt += 1
        except Exception as e:
            errors.append(f"shopify_url_info: {e}")
        _report("shopify_url_info")

        log.info(f"计数器重建完成: {rebuilt} 个集合, {len(errors)} 个错误")
        return {"rebuilt": rebuilt, "errors": errors}

    # ── 写入（未筛选） ────────────────────────────────────

    def save_unfiltered(self, category: str, stores: list[dict]) -> int:
        """保存未筛选数据到 {category} 集合（按 domain upsert）

        stores 中每个 dict 应包含:
            url, domain, platform, product_count, store_name, currency
        """
        if not stores:
            return 0
        col = self.unfiltered_col(category)
        self.ensure_indexes(category)
        ts = datetime.utcnow().isoformat()
        count = 0
        for store in stores:
            result = col.update_one(
                {"domain": store["domain"]},
                {"$set": {
                    "domain": store["domain"],
                    "url": store["url"],
                    "platform": store["platform"],
                    "product_count": store.get("product_count", 0),
                    "store_name": store.get("store_name", ""),
                    "currency": store.get("currency", "USD"),
                    "category": category,
                    "search_query": store.get("search_query", ""),
                    "source": store.get("source", "google_search"),
                    "filter_status": FILTER_STATUS_UNFILTERED,
                    "updated_at": ts,
                }, "$setOnInsert": {
                    "created_at": ts,
                }},
                upsert=True,
            )
            if result.upserted_id or result.modified_count > 0:
                count += 1
                if result.upserted_id:
                    self._set_counter_type(category, "unfiltered", category)
                    self._inc_counter(category, "unfiltered", 1, doc_delta=1)
        log.info(f"MongoDB 写入 {category} (unfiltered): {count}/{len(stores)} 条")
        return count

    def save_uncertain(self, category: str, stores: list[dict]) -> int:
        """保存平台检测时被拦截/无法确认的店铺（filter_status=uncertain，供人工复核）

        这些站点不是确认非 Shopify（可能只是被 WAF/网络拦截的真店），
        单独保存为"待确认"，后续可换网络/时间人工复核，避免被静默误杀。

        stores 中每个 dict 应包含:
            url, domain, platform, product_count, store_name, currency, category, search_query
        """
        if not stores:
            return 0
        col = self.unfiltered_col(category)
        self.ensure_indexes(category)
        ts = datetime.utcnow().isoformat()
        count = 0
        for store in stores:
            result = col.update_one(
                {"domain": store["domain"]},
                {"$set": {
                    "domain": store["domain"],
                    "url": store["url"],
                    "platform": store.get("platform", "Blocked/待确认"),
                    "product_count": store.get("product_count", 0),
                    "store_name": store.get("store_name", ""),
                    "currency": store.get("currency", "USD"),
                    "category": category,
                    "search_query": store.get("search_query", ""),
                    "source": store.get("source", "google_search"),
                    "filter_status": FILTER_STATUS_UNCERTAIN,
                    "updated_at": ts,
                }, "$setOnInsert": {
                    "created_at": ts,
                }},
                upsert=True,
            )
            if result.upserted_id or result.modified_count > 0:
                count += 1
        log.info(f"MongoDB 写入 {category} (uncertain): {count}/{len(stores)} 条")
        return count

    # ── 筛选状态迁移（原 move_to_filtered，改为同集合更新） ──

    def move_to_filtered(self, category: str, domain: str, filtered_data: dict, subcategory: str = "") -> bool:
        """将一条记录从 unfiltered 集合迁移到 filtered 集合（跨集合，保留原逻辑）

        单一集合模式下：从 {category} 集合读取，写入 {category}__{subcategory} 集合，
        并在目标文档中设置 filter_status=filtered。

        Args:
            category: 一级分类名称
            domain: 店铺域名
            filtered_data: 包含 filtered_category, confidence, matched_signals
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        prefix = make_collection_prefix(category, subcategory)
        uf = self.unfiltered_col(category)
        ff = self.filtered_col(category, subcategory)

        doc = uf.find_one({"domain": domain})
        if not doc:
            log.warning(f"未找到待筛选记录: {domain} (from {category})")
            return False

        ts = datetime.utcnow().isoformat()
        new_doc = {
            "domain": doc["domain"],
            "url": doc["url"],
            "platform": doc["platform"],
            "product_count": doc.get("product_count", 0),
            "store_name": doc.get("store_name", ""),
            "currency": doc.get("currency", "USD"),
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "filtered_category": filtered_data.get("filtered_category", ""),
            "confidence": filtered_data.get("confidence", 0),
            "matched_signals": filtered_data.get("matched_signals", []),
            "classified_from": category,
            "filter_status": FILTER_STATUS_FILTERED,
            "crawl_status": CRAWL_STATUS_UNCRAWLED,
            "filtered_at": ts,
            "created_at": doc.get("created_at", ts),
        }

        result = ff.update_one(
            {"domain": domain, "collection_handle": new_doc.get("collection_handle", "")},
            {"$set": new_doc},
            upsert=True,
        )
        deleted = uf.delete_one({"domain": domain}).deleted_count
        # 计数器：unfiltered -1（仅真正删除时）, filtered(subcategory) +1（仅新插入时计文档数）
        self._set_counter_type(prefix, "filtered", category, normalize_subcategory(subcategory))
        if deleted > 0:
            self._inc_counter(category, "unfiltered", -1, doc_delta=-1)
        if result.upserted_id:
            self._inc_counters(prefix, {"filtered": 1, "uncrawled": 1}, doc_delta=1)
        log.info(f"已移动: {domain} -> {prefix}")
        return True

    # ── 爬取状态迁移（原 move_to_crawled，改为同集合更新） ──

    def move_to_crawled(self, category: str, url: str, crawl_info: dict = None, subcategory: str = "") -> bool:
        """将单条 URL 标记为已爬取（同集合内更新 crawl_status，不再跨集合移动）

        Args:
            category: 一级分类名称
            url: collection URL
            crawl_info: 爬取信息（商品数、爬取时间等）
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            是否成功更新
        """
        prefix = make_collection_prefix(category, subcategory)
        col = self.filtered_col(category, subcategory)

        ts = datetime.utcnow().isoformat()
        success = crawl_info.get("success", False) if crawl_info else False
        products = crawl_info.get("products", 0) if crawl_info else 0
        new_status = CRAWL_STATUS_CRAWLED if success else CRAWL_STATUS_FAILED

        # 先查旧状态，仅在状态真正变化时迁移计数（避免重复爬取反复扣减 uncrawled）
        old_doc = col.find_one({"url": url}, {"crawl_status": 1, "_id": 0})
        old_status = old_doc.get("crawl_status") if old_doc else None

        result = col.update_one(
            {"url": url},
            {"$set": {
                "crawl_status": new_status,
                "crawl_time": ts,
                "crawl_products": products,
                "crawl_success": success,
            }}
        )
        if result.modified_count > 0 or (old_doc and old_status == new_status):
            if old_status is not None and old_status != new_status:
                self._inc_counters(prefix, {old_status: -1, new_status: 1})
            log.info(f"已标记爬取: {url} -> {prefix}")
            return True
        return False

    def move_to_crawled_batch(self, category: str, url_crawl_info_list: list[dict], subcategory: str = "") -> int:
        """批量将 URL 标记为已爬取（同集合内更新 crawl_status，不再跨集合移动）

        Args:
            category: 一级分类名称
            url_crawl_info_list: [{"url": str, "products": int, "success": bool}, ...]
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            成功更新的数量
        """
        prefix = make_collection_prefix(category, subcategory)
        col = self.filtered_col(category, subcategory)
        ts = datetime.utcnow().isoformat()

        # 先查旧状态，仅在状态真正变化时迁移计数（避免重复/未匹配 URL 导致计数漂移）
        # 同一批次内 URL 重复时只保留最后一次结果，避免一次实际更新对应多次计数迁移。
        deduped_items = {}
        for item in url_crawl_info_list:
            url = item.get("url", "")
            if url:
                deduped_items[url] = item
        url_crawl_info_list = list(deduped_items.values())
        all_urls = list(deduped_items)
        if not all_urls:
            return 0
        old_status_map = {
            doc["url"]: doc.get("crawl_status")
            for doc in col.find({"url": {"$in": all_urls}}, {"url": 1, "crawl_status": 1, "_id": 0})
        }

        # 构建批量更新操作
        from pymongo import UpdateOne
        operations = []
        valid_items = []
        for item in url_crawl_info_list:
            url = item.get("url", "")
            if not url:
                continue
            valid_items.append(item)
            success = item.get("success", False)
            products = item.get("products", 0)
            operations.append(UpdateOne(
                {"url": url},
                {"$set": {
                    "crawl_status": CRAWL_STATUS_CRAWLED if success else CRAWL_STATUS_FAILED,
                    "crawl_time": ts,
                    "crawl_products": products,
                    "crawl_success": success,
                }}
            ))

        if not operations:
            return 0

        result = col.bulk_write(operations, ordered=False)
        moved_count = result.modified_count or 0

        # 按实际旧状态 -> 新状态统计迁移量
        status_moves: dict[tuple, int] = {}
        for item in valid_items:
            url = item.get("url", "")
            old_status = old_status_map.get(url)
            if old_status is None or old_status == "":
                continue
            new_status = CRAWL_STATUS_CRAWLED if item.get("success", False) else CRAWL_STATUS_FAILED
            if old_status != new_status:
                status_moves[(old_status, new_status)] = status_moves.get((old_status, new_status), 0) + 1
        if status_moves:
            incs: dict[str, int] = {}
            for (old_s, new_s), n in status_moves.items():
                incs[old_s] = incs.get(old_s, 0) - n
                incs[new_s] = incs.get(new_s, 0) + n
            self._inc_counters(prefix, incs)
            log.info(f"批量标记爬取完成: {moved_count} 条URL -> {prefix}")
        return moved_count

    def get_crawled_urls(self, category: str, limit: int = 100, skip: int = 0, subcategory: str = "") -> list[dict]:
        """获取已爬取的 URL（查询 crawl_status=crawled 的文档）"""
        col = self.crawled_col(category, subcategory)
        docs = col.find({"crawl_status": CRAWL_STATUS_CRAWLED}, {"_id": 0}).sort("crawl_time", -1).skip(skip).limit(limit)
        return list(docs)

    def get_crawled_count(self, category: str, subcategory: str = "") -> int:
        """获取已爬取 URL 数量（从 _counters 读取，O(1)）"""
        prefix = make_collection_prefix(category, subcategory)
        doc = self._counters_col().find_one({"_id": prefix}, {"counts.crawled": 1, "_id": 0})
        return (doc.get("counts", {}) or {}).get("crawled", 0) if doc else 0

    # ── AI 分类（filtered_failed 统一集合） ──────────────

    def filtered_failed_col(self) -> Collection:
        """获取统一的 filtered_failed 集合（所有类目共用）"""
        return self.db[FILTERED_FAILED_COLLECTION]

    def ensure_filtered_failed_indexes(self):
        """为 filtered_failed 集合创建索引"""
        col = self.filtered_failed_col()
        col.create_index([("domain", ASCENDING), ("category", ASCENDING)], name="idx_domain_category")
        col.create_index([("category", ASCENDING)], name="idx_category")
        col.create_index([("reason", ASCENDING)], name="idx_reason")
        col.create_index([("filter_status", ASCENDING)], name="idx_filter_status")
        col.create_index([("created_at", ASCENDING)], name="idx_created_at")
        log.info(f"索引已创建: {FILTERED_FAILED_COLLECTION}")

    def save_filtered_failed(
        self,
        category: str,
        domain: str,
        store_url: str,
        reason: str,
        subcategory_raw: str = "",
        language: str = "",
        black_five_type: str = "",
        source: str = "ai_classifier",
        extra: Optional[dict] = None,
    ) -> bool:
        """将过滤失败记录（非英文/黑五类/综合站/类目不匹配/无法识别）写入统一的 filtered_failed 集合

        并从 {category} unfiltered 集合中删除该域名。

        Args:
            category: 原一级分类名称
            domain: 店铺域名
            store_url: 店铺完整 URL
            reason: 过滤原因（见 FILTER_FAIL_REASON_* 常量）
            subcategory_raw: 原始子分类路径
            language: 检测到的语言代码（非英文站时填充）
            black_five_type: 黑五类具体类型（黑五类时填充）
            source: 数据来源标识
            extra: 额外字段

        Returns:
            是否成功写入
        """
        col = self.filtered_failed_col()
        self.ensure_filtered_failed_indexes()
        ts = datetime.utcnow().isoformat()

        old_doc = col.find_one(
            {"domain": domain, "category": category}, {"reason": 1, "_id": 0}
        )
        old_reason = old_doc.get("reason") if old_doc else None

        doc = {
            "domain": domain,
            "store_url": store_url,
            "category": category,
            "reason": reason,
            "subcategory_raw": subcategory_raw,
            "language": language,
            "black_five_type": black_five_type,
            "source": source,
            "filter_status": FILTER_STATUS_FAILED,
            "updated_at": ts,
        }
        if extra:
            doc.update(extra)

        result = col.update_one(
            {"domain": domain, "category": category},
            {"$set": doc, "$setOnInsert": {"created_at": ts}},
            upsert=True,
        )

        # 从 unfiltered 集合删除
        uf = self.unfiltered_col(category)
        deleted = uf.delete_one({"domain": domain}).deleted_count
        # 计数器：unfiltered -1（仅真正删除时）, filtered_failed +1(按reason，仅新插入时)
        self._set_counter_type(FILTERED_FAILED_COLLECTION, "filtered_failed")
        if deleted > 0:
            self._inc_counter(category, "unfiltered", -1, doc_delta=-1)
        if result.upserted_id:
            self._inc_counters(FILTERED_FAILED_COLLECTION,
                               {"filter_failed": 1, reason: 1}, doc_delta=1)
        elif old_reason and old_reason != reason:
            # 已存在记录的 reason 变化：迁移按 reason 的细分计数
            self._inc_counters(FILTERED_FAILED_COLLECTION,
                               {old_reason: -1, reason: 1})
        log.info(f"filtered_failed 写入: {domain} (category={category}, reason={reason})")
        return True

    def save_ai_classified(
        self,
        category: str,
        domain: str,
        subcategory: str,
        store_url: str,
        source_subcategory: str = "",
        from_category: str = "",
    ) -> bool:
        """将 AI 分类成功的记录写入 {category}__{subcategory} 集合

        设置 source="ai_classifier"，filter_status=filtered。
        并从 unfiltered 集合中删除该域名。

        Args:
            category: 一级分类名称（QMDS 简化名）
            domain: 店铺域名
            subcategory: 二级分类名称（经 normalize_subcategory 标准化）
            store_url: 店铺完整 URL
            source_subcategory: 原始子分类路径（如 "Hardware > Tools"）
            from_category: 原一级分类名称（跨大类迁移时指定，从该类目的 unfiltered 集合删除；
                          为空则从 category 自身的 unfiltered 集合删除）

        Returns:
            是否成功写入
        """
        subcategory_norm = normalize_subcategory(subcategory)
        col = self.filtered_col(category, subcategory_norm)
        self.ensure_indexes(category, subcategory_norm)
        ts = datetime.utcnow().isoformat()

        result = col.update_one(
            {"domain": domain, "collection_handle": ""},
            {"$set": {
                "domain": domain,
                "store_url": store_url,
                "url": store_url,
                "collection_title": "",
                "collection_handle": "",
                "category": category,
                "subcategory": subcategory_norm,
                "source": "ai_classifier",
                "source_subcategory": source_subcategory,
                "filter_status": FILTER_STATUS_FILTERED,
                "crawl_status": CRAWL_STATUS_UNCRAWLED,
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )

        # 从 unfiltered 集合删除（跨大类迁移时从原类目删除）
        uf_category = from_category or category
        uf = self.unfiltered_col(uf_category)
        deleted = uf.delete_one({"domain": domain}).deleted_count
        # 计数器：unfiltered -1（仅真正删除时）, filtered(subcategory) +1（仅新插入时）
        prefix = make_collection_prefix(category, subcategory_norm)
        self._set_counter_type(prefix, "filtered", category, subcategory_norm)
        if deleted > 0:
            self._inc_counter(uf_category, "unfiltered", -1, doc_delta=-1)
        if result.upserted_id:
            self._inc_counters(prefix, {"filtered": 1, "uncrawled": 1}, doc_delta=1)
        if from_category and from_category != category:
            log.info(f"AI 分类跨大类迁移: {domain} {from_category} -> {category}__{subcategory_norm}")
        else:
            log.info(f"AI 分类写入: {domain} -> {category}__{subcategory_norm}")
        return result.upserted_id is not None or result.modified_count > 0

    def get_filtered_failed_count(self, reason: Optional[str] = None) -> int:
        """获取 filtered_failed 集合的记录数

        Args:
            reason: 过滤原因（可选，为 None 时统计全部）

        Returns:
            记录数
        """
        query = {"reason": reason} if reason else {}
        return self.filtered_failed_col().count_documents(query)

    def get_filtered_failed_stores(
        self, limit: int = 100, skip: int = 0, reason: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[dict]:
        """获取 filtered_failed 集合的记录

        Args:
            limit: 返回记录数限制
            skip: 跳过记录数
            reason: 过滤原因（可选）
            category: 一级分类（可选）

        Returns:
            记录列表
        """
        query = {}
        if reason:
            query["reason"] = reason
        if category:
            query["category"] = category
        col = self.filtered_failed_col()
        docs = col.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
        return list(docs)

    # ── 精准类目筛选 ──────────────────────────────────────

    def get_all_urls(self, category: str) -> list[dict]:
        """从 {category} 集合获取所有未筛选且需要抓取信息的店铺 url 和 domain

        排除已抓取成功（page_info 有内容）的记录，保留 page_info 为空的记录以便重试。
        已分类（status=classified）的记录不再重复抓取。

        Returns:
            [{"url": "https://store.com", "domain": "store.com"}, ...]
        """
        col = self.unfiltered_col(category)
        docs = col.find(
            {
                "filter_status": FILTER_STATUS_UNFILTERED,
                "status": {"$ne": SITE_INFO_STATUS_CLASSIFIED},
                "$or": [
                    {"page_info": {"$exists": False}},
                    {"page_info": {}},
                    {
                        "$and": [
                            {"page_info.homepage_content": {"$in": ["", None]}},
                            {"page_info.title": {"$in": ["", None]}},
                            {"page_info.nav_categories": {"$in": [[], None]}},
                        ]
                    },
                ],
            },
            {"url": 1, "domain": 1, "_id": 0},
        )
        return [{"url": d.get("url", ""), "domain": d.get("domain", "")} for d in docs if d.get("url")]

    def save_filtered_url(self, category: str, domain: str, store_url: str,
                          collection_title: str, collection_handle: str,
                          subcategory: str = "") -> bool:
        """保存匹配到的 collection URL 到 {prefix} 集合，设置 filter_status=filtered

        Args:
            category: 一级分类名称
            domain: 店铺域名
            store_url: 店铺完整 URL
            collection_title: collection 标题
            collection_handle: collection handle
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        prefix = make_collection_prefix(category, subcategory)
        col = self.filtered_col(category, subcategory)
        self.ensure_indexes(category, subcategory)
        collection_url = f"{store_url.rstrip('/')}/collections/{collection_handle}"
        ts = datetime.utcnow().isoformat()

        result = col.update_one(
            {"domain": domain, "collection_handle": collection_handle},
            {"$set": {
                "domain": domain,
                "store_url": store_url,
                "url": collection_url,
                "collection_title": collection_title,
                "collection_handle": collection_handle,
                "category": category,
                "subcategory": normalize_subcategory(subcategory),
                "source": "collections_filter",
                "filter_status": FILTER_STATUS_FILTERED,
                "crawl_status": CRAWL_STATUS_UNCRAWLED,
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        if result.upserted_id:
            self._set_counter_type(prefix, "filtered", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, {"filtered": 1, "uncrawled": 1}, doc_delta=1)
        return result.upserted_id is not None or result.modified_count > 0

    def add_filtered_manual(self, category: str, store_url: str, collection_url: str,
                           domain: str = "", collection_title: str = "",
                           collection_handle: str = "", subcategory: str = "") -> bool:
        """手动添加单条记录到 {prefix} 集合

        Args:
            category: 一级分类名称
            store_url: 店铺 URL
            collection_url: collection 完整 URL
            domain: 店铺域名（可选，从 URL 自动提取）
            collection_title: collection 标题（可选）
            collection_handle: collection handle（可选，从 URL 自动提取）
            subcategory: 二级分类名称（可选，空字符串归入 "other"）
        """
        from urllib.parse import urlparse
        col = self.filtered_col(category, subcategory)
        self.ensure_indexes(category, subcategory)
        ts = datetime.utcnow().isoformat()

        if not domain:
            parsed = urlparse(store_url)
            domain = parsed.netloc or parsed.path.split('/')[0]

        if not collection_handle and collection_url:
            parsed = urlparse(collection_url)
            path_parts = parsed.path.split('/')
            if 'collections' in path_parts:
                idx = path_parts.index('collections')
                if idx + 1 < len(path_parts):
                    collection_handle = path_parts[idx + 1]

        if not collection_title:
            collection_title = collection_handle

        result = col.update_one(
            {"domain": domain, "collection_handle": collection_handle},
            {"$set": {
                "domain": domain,
                "store_url": store_url,
                "url": collection_url,
                "collection_title": collection_title,
                "collection_handle": collection_handle,
                "category": category,
                "subcategory": normalize_subcategory(subcategory),
                "source": "manual",
                "filter_status": FILTER_STATUS_FILTERED,
                "crawl_status": CRAWL_STATUS_UNCRAWLED,
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        if result.upserted_id:
            sub = normalize_subcategory(subcategory)
            self._set_counter_type(make_collection_prefix(category, sub), "filtered", category, sub)
            self._inc_counters(make_collection_prefix(category, sub),
                               {"filtered": 1, "uncrawled": 1}, doc_delta=1)
        return result.upserted_id is not None or result.modified_count > 0

    def add_filtered_batch(self, category: str, urls: list[dict], subcategory: str = "") -> dict:
        """批量添加记录到 {prefix} 集合

        Args:
            category: 一级分类名称
            urls: [{"store_url": str, "collection_url": str, "subcategory": str}, ...]
                  支持纯域名、网站首页URL、集合URL。每条可单独指定 subcategory，
                  未指定时使用方法参数 subcategory。
            subcategory: 默认二级分类名称（空字符串归入 "other"）

        Returns:
            {"created": int, "updated": int, "errors": list}
        """
        from urllib.parse import urlparse
        ts = datetime.utcnow().isoformat()
        created = 0
        updated = 0
        errors = []
        created_subs: list[str] = []

        for item in urls:
            try:
                store_url = item.get("store_url", "").strip()
                collection_url = item.get("collection_url", "").strip()
                # 优先使用单条记录的 subcategory，回退到方法参数
                item_sub = item.get("subcategory", "").strip() or subcategory
                col = self.filtered_col(category, item_sub)
                self.ensure_indexes(category, item_sub)

                # 如果两个都没有，跳过
                if not store_url and not collection_url:
                    errors.append(f"缺少 URL: {item}")
                    continue

                # 解析域名
                domain = item.get("domain", "").strip()

                if collection_url:
                    # 集合链接
                    if not store_url:
                        parsed = urlparse(collection_url)
                        store_url = f"{parsed.scheme}://{parsed.netloc}"
                    if not domain:
                        parsed = urlparse(store_url)
                        domain = parsed.netloc or parsed.path.split('/')[0]

                    collection_handle = item.get("collection_handle", "").strip()
                    if not collection_handle:
                        parsed = urlparse(collection_url)
                        path_parts = parsed.path.split('/')
                        if 'collections' in path_parts:
                            idx = path_parts.index('collections')
                            if idx + 1 < len(path_parts):
                                collection_handle = path_parts[idx + 1]

                    collection_title = item.get("collection_title", "").strip()
                    if not collection_title:
                        collection_title = collection_handle

                    result = col.update_one(
                        {"domain": domain, "collection_handle": collection_handle},
                        {"$set": {
                            "domain": domain,
                            "store_url": store_url,
                            "url": collection_url,
                            "collection_title": collection_title,
                            "collection_handle": collection_handle,
                            "category": category,
                            "subcategory": normalize_subcategory(item_sub),
                            "source": "manual",
                            "filter_status": FILTER_STATUS_FILTERED,
                            "crawl_status": CRAWL_STATUS_UNCRAWLED,
                            "updated_at": ts,
                        }, "$setOnInsert": {
                            "created_at": ts,
                        }},
                        upsert=True,
                    )
                    if result.upserted_id:
                        created += 1
                        created_subs.append(item_sub)
                    elif result.modified_count > 0:
                        updated += 1
                else:
                    # 仅店铺URL（无 collection）
                    if not domain:
                        parsed = urlparse(store_url)
                        domain = parsed.netloc or parsed.path.split('/')[0]
                    collection_url = store_url
                    collection_handle = ""
                    collection_title = ""

                    result = col.update_one(
                        {"domain": domain, "collection_handle": ""},
                        {"$set": {
                            "domain": domain,
                            "store_url": store_url,
                            "url": collection_url,
                            "collection_title": collection_title,
                            "collection_handle": collection_handle,
                            "category": category,
                            "subcategory": normalize_subcategory(item_sub),
                            "source": "manual",
                            "filter_status": FILTER_STATUS_FILTERED,
                            "crawl_status": CRAWL_STATUS_UNCRAWLED,
                            "updated_at": ts,
                        }, "$setOnInsert": {
                            "created_at": ts,
                        }},
                        upsert=True,
                    )
                    if result.upserted_id:
                        created += 1
                        created_subs.append(item_sub)
                    elif result.modified_count > 0:
                        updated += 1
            except Exception as e:
                errors.append(f"添加失败: {item} - {e}")

        log.info(f"批量添加 [{category}]: 新增 {created}, 更新 {updated}, 错误 {len(errors)}")
        if created_subs:
            # 仅对新插入（upsert）的条目增加计数，避免已存在条目被重复计数
            sub_counts: dict[str, int] = {}
            for item_sub in created_subs:
                prefix = make_collection_prefix(category, item_sub)
                sub_counts[prefix] = sub_counts.get(prefix, 0) + 1
            for prefix, n in sub_counts.items():
                self._set_counter_type(prefix, "filtered", category, normalize_subcategory(prefix.split("__")[-1] if "__" in prefix else "other"))
                self._inc_counters(prefix, {"filtered": n, "uncrawled": n}, doc_delta=n)
        return {"created": created, "updated": updated, "errors": errors}

    # ── 查询 ──────────────────────────────────────────────

    def list_categories(self) -> list[str]:
        """列出所有有 unfiltered 数据的一级分类（{category} 集合，不含 __ 分隔符）"""
        categories = set()
        for name in self.db.list_collection_names():
            if name.startswith("system.") or name.startswith("_"):
                continue
            # 排除内部固定集合（计数器 / 过滤失败 / 综合站 / 信息缓存，非类目集合）
            if name in (COUNTERS_COLLECTION, FILTERED_FAILED_COLLECTION,
                        settings.comprehensive_collection, settings.shopify_url_info_collection):
                continue
            # 排除带旧后缀的集合（避免误识别为一级分类）
            if name.endswith("_filtered") or name.endswith("_crawled") or name.endswith("_unfiltered"):
                continue
            # 单一集合：不含 __ 分隔符的视为一级分类的 unfiltered 集合
            if "__" not in name:
                categories.add(name)
        return sorted(categories)

    def list_filtered_categories(self) -> list[str]:
        """列出所有 filtered 数据的分类前缀（{category}__{subcategory}）

        返回前缀列表，如 ["electronics__headphones", "electronics__other", ...]
        """
        prefixes = set()
        for name in self.db.list_collection_names():
            if name.startswith("system."):
                continue
            # 排除带旧后缀的集合（避免误识别为 filtered 前缀）
            if name.endswith("_filtered") or name.endswith("_crawled") or name.endswith("_unfiltered"):
                continue
            # 单一集合：包含 __ 分隔符的视为 filtered 集合
            if "__" in name:
                prefixes.add(name)
        return sorted(prefixes)

    def list_filtered_categories_with_sub(self) -> list[dict]:
        """列出所有 filtered 数据的分类（包含一级和二级分类信息）

        返回: [{"category": "electronics", "subcategory": "headphones", "prefix": "electronics__headphones"}, ...]
        """
        results = []
        for prefix in self.list_filtered_categories():
            cat, sub = parse_collection_prefix(prefix)
            results.append({"category": cat, "subcategory": sub, "prefix": prefix})
        return results

    def list_filtered_subcategories(self, category: str) -> list[str]:
        """列出指定一级分类下所有有 filtered 数据的二级分类

        Args:
            category: 一级分类名称

        Returns:
            二级分类列表，如 ["headphones", "other", "speakers"]
        """
        subcategories = set()
        for prefix in self.list_filtered_categories():
            cat, sub = parse_collection_prefix(prefix)
            if cat == category:
                subcategories.add(sub)
        return sorted(subcategories)

    def get_filtered_urls(self, category: str, subcategory: str = "") -> list[dict]:
        """从 {prefix} 集合获取所有未爬取的 filtered URL

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            [{"url": "https://store.com/collections/xxx", "domain": "store.com"}, ...]
        """
        col = self.filtered_col(category, subcategory)
        docs = col.find(
            {"filter_status": FILTER_STATUS_FILTERED, "crawl_status": CRAWL_STATUS_UNCRAWLED},
            {"url": 1, "domain": 1, "_id": 0}
        )
        return [{"url": d.get("url", ""), "domain": d.get("domain", "")} for d in docs if d.get("url")]

    def get_stats(self, category: str, subcategory: str = "") -> dict:
        """获取指定分类的 unfiltered/filtered 数量统计（从 _counters 读取，O(1)）"""
        prefix = make_collection_prefix(category, subcategory)
        uf_doc = self._counters_col().find_one({"_id": category}, {"counts": 1, "_id": 0})
        ff_doc = self._counters_col().find_one({"_id": prefix}, {"counts": 1, "_id": 0})
        uf_counts = (uf_doc.get("counts", {}) or {}) if uf_doc else {}
        ff_counts = (ff_doc.get("counts", {}) or {}) if ff_doc else {}
        return {
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "unfiltered": uf_counts.get("unfiltered", 0),
            "filtered": ff_counts.get("filtered", 0),
        }

    def get_unfiltered_stores(self, category: str, limit: int = 100, skip: int = 0) -> list[dict]:
        """获取指定类目的 unfiltered 店铺数据（仅 filter_status=unfiltered）

        Args:
            category: 类目名称
            limit: 返回记录数限制
            skip: 跳过记录数（分页用）

        Returns:
            店铺数据列表
        """
        col = self.unfiltered_col(category)
        docs = col.find({"filter_status": FILTER_STATUS_UNFILTERED}, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
        return list(docs)

    def get_unfiltered_count(self, category: str) -> int:
        """获取指定类目的 unfiltered 数据总数（从 _counters 读取，O(1)）"""
        doc = self._counters_col().find_one({"_id": category}, {"counts.unfiltered": 1, "_id": 0})
        return (doc.get("counts", {}) or {}).get("unfiltered", 0) if doc else 0

    # ── 单条记录操作（CRUD） ──────────────────────────────

    def get_unfiltered_by_domain(self, category: str, domain: str) -> Optional[dict]:
        """根据域名获取单条 unfiltered 记录"""
        col = self.unfiltered_col(category)
        doc = col.find_one({"domain": domain}, {"_id": 0})
        return doc

    def add_unfiltered(self, category: str, store_data: dict) -> bool:
        """添加单条 unfiltered 记录"""
        col = self.unfiltered_col(category)
        self.ensure_indexes(category)
        ts = datetime.utcnow().isoformat()

        domain = store_data.get("domain", "")
        if not domain:
            return False

        result = col.update_one(
            {"domain": domain},
            {"$set": {
                "domain": domain,
                "url": store_data.get("url", f"https://{domain}"),
                "platform": store_data.get("platform", "Shopify"),
                "product_count": store_data.get("product_count", 0),
                "store_name": store_data.get("store_name", ""),
                "currency": store_data.get("currency", "USD"),
                "category": category,
                "search_query": store_data.get("search_query", ""),
                "source": store_data.get("source", "manual"),
                "filter_status": FILTER_STATUS_UNFILTERED,
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        if result.upserted_id:
            self._set_counter_type(category, "unfiltered", category)
            self._inc_counter(category, "unfiltered", 1, doc_delta=1)
        return result.upserted_id is not None or result.modified_count > 0

    def update_unfiltered(self, category: str, domain: str, update_data: dict) -> bool:
        """更新单条 unfiltered 记录"""
        col = self.unfiltered_col(category)
        ts = datetime.utcnow().isoformat()

        update_fields = {"updated_at": ts}
        allowed_fields = ["url", "platform", "product_count", "store_name", "currency", "search_query", "source"]
        for field in allowed_fields:
            if field in update_data:
                update_fields[field] = update_data[field]

        result = col.update_one(
            {"domain": domain},
            {"$set": update_fields}
        )
        return result.modified_count > 0

    def delete_unfiltered(self, category: str, domain: str) -> bool:
        """删除单条 unfiltered 记录"""
        col = self.unfiltered_col(category)
        result = col.delete_one({"domain": domain})
        if result.deleted_count > 0:
            self._inc_counter(category, "unfiltered", -1, doc_delta=-1)
        return result.deleted_count > 0

    def delete_unfiltered_many(self, category: str, domains: list[str]) -> int:
        """批量删除 unfiltered 记录"""
        col = self.unfiltered_col(category)
        result = col.delete_many({"domain": {"$in": domains}})
        count = result.deleted_count
        if count > 0:
            self._inc_counter(category, "unfiltered", -count, doc_delta=-count)
        return count

    def _sync_unfiltered_status_counters(self, category: str):
        """用实际数据校正 unfiltered 集合的 pending/classified 计数（轻量 aggregate）"""
        col = self.unfiltered_col(category)
        pending = col.count_documents({"status": SITE_INFO_STATUS_PENDING})
        classified = col.count_documents({"status": SITE_INFO_STATUS_CLASSIFIED})
        self._counters_col().update_one(
            {"_id": category},
            {"$set": {
                f"counts.{SITE_INFO_STATUS_PENDING}": pending,
                f"counts.{SITE_INFO_STATUS_CLASSIFIED}": classified,
            }},
        )

    def get_filtered_stores(self, category: str, limit: int = 100, skip: int = 0, subcategory: str = "",
                            review_status: str = "") -> list[dict]:
        """获取指定分类的 filtered 店铺数据（未爬取的）

        Args:
            category: 一级分类名称
            limit: 返回记录数限制
            skip: 跳过记录数（分页用）
            subcategory: 二级分类名称（空字符串归入 "other"）
            review_status: 人工审核状态筛选（"" 全部 / "pending" 未审核 / "kept" 已保留）

        Returns:
            店铺数据列表
        """
        col = self.filtered_col(category, subcategory)
        query = {"crawl_status": CRAWL_STATUS_UNCRAWLED}
        query.update(self._review_status_query(review_status))
        docs = col.find(query).sort("created_at", -1).skip(skip).limit(limit)
        return list(docs)

    def get_filtered_count(self, category: str, subcategory: str = "") -> int:
        """获取指定分类未爬取的 filtered 数据数量（从 _counters 读取，O(1)）。"""
        prefix = make_collection_prefix(category, subcategory)
        doc = self._counters_col().find_one({"_id": prefix}, {"counts.uncrawled": 1, "_id": 0})
        return (doc.get("counts", {}) or {}).get("uncrawled", 0) if doc else 0

    # ── 人工审核（手动筛选精准类目：逐个浏览网站后决策） ──────────

    @staticmethod
    def _review_status_query(review_status: str = "") -> dict:
        """构造人工审核状态查询条件（老数据无 review_status 字段同样视为未审核）"""
        if review_status == REVIEW_STATUS_KEPT:
            return {"review_status": REVIEW_STATUS_KEPT}
        if review_status == REVIEW_STATUS_PENDING:
            return {"review_status": {"$ne": REVIEW_STATUS_KEPT}}
        return {}

    def get_filtered_review_queue(self, category: str, subcategory: str = "",
                                  review_status: str = REVIEW_STATUS_PENDING,
                                  limit: int = 500, skip: int = 0) -> list[dict]:
        """获取人工审核队列（按域名 + collection 排序，同一店铺的链接相邻便于逐个浏览）

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称（空字符串归入 "other"）
            review_status: "" 全部 / "pending" 未审核（默认）/ "kept" 已保留
            limit: 队列长度上限
            skip: 跳过记录数

        Returns:
            记录列表（含 _id / domain / url / collection_title / collection_handle / review_status）
        """
        col = self.filtered_col(category, subcategory)
        query = {"crawl_status": CRAWL_STATUS_UNCRAWLED}
        query.update(self._review_status_query(review_status))
        docs = col.find(query).sort([("domain", ASCENDING), ("collection_handle", ASCENDING)]) \
            .skip(skip).limit(limit)
        return list(docs)

    def get_filtered_review_counts(self, category: str, subcategory: str = "") -> dict:
        """统计人工审核进度（口径：crawl_status=uncrawled）

        Returns:
            {"total": n, "kept": n, "pending": n}
        """
        col = self.filtered_col(category, subcategory)
        base = {"crawl_status": CRAWL_STATUS_UNCRAWLED}
        total = col.count_documents(base)
        kept = col.count_documents({**base, "review_status": REVIEW_STATUS_KEPT})
        return {"total": total, "kept": kept, "pending": total - kept}

    def mark_filtered_review(self, category: str, subcategory: str, doc_ids: list,
                             review_status: str = REVIEW_STATUS_KEPT) -> dict:
        """批量标记人工审核结果（保留 / 取消保留），同步 _counters.kept

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称（空字符串归入 "other"）
            doc_ids: 文档 _id 字符串列表
            review_status: REVIEW_STATUS_KEPT 保留 / REVIEW_STATUS_PENDING 取消保留

        Returns:
            {"marked": 实际变更条数, "unchanged": 状态未变的条数}
        """
        from bson import ObjectId

        status = REVIEW_STATUS_KEPT if review_status == REVIEW_STATUS_KEPT else REVIEW_STATUS_PENDING
        col = self.filtered_col(category, subcategory)
        object_ids = []
        for doc_id in doc_ids:
            try:
                object_ids.append(ObjectId(doc_id))
            except Exception:
                continue
        if not object_ids:
            return {"marked": 0, "unchanged": 0}

        docs = list(col.find({"_id": {"$in": object_ids}},
                             {"review_status": 1, "subcategory": 1}))
        ts = datetime.utcnow().isoformat()
        changed_ids = []
        transitions: dict[str, int] = {}
        for doc in docs:
            old_status = doc.get("review_status") or REVIEW_STATUS_PENDING
            if old_status == status:
                continue
            changed_ids.append(doc["_id"])
            prefix = make_collection_prefix(category, doc.get("subcategory") or subcategory)
            delta = 1 if status == REVIEW_STATUS_KEPT else -1
            transitions[prefix] = transitions.get(prefix, 0) + delta

        if changed_ids:
            if status == REVIEW_STATUS_KEPT:
                update = {"$set": {"review_status": status, "review_time": ts, "updated_at": ts}}
            else:
                update = {"$set": {"updated_at": ts}, "$unset": {"review_status": "", "review_time": ""}}
            col.update_many({"_id": {"$in": changed_ids}}, update)
            for prefix, delta in transitions.items():
                if delta:
                    self._inc_counters(prefix, {"kept": delta})

        return {"marked": len(changed_ids), "unchanged": len(docs) - len(changed_ids)}

    def move_filtered_records(self, category: str, doc_ids: list, subcategory: str,
                              target_category: str, target_subcategory: str) -> dict:
        """把 filtered 记录迁移到目标集合（支持跨一级/二级分类）

        规则：目标集合写入（同 domain+collection_handle 已存在则合并覆盖），
        源集合删除，两侧 _counters 同步修正（total / filtered / crawl_status / kept）。

        Args:
            category: 源一级分类名称
            doc_ids: 文档 _id 字符串列表
            subcategory: 源二级分类名称（空字符串归入 "other"）
            target_category: 目标一级分类名称
            target_subcategory: 目标二级分类名称（空字符串归入 "other"）

        Returns:
            {"moved": 新增到目标集合的条数, "merged": 合并进目标已有记录的条数,
             "errors": [失败原因, ...]}
        """
        from bson import ObjectId

        target_sub = normalize_subcategory(target_subcategory)
        source_sub = normalize_subcategory(subcategory)
        if target_category == category and target_sub == source_sub:
            return {"moved": 0, "merged": 0, "errors": ["目标类目与当前类目相同，未执行迁移"]}

        src_col = self.filtered_col(category, subcategory)
        tgt_col = self.filtered_col(target_category, target_sub)
        self.ensure_indexes(target_category, target_sub)
        tgt_prefix = make_collection_prefix(target_category, target_sub)
        ts = datetime.utcnow().isoformat()

        moved = 0
        merged = 0
        errors: list[str] = []
        for doc_id in doc_ids:
            try:
                oid = ObjectId(doc_id)
            except Exception:
                errors.append(f"非法记录 ID: {doc_id}")
                continue
            doc = src_col.find_one({"_id": oid})
            if not doc:
                errors.append(f"记录不存在: {doc_id}")
                continue

            src_prefix = make_collection_prefix(category, doc.get("subcategory") or subcategory)
            crawl_status = doc.get("crawl_status", CRAWL_STATUS_UNCRAWLED)
            new_doc = {k: v for k, v in doc.items() if k != "_id"}
            new_doc.update({
                "category": target_category,
                "subcategory": target_sub,
                "moved_from": src_prefix,
                "moved_at": ts,
                "updated_at": ts,
            })

            result = tgt_col.update_one(
                {"domain": new_doc.get("domain", ""),
                 "collection_handle": new_doc.get("collection_handle", "")},
                {"$set": new_doc},
                upsert=True,
            )
            if result.upserted_id:
                self._set_counter_type(tgt_prefix, "filtered", target_category, target_sub)
                incs = {"filtered": 1, crawl_status: 1}
                if new_doc.get("review_status") == REVIEW_STATUS_KEPT:
                    incs["kept"] = 1
                self._inc_counters(tgt_prefix, incs, doc_delta=1)
                moved += 1
            else:
                merged += 1

            if src_col.delete_one({"_id": oid}).deleted_count:
                decs = {"filtered": -1, crawl_status: -1}
                if doc.get("review_status") == REVIEW_STATUS_KEPT:
                    decs["kept"] = -1
                self._inc_counters(src_prefix, decs, doc_delta=-1)

        log.info(f"filtered 迁移 [{make_collection_prefix(category, source_sub)} -> {tgt_prefix}]: "
                 f"新增 {moved}, 合并 {merged}, 失败 {len(errors)}")
        return {"moved": moved, "merged": merged, "errors": errors}

    # ── 综合站集合（comprehensive_stores） ─────────────────

    def comprehensive_col(self) -> Collection:
        """获取综合站集合（所有一级分类共用的综合站集合）"""
        return self.db[settings.comprehensive_collection]

    def ensure_comprehensive_indexes(self):
        """为综合站集合创建索引"""
        col = self.comprehensive_col()
        col.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        col.create_index([("filter_status", ASCENDING)], name="idx_filter_status")
        col.create_index([("created_at", ASCENDING)], name="idx_created_at")
        log.info(f"索引已创建: {settings.comprehensive_collection}")

    def save_comprehensive_store(
        self,
        domain: str,
        store_url: str,
        subcategories: list,
        source: str = SOURCE_SHOPIFY_URL,
        source_subcategory: str = "",
    ) -> bool:
        """将综合站记录写入 comprehensive_stores 集合

        Args:
            domain: 店铺域名
            store_url: 店铺完整 URL
            subcategories: 综合站涉及的多个二级分类列表
            source: 数据来源标识
            source_subcategory: 原始子分类路径

        Returns:
            是否成功写入
        """
        col = self.comprehensive_col()
        self.ensure_comprehensive_indexes()
        ts = datetime.utcnow().isoformat()

        result = col.update_one(
            {"domain": domain},
            {"$set": {
                "domain": domain,
                "store_url": store_url,
                "url": store_url,
                "category": "综合站",
                "subcategories": subcategories,
                "source": source,
                "source_subcategory": source_subcategory,
                "filter_status": FILTER_STATUS_FILTERED,
                "crawl_status": CRAWL_STATUS_UNCRAWLED,
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        if result.upserted_id:
            self._set_counter_type(settings.comprehensive_collection, "comprehensive")
            self._inc_counters(settings.comprehensive_collection,
                               {"filtered": 1, "uncrawled": 1}, doc_delta=1)
        log.info(f"综合站写入: {domain} -> {settings.comprehensive_collection}")
        return result.upserted_id is not None or result.modified_count > 0

    def get_comprehensive_count(self) -> int:
        """获取综合站集合的记录总数（从 _counters 读取，O(1)）"""
        doc = self._counters_col().find_one({"_id": settings.comprehensive_collection}, {"counts.filtered": 1, "_id": 0})
        return (doc.get("counts", {}) or {}).get("filtered", 0) if doc else 0

    # ── shopify_url_info 集合（网站信息缓存） ───────────────

    def shopify_url_info_col(self) -> Collection:
        """获取 shopify_url_info 集合（存放从 shopify_url 库抓取的网站信息）"""
        return self.db[settings.shopify_url_info_collection]

    def get_shopify_url_info_count(self, status: str = "") -> int:
        """统计 shopify_url_info 集合数量（按状态）"""
        query = {"status": status} if status else {}
        return self.shopify_url_info_col().count_documents(query)

    # ── unfiltered 文档 page_info 操作 ─────────────────────

    def update_unfiltered_page_info(self, category: str, domain: str, page_info: dict) -> bool:
        """更新 unfiltered 文档的 page_info 和 status 字段

        Args:
            category: 一级分类名称
            domain: 店铺域名
            page_info: fetch_page_info 返回的完整页面信息 dict

        Returns:
            是否更新成功
        """
        ts = datetime.utcnow().isoformat()
        col = self.unfiltered_col(category)
        old_doc = col.find_one(
            {"domain": domain, "filter_status": FILTER_STATUS_UNFILTERED},
            {"status": 1, "_id": 0},
        )
        result = col.update_one(
            {"domain": domain, "filter_status": FILTER_STATUS_UNFILTERED},
            {"$set": {
                "page_info": page_info,
                "status": SITE_INFO_STATUS_PENDING,
                "updated_at": ts,
            }},
        )
        if result.modified_count > 0:
            # 同步 status 计数（pending/classified）：旧状态有效且变化时迁移；
            # 旧文档无 status（计数器未计入）时只加新状态
            old_status = (old_doc or {}).get("status") if old_doc else None
            if old_status != SITE_INFO_STATUS_PENDING:
                if old_status:
                    self._inc_counter(category, old_status, -1)
                self._inc_counter(category, SITE_INFO_STATUS_PENDING, 1)
        return result.modified_count > 0

    def get_unfiltered_for_classify(self, category: str) -> list[dict]:
        """获取 unfiltered 集合中 status=pending 的记录（阶段2 读取）

        Args:
            category: 一级分类名称

        Returns:
            [{"domain": str, "url": str, "page_info": dict}, ...]
        """
        col = self.unfiltered_col(category)
        docs = col.find(
            {"filter_status": FILTER_STATUS_UNFILTERED, "status": SITE_INFO_STATUS_PENDING},
            {"domain": 1, "url": 1, "page_info": 1, "_id": 0},
        )
        return list(docs)

    def mark_unfiltered_classified(self, category: str, domain: str) -> bool:
        """标记 unfiltered 文档为已分类（status=classified）"""
        ts = datetime.utcnow().isoformat()
        col = self.unfiltered_col(category)
        old_doc = col.find_one({"domain": domain}, {"status": 1, "_id": 0})
        result = col.update_one(
            {"domain": domain},
            {"$set": {"status": SITE_INFO_STATUS_CLASSIFIED, "updated_at": ts}},
        )
        if result.modified_count > 0:
            # 同步 status 计数（pending -> classified）：旧状态有效且变化时迁移；
            # 旧文档无 status（计数器未计入）时只加新状态
            old_status = (old_doc or {}).get("status") if old_doc else None
            if old_status != SITE_INFO_STATUS_CLASSIFIED:
                if old_status:
                    self._inc_counter(category, old_status, -1)
                self._inc_counter(category, SITE_INFO_STATUS_CLASSIFIED, 1)
        return result.modified_count > 0

    def get_filtered_by_id(self, category: str, doc_id: str, subcategory: str = "") -> Optional[dict]:
        """根据 _id 获取 filtered 记录

        Args:
            category: 一级分类名称
            doc_id: 文档 _id
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            文档数据或 None
        """
        from bson import ObjectId
        col = self.filtered_col(category, subcategory)
        return col.find_one({"_id": ObjectId(doc_id)})

    def update_filtered_by_id(self, category: str, doc_id: str, updates: dict, subcategory: str = "") -> bool:
        """更新 filtered 记录

        Args:
            category: 一级分类名称
            doc_id: 文档 _id
            updates: 要更新的字段
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            是否更新成功
        """
        from bson import ObjectId
        col = self.filtered_col(category, subcategory)
        updates["updated_at"] = datetime.utcnow().isoformat()
        result = col.update_one(
            {"_id": ObjectId(doc_id)},
            {"$set": updates}
        )
        return result.modified_count > 0

    def delete_filtered_by_id(self, category: str, doc_id: str, subcategory: str = "") -> bool:
        """删除 filtered 记录

        Args:
            category: 一级分类名称
            doc_id: 文档 _id
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            是否删除成功
        """
        from bson import ObjectId
        col = self.filtered_col(category, subcategory)
        doc = col.find_one({"_id": ObjectId(doc_id)},
                           {"crawl_status": 1, "subcategory": 1, "review_status": 1})
        result = col.delete_one({"_id": ObjectId(doc_id)})
        if result.deleted_count > 0 and doc:
            prefix = make_collection_prefix(category, doc.get("subcategory", "other"))
            crawl_status = doc.get("crawl_status", CRAWL_STATUS_UNCRAWLED)
            incs = {"filtered": -1, crawl_status: -1}
            if doc.get("review_status") == REVIEW_STATUS_KEPT:
                incs["kept"] = -1
            self._inc_counters(prefix, incs, doc_delta=-1)
        return result.deleted_count > 0

    def delete_filtered_many(self, category: str, doc_ids: list[str], subcategory: str = "") -> int:
        """批量删除 filtered 记录

        Args:
            category: 一级分类名称
            doc_ids: 文档 _id 列表
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            删除的记录数
        """
        from bson import ObjectId
        col = self.filtered_col(category, subcategory)
        object_ids = [ObjectId(id) for id in doc_ids]
        docs = list(col.find({"_id": {"$in": object_ids}},
                             {"crawl_status": 1, "subcategory": 1, "review_status": 1}))
        result = col.delete_many({"_id": {"$in": object_ids}})
        if result.deleted_count > 0:
            sub_counts: dict[str, dict] = {}
            for doc in docs:
                sub = doc.get("subcategory", "other")
                prefix = make_collection_prefix(category, sub)
                if prefix not in sub_counts:
                    sub_counts[prefix] = {"filtered": 0, "uncrawled": 0, "crawled": 0,
                                          "crawl_failed": 0, "kept": 0, "_deleted": 0}
                sub_counts[prefix]["filtered"] -= 1
                sub_counts[prefix][doc.get("crawl_status", CRAWL_STATUS_UNCRAWLED)] -= 1
                if doc.get("review_status") == REVIEW_STATUS_KEPT:
                    sub_counts[prefix]["kept"] -= 1
                sub_counts[prefix]["_deleted"] += 1
            for prefix, incs in sub_counts.items():
                doc_delta = incs.pop("_deleted")
                self._inc_counters(prefix, incs, doc_delta=-doc_delta)
        return result.deleted_count

    # ── 批量导入 ──────────────────────────────────────────────

    def import_from_excel(self, category: str, filepath: str) -> dict:
        """从 Excel 文件批量导入店铺数据到 {category} 集合（unfiltered）

        Excel列名支持中英文：
        - 域名/domain (必填)
        - URL/url
        - 店铺名称/store_name
        - 平台/platform
        - 商品数/product_count
        - 货币/currency

        Args:
            category: 类目名称
            filepath: Excel文件路径

        Returns:
            {"created": int, "updated": int, "skipped": int, "errors": list}
        """
        import pandas as pd
        df = pd.read_excel(filepath)
        created = 0
        updated = 0
        skipped = 0
        errors = []

        for idx, row in df.iterrows():
            try:
                domain = str(row.get("域名", "") or row.get("domain", "")).strip()
                if not domain:
                    skipped += 1
                    continue

                store_data = {
                    "domain": domain,
                    "url": str(row.get("URL", "") or row.get("url", "") or f"https://{domain}").strip(),
                    "store_name": str(row.get("店铺名称", "") or row.get("store_name", "")).strip(),
                    "platform": str(row.get("平台", "") or row.get("platform", "Shopify")).strip() or "Shopify",
                    "product_count": int(row.get("商品数", 0) or row.get("product_count", 0) or 0),
                    "currency": str(row.get("货币", "") or row.get("currency", "USD")).strip() or "USD",
                    "source": "import",
                }

                existing = self.get_unfiltered_by_domain(category, domain)
                if existing:
                    self.update_unfiltered(category, domain, store_data)
                    updated += 1
                else:
                    self.add_unfiltered(category, store_data)
                    created += 1
            except Exception as e:
                errors.append(f"第 {idx + 2} 行导入失败: {e}")

        log.info(f"Excel导入完成 [{category}]: 新增 {created}, 更新 {updated}, 跳过 {skipped}, 错误 {len(errors)}")
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        }

    # ── 模型筛站导入（cc_c.shopify_site_01） ────────────────

    def import_from_shopify_site_01(
        self,
        category: str,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site_01",
        batch_size: int = 500,
        progress_callback=None,
        stop_check=None,
    ) -> dict:
        """从 cc_c.shopify_site_01 导入模型筛站数据到 {category}__{subcategory} 集合

        流程:
            1. 将简化 category 转换为 Google Taxonomy 名称查询源集合
            2. 分批查询 extracted != true 的文档
            3. 解析 subcategory 路径最后一段作为二级分类
            4. 从 final_url 提取 store_url
            5. 写入对应的 {category}__{subcategory} 集合（upsert），设置 filter_status=filtered
            6. 更新源文档 extracted=true, extracted_at=时间戳

        Args:
            category: Shopify 简化一级分类名（如 "hardware"）
            source_db_name: 源数据库名（默认 "cc_c"）
            source_collection: 源集合名（默认 "shopify_site_01"）
            batch_size: 分批处理的批量大小
            progress_callback: 进度回调 fn(processed, total, message)
            stop_check: 停止检查 fn() -> bool，返回 True 时停止

        Returns:
            {"total": int, "imported": int, "skipped": int, "errors": list,
             "subcategory_stats": dict}
        """
        from urllib.parse import urlparse

        google_cat = get_google_category_name(category)
        src_col = self.get_collection(source_db_name, source_collection)

        # 查询条件：category 匹配且未提取过
        query = {"category": google_cat, "extracted": {"$ne": True}}
        total = src_col.count_documents(query)
        log.info(f"模型筛站导入 [{category}]: 源集合 {source_db_name}.{source_collection} "
                 f"匹配 {total} 条待提取记录（Google 分类: {google_cat}）")

        if total == 0:
            if progress_callback:
                progress_callback(0, 0, f"类目 {category} 无待提取记录")
            return {
                "total": 0,
                "imported": 0,
                "skipped": 0,
                "errors": [],
                "subcategory_stats": {},
            }

        imported = 0
        skipped = 0
        errors: list[str] = []
        subcategory_stats: dict[str, int] = {}
        processed = 0

        # 分批查询，避免一次性加载全部数据
        cursor = src_col.find(query, no_cursor_timeout=True)
        try:
            for doc in cursor:
                if stop_check and stop_check():
                    log.info(f"模型筛站导入 [{category}]: 收到停止信号，已处理 {processed}/{total}")
                    if progress_callback:
                        progress_callback(processed, total, f"已停止: {processed}/{total}")
                    break

                processed += 1
                try:
                    domain = (doc.get("domain") or "").strip()
                    final_url = (doc.get("final_url") or "").strip()
                    subcategory_raw = (doc.get("subcategory") or "").strip()

                    if not domain:
                        skipped += 1
                        errors.append(f"第 {processed} 条: 缺少 domain")
                        continue

                    # 解析 store_url: 从 final_url 提取 scheme://netloc
                    store_url = ""
                    if final_url:
                        try:
                            parsed = urlparse(final_url)
                            if parsed.scheme and parsed.netloc:
                                store_url = f"{parsed.scheme}://{parsed.netloc}"
                        except Exception:
                            pass
                    if not store_url:
                        # 回退到 https://domain
                        store_url = f"https://{domain}"

                    # 解析二级分类：取 Google Taxonomy 路径最后一段
                    subcategory = ""
                    if subcategory_raw:
                        parts = [p.strip() for p in subcategory_raw.split(">")]
                        if parts:
                            subcategory = parts[-1]

                    subcategory_norm = normalize_subcategory(subcategory)
                    subcategory_stats[subcategory_norm] = subcategory_stats.get(subcategory_norm, 0) + 1

                    # 写入对应的 {category}__{subcategory} 集合
                    ff = self.filtered_col(category, subcategory_norm)
                    self.ensure_indexes(category, subcategory_norm)
                    ts = datetime.utcnow().isoformat()
                    collection_url = store_url  # 源数据无 collection handle

                    upsert_result = ff.update_one(
                        {"domain": domain, "collection_handle": ""},
                        {"$set": {
                            "domain": domain,
                            "store_url": store_url,
                            "url": collection_url,
                            "collection_title": "",
                            "collection_handle": "",
                            "category": category,
                            "subcategory": subcategory_norm,
                            "source": "shopify_site_01",
                            "source_subcategory": subcategory_raw,
                            "filter_status": FILTER_STATUS_FILTERED,
                            "crawl_status": CRAWL_STATUS_UNCRAWLED,
                            "updated_at": ts,
                        }, "$setOnInsert": {
                            "created_at": ts,
                        }},
                        upsert=True,
                    )
                    imported += 1

                    # 仅新插入时计数，避免重跑导入时双倍计数
                    if upsert_result.upserted_id:
                        prefix = make_collection_prefix(category, subcategory_norm)
                        self._set_counter_type(prefix, "filtered", category, subcategory_norm)
                        self._inc_counters(prefix, {"filtered": 1, "uncrawled": 1}, doc_delta=1)

                    # 标记源文档为已提取
                    src_col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {
                            "extracted": True,
                            "extracted_at": ts,
                            "extracted_to": make_collection_prefix(category, subcategory_norm),
                        }},
                    )

                    if progress_callback and (processed % 50 == 0 or processed == total):
                        progress_callback(
                            processed, total,
                            f"处理中: {processed}/{total}, 已导入 {imported}",
                        )

                except Exception as e:
                    skipped += 1
                    errors.append(f"第 {processed} 条: {domain or '未知'} - {e}")
                    log.warning(f"模型筛站导入 [{category}] 第 {processed} 条失败: {e}")
        finally:
            cursor.close()

        summary = (f"模型筛站导入完成 [{category}]: 总计 {total}, 导入 {imported}, "
                   f"跳过 {skipped}, 错误 {len(errors)}")
        log.info(summary)
        if progress_callback:
            progress_callback(processed, total, summary)

        return {
            "total": total,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "subcategory_stats": subcategory_stats,
        }

    def import_all_from_shopify_site_01(
        self,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site_01",
        batch_size: int = 500,
        progress_callback=None,
        stop_check=None,
    ) -> dict:
        """批量导入所有类目：遍历 SHOPIFY_CATEGORIES 依次调用单类目导入

        Args:
            source_db_name: 源数据库名（默认 "cc_c"）
            source_collection: 源集合名（默认 "shopify_site_01"）
            batch_size: 单类目分批处理的批量大小
            progress_callback: 进度回调 fn(processed, total, message)
            stop_check: 停止检查 fn() -> bool，返回 True 时停止

        Returns:
            {"total": int, "imported": int, "skipped": int, "errors": list,
             "category_stats": dict, "subcategory_stats": dict}
        """
        from qmds.config.categories import SHOPIFY_CATEGORIES

        total_categories = len(SHOPIFY_CATEGORIES)
        log.info(f"批量模型筛站导入: 共 {total_categories} 个类目待处理")

        # 预查询所有类目的待提取总数
        src_col = self.get_collection(source_db_name, source_collection)
        google_cats = [get_google_category_name(c) for c in SHOPIFY_CATEGORIES]
        grand_total = src_col.count_documents(
            {"category": {"$in": google_cats}, "extracted": {"$ne": True}}
        )
        log.info(f"批量模型筛站导入: 总待提取记录 {grand_total} 条")
        if progress_callback:
            progress_callback(0, grand_total, f"开始批量导入: {total_categories} 个类目, 共 {grand_total} 条")

        grand_imported = 0
        grand_skipped = 0
        grand_errors: list[str] = []
        grand_processed = 0
        category_stats: dict[str, int] = {}
        subcategory_stats: dict[str, int] = {}

        for cat_idx, category in enumerate(SHOPIFY_CATEGORIES, 1):
            if stop_check and stop_check():
                log.info(f"批量模型筛站导入: 收到停止信号，已完成 {cat_idx - 1}/{total_categories} 个类目")
                if progress_callback:
                    progress_callback(
                        grand_processed, grand_total,
                        f"已停止: {cat_idx - 1}/{total_categories} 类目, 已导入 {grand_imported}",
                    )
                break

            google_cat = get_google_category_name(category)
            cat_pending = src_col.count_documents(
                {"category": google_cat, "extracted": {"$ne": True}}
            )
            log.info(f"批量导入 [{cat_idx}/{total_categories}] {category}: 待提取 {cat_pending} 条")
            if progress_callback:
                progress_callback(
                    grand_processed, grand_total,
                    f"[{cat_idx}/{total_categories}] 类目 {category}: 待提取 {cat_pending} 条",
                )

            if cat_pending == 0:
                category_stats[category] = 0
                continue

            # 包装进度回调，累加全局已处理量
            def cat_progress_cb(processed, total, message, _cat=category, _cat_idx=cat_idx,
                                _base=grand_processed):
                full_msg = f"[{_cat_idx}/{total_categories}] {_cat} - {message}"
                progress_callback(_base + processed, grand_total, full_msg)

            result = self.import_from_shopify_site_01(
                category=category,
                source_db_name=source_db_name,
                source_collection=source_collection,
                batch_size=batch_size,
                progress_callback=cat_progress_cb if progress_callback else None,
                stop_check=stop_check,
            )

            grand_processed += result["total"]
            grand_imported += result["imported"]
            grand_skipped += result["skipped"]
            grand_errors.extend(result["errors"])
            category_stats[category] = result["imported"]

            # 聚合二级分类统计（加上一级分类前缀避免重名混淆）
            for sub, cnt in result.get("subcategory_stats", {}).items():
                key = f"{category}__{sub}"
                subcategory_stats[key] = subcategory_stats.get(key, 0) + cnt

            if stop_check and stop_check():
                break

        summary = (f"批量模型筛站导入完成: 共 {total_categories} 个类目, 总计 {grand_processed}, "
                   f"导入 {grand_imported}, 跳过 {grand_skipped}, 错误 {len(grand_errors)}")
        log.info(summary)
        if progress_callback:
            progress_callback(grand_processed, grand_total, summary)

        return {
            "total": grand_processed,
            "imported": grand_imported,
            "skipped": grand_skipped,
            "errors": grand_errors,
            "category_stats": category_stats,
            "subcategory_stats": subcategory_stats,
        }

    # ── AI 智能分类（直接在 cc_c.shopify_site 源集合上操作） ────────

    def get_shopify_site_pending_for_fetch(
        self,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site",
        limit: int = 0,
    ) -> list[dict]:
        """获取 cc_c.shopify_site 中待抓取 page_info 的记录

        筛选条件（满足任一即可）：
            1. page_info 为 null/空，且 category/subcategory 也为空/不存在
               （模型未识别分类的 URL）
            2. category 为 "无法识别"（之前 AI 分类失败，需重新抓取让模型再判断）

        Args:
            source_db_name: 源数据库名
            source_collection: 源集合名
            limit: 限制返回数量（0=不限制）

        Returns:
            [{"_id", "domain", "final_url"}, ...]
        """
        src_col = self.get_collection(source_db_name, source_collection)
        query = {
            "domain": {"$exists": True, "$ne": ""},
            "$or": [
                # 情况1：page_info 为空且无 category/subcategory（未分类）
                {
                    "$or": [
                        {"page_info": None},
                        {"page_info": {"$exists": False}},
                        {"page_info": {}},
                    ],
                    "$and": [
                        {"$or": [
                            {"category": {"$in": [None, ""]}},
                            {"category": {"$exists": False}},
                        ]},
                        {"$or": [
                            {"subcategory": {"$in": [None, ""]}},
                            {"subcategory": {"$exists": False}},
                        ]},
                    ],
                },
                # 情况2：category 为 "无法识别"（之前分类失败，重新抓取）
                {"category": "无法识别"},
            ],
        }
        cursor = src_col.find(query, {"_id": 1, "domain": 1, "final_url": 1})
        if limit > 0:
            cursor = cursor.limit(limit)
        return list(cursor)

    def update_shopify_site_page_info(
        self,
        doc_id,
        page_info: dict,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site",
    ) -> bool:
        """将抓取的 page_info 写回 cc_c.shopify_site 源文档

        Args:
            doc_id: 源文档 _id
            page_info: fetch_page_info 返回的页面信息 dict
            source_db_name: 源数据库名
            source_collection: 源集合名

        Returns:
            是否更新成功
        """
        src_col = self.get_collection(source_db_name, source_collection)
        ts = datetime.utcnow().isoformat()
        result = src_col.update_one(
            {"_id": doc_id},
            {"$set": {
                "page_info": page_info,
                "page_info_fetched_at": ts,
                "updated_at": ts,
            }},
        )
        return result.modified_count > 0

    def get_shopify_site_pending_for_classify(
        self,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site",
        limit: int = 0,
    ) -> list[dict]:
        """获取 cc_c.shopify_site 中已抓取 page_info 但需要 AI 分类的记录

        筛选条件（page_info 非空，且满足以下任一）：
            1. ai_status 不存在/为空/pending，且 category/subcategory 为空（未分类）
            2. category 为 "无法识别"（之前分类失败，重新抓取后需重新分类）

        Args:
            source_db_name: 源数据库名
            source_collection: 源集合名
            limit: 限制返回数量（0=不限制）

        Returns:
            [{"_id", "domain", "final_url", "page_info"}, ...]
        """
        src_col = self.get_collection(source_db_name, source_collection)
        query = {
            "$and": [
                {"page_info": {"$exists": True, "$ne": None}},
                {"page_info": {"$ne": {}}},
                {"domain": {"$exists": True, "$ne": ""}},
                {"$or": [
                    # 情况1：未分类（ai_status 为空，且 category/subcategory 为空）
                    {
                        "$and": [
                            {"$or": [
                                {"ai_status": {"$in": [None, "pending", ""]}},
                                {"ai_status": {"$exists": False}},
                            ]},
                            {"$or": [
                                {"category": {"$in": [None, ""]}},
                                {"category": {"$exists": False}},
                            ]},
                            {"$or": [
                                {"subcategory": {"$in": [None, ""]}},
                                {"subcategory": {"$exists": False}},
                            ]},
                        ]
                    },
                    # 情况2：category 为 "无法识别"（重新分类）
                    {"category": "无法识别"},
                ]},
            ],
        }
        cursor = src_col.find(query, {"_id": 1, "domain": 1, "final_url": 1, "page_info": 1})
        if limit > 0:
            cursor = cursor.limit(limit)
        return list(cursor)

    def update_shopify_site_classification(
        self,
        doc_id,
        ai_category: str = "",
        ai_subcategory: str = "",
        ai_status: str = "classified",
        ai_black_five_type: str = "",
        ai_subcategories: list = None,
        extra: Optional[dict] = None,
        source_db_name: str = "cc_c",
        source_collection: str = "shopify_site",
    ) -> bool:
        """将 AI 分类结果写回 cc_c.shopify_site 源文档

        写入字段：
            - category: AI 判定的 Google 一级分类名（如 "Hardware"）；黑五类/综合站/无法识别留空
            - subcategory: AI 判定的完整子分类路径（如 "Hardware > Tools"）
            - ai_status: classified / non_english / black_five / comprehensive / unrecognized
            - ai_black_five_type: 黑五类具体类型
            - ai_subcategories: 综合站的多个子分类列表
            - ai_classified_at: 分类时间戳

        Args:
            doc_id: 源文档 _id
            ai_category: AI 判定的一级分类（Google 名称）
            ai_subcategory: AI 判定的子分类路径
            ai_status: 分类状态
            ai_black_five_type: 黑五类类型
            ai_subcategories: 综合站子分类列表
            extra: 额外字段
            source_db_name: 源数据库名
            source_collection: 源集合名

        Returns:
            是否更新成功
        """
        src_col = self.get_collection(source_db_name, source_collection)
        ts = datetime.utcnow().isoformat()
        update_doc = {
            "category": ai_category,
            "subcategory": ai_subcategory,
            "ai_status": ai_status,
            "ai_classified_at": ts,
            "updated_at": ts,
        }
        if ai_black_five_type:
            update_doc["ai_black_five_type"] = ai_black_five_type
        if ai_subcategories is not None:
            update_doc["ai_subcategories"] = ai_subcategories
        if extra:
            update_doc.update(extra)

        result = src_col.update_one(
            {"_id": doc_id},
            {"$set": update_doc},
        )
        return result.modified_count > 0
