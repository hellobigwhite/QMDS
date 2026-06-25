from datetime import datetime
from typing import Any, Optional

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure
from pymongo.collection import Collection

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("mongodb")


class MongoDBClient:
    """MongoDB 数据库客户端 — qmds_url_stores 专用"""

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
        """获取 {category}_unfiltered 集合"""
        return self.db[f"{category}_unfiltered"]

    def filtered_col(self, category: str) -> Collection:
        """获取 {category}_filtered 集合"""
        return self.db[f"{category}_filtered"]

    # ── 索引 ──────────────────────────────────────────────

    def ensure_indexes(self, category: str):
        """为指定类目的 unfiltered 和 filtered 集合创建索引"""
        uf = self.unfiltered_col(category)
        uf.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        uf.create_index([("platform", ASCENDING)], name="idx_platform")
        uf.create_index([("created_at", ASCENDING)], name="idx_created_at")

        ff = self.filtered_col(category)
        ff.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        ff.create_index([("filtered_category", ASCENDING)], name="idx_filtered_category")
        ff.create_index([("confidence", ASCENDING)], name="idx_confidence")
        ff.create_index([("classified_from", ASCENDING)], name="idx_classified_from")

        log.info(f"索引已创建: {category}_unfiltered, {category}_filtered")

    # ── 写入（未筛选） ────────────────────────────────────

    def save_unfiltered(self, category: str, stores: list[dict]) -> int:
        """保存未筛选数据到 {category}_unfiltered（按 domain upsert）

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
                    "updated_at": ts,
                }, "$setOnInsert": {
                    "created_at": ts,
                }},
                upsert=True,
            )
            if result.upserted_id or result.modified_count > 0:
                count += 1
        log.info(f"MongoDB 写入 {category}_unfiltered: {count}/{len(stores)} 条")
        return count

    # ── 移动（筛选后） ────────────────────────────────────

    def move_to_filtered(self, category: str, domain: str, filtered_data: dict) -> bool:
        """将一条记录从 unfiltered 移动到 filtered（后续筛选用）

        filtered_data 应包含:
            filtered_category, confidence, matched_signals
        """
        uf = self.unfiltered_col(category)
        ff = self.filtered_col(category)

        doc = uf.find_one({"domain": domain})
        if not doc:
            log.warning(f"未找到待筛选记录: {domain} (from {category}_unfiltered)")
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
            "filtered_category": filtered_data.get("filtered_category", ""),
            "confidence": filtered_data.get("confidence", 0),
            "matched_signals": filtered_data.get("matched_signals", []),
            "classified_from": f"{category}_unfiltered",
            "filtered_at": ts,
            "created_at": doc.get("created_at", ts),
        }

        ff.update_one(
            {"domain": domain},
            {"$set": new_doc},
            upsert=True,
        )
        uf.delete_one({"domain": domain})
        log.info(f"已移动: {domain} → {category}_filtered")
        return True

    # ── 精准类目筛选 ──────────────────────────────────────

    def get_all_urls(self, category: str) -> list[dict]:
        """从 {category}_unfiltered 获取所有店铺的 url 和 domain

        Returns:
            [{"url": "https://store.com", "domain": "store.com"}, ...]
        """
        col = self.unfiltered_col(category)
        docs = col.find({}, {"url": 1, "domain": 1, "_id": 0})
        return [{"url": d.get("url", ""), "domain": d.get("domain", "")} for d in docs if d.get("url")]

    def save_filtered_url(self, category: str, domain: str, store_url: str,
                          collection_title: str, collection_handle: str) -> bool:
        """保存匹配到的 collection URL 到 {category}_filtered

        Args:
            category: 类目名称
            domain: 店铺域名
            store_url: 店铺完整 URL
            collection_title: collection 标题
            collection_handle: collection handle
        """
        col = self.filtered_col(category)
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
                "source": "collections_filter",
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        return result.upserted_id is not None or result.modified_count > 0

    # ── 查询 ──────────────────────────────────────────────

    def list_categories(self) -> list[str]:
        """列出所有有 unfiltered 数据的类目"""
        categories = set()
        for name in self.db.list_collection_names():
            if name.endswith("_unfiltered"):
                categories.add(name.replace("_unfiltered", ""))
        return sorted(categories)

    def list_filtered_categories(self) -> list[str]:
        """列出所有有 filtered 数据的类目"""
        categories = set()
        for name in self.db.list_collection_names():
            if name.endswith("_filtered"):
                categories.add(name.replace("_filtered", ""))
        return sorted(categories)

    def get_filtered_urls(self, category: str) -> list[dict]:
        """从 {category}_filtered 获取所有URL
        
        Returns:
            [{"url": "https://store.com/collections/xxx", "domain": "store.com"}, ...]
        """
        col = self.filtered_col(category)
        docs = col.find({}, {"url": 1, "domain": 1, "_id": 0})
        return [{"url": d.get("url", ""), "domain": d.get("domain", "")} for d in docs if d.get("url")]

    def get_stats(self, category: str) -> dict:
        """获取指定类目的 unfiltered/filtered 数量统计"""
        uf_count = self.unfiltered_col(category).estimated_document_count()
        ff_count = self.filtered_col(category).estimated_document_count()
        return {
            "category": category,
            "unfiltered": uf_count,
            "filtered": ff_count,
        }

    def get_unfiltered_stores(self, category: str, limit: int = 100, skip: int = 0) -> list[dict]:
        """获取指定类目的 unfiltered 店铺数据
        
        Args:
            category: 类目名称
            limit: 返回记录数限制
            skip: 跳过记录数（分页用）
            
        Returns:
            店铺数据列表
        """
        col = self.unfiltered_col(category)
        docs = col.find({}, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
        return list(docs)

    def get_unfiltered_count(self, category: str) -> int:
        """获取指定类目的 unfiltered 数据总数"""
        return self.unfiltered_col(category).estimated_document_count()
