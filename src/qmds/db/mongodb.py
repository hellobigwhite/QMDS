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
    
    def crawled_col(self, category: str) -> Collection:
        """获取 {category}_crawled 集合（已爬取URL备份）"""
        return self.db[f"{category}_crawled"]

    # ── 索引 ──────────────────────────────────────────────

    def ensure_indexes(self, category: str):
        """为指定类目的 unfiltered、filtered 和 crawled 集合创建索引"""
        uf = self.unfiltered_col(category)
        uf.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        uf.create_index([("platform", ASCENDING)], name="idx_platform")
        uf.create_index([("created_at", ASCENDING)], name="idx_created_at")

        ff = self.filtered_col(category)
        # 删除旧的单字段 idx_domain 索引（如果存在）
        existing_indexes = {idx["name"]: idx for idx in ff.list_indexes()}
        if "idx_domain" in existing_indexes:
            log.info(f"删除旧索引: {category}_filtered.idx_domain")
            ff.drop_index("idx_domain")
        ff.create_index([("domain", ASCENDING), ("collection_handle", ASCENDING)], unique=True, name="idx_domain_collection")
        ff.create_index([("filtered_category", ASCENDING)], name="idx_filtered_category")
        ff.create_index([("confidence", ASCENDING)], name="idx_confidence")
        ff.create_index([("classified_from", ASCENDING)], name="idx_classified_from")

        cc = self.crawled_col(category)
        cc.create_index([("url", ASCENDING)], unique=True, name="idx_url")
        cc.create_index([("domain", ASCENDING)], name="idx_domain")
        cc.create_index([("crawled_at", ASCENDING)], name="idx_crawled_at")
        cc.create_index([("crawl_success", ASCENDING)], name="idx_crawl_success")

        log.info(f"索引已创建: {category}_unfiltered, {category}_filtered, {category}_crawled")

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

    # ── 已爬取URL备份 ──────────────────────────────────────

    def move_to_crawled(self, category: str, url: str, crawl_info: dict = None) -> bool:
        """将URL从 filtered 移动到 crawled 备份集合

        Args:
            category: 类目名称
            url: collection URL
            crawl_info: 爬取信息（商品数、爬取时间等）

        Returns:
            是否成功移动
        """
        ff = self.filtered_col(category)
        cc = self.crawled_col(category)

        doc = ff.find_one({"url": url})
        if not doc:
            log.warning(f"未找到待备份记录: {url} (from {category}_filtered)")
            return False

        ts = datetime.utcnow().isoformat()
        crawled_doc = {
            **doc,
            "crawled_at": ts,
            "crawl_products": crawl_info.get("products", 0) if crawl_info else 0,
            "crawl_success": crawl_info.get("success", False) if crawl_info else False,
            "source_collection": f"{category}_filtered",
        }
        # 移除 _id 避免冲突
        if "_id" in crawled_doc:
            del crawled_doc["_id"]

        cc.update_one(
            {"url": url},
            {"$set": crawled_doc},
            upsert=True,
        )
        ff.delete_one({"url": url})
        log.info(f"已备份: {url} → {category}_crawled")
        return True

    def move_to_crawled_batch(self, category: str, url_crawl_info_list: list[dict]) -> int:
        """批量将URL从 filtered 移动到 crawled 备份集合

        Args:
            category: 类目名称
            url_crawl_info_list: [{"url": str, "products": int, "success": bool}, ...]

        Returns:
            成功移动的数量
        """
        ff = self.filtered_col(category)
        cc = self.crawled_col(category)
        ts = datetime.utcnow().isoformat()

        moved_count = 0
        for item in url_crawl_info_list:
            url = item.get("url", "")
            if not url:
                continue

            doc = ff.find_one({"url": url})
            if not doc:
                continue

            crawled_doc = {
                **doc,
                "crawled_at": ts,
                "crawl_products": item.get("products", 0),
                "crawl_success": item.get("success", False),
                "source_collection": f"{category}_filtered",
            }
            if "_id" in crawled_doc:
                del crawled_doc["_id"]

            cc.update_one(
                {"url": url},
                {"$set": crawled_doc},
                upsert=True,
            )
            ff.delete_one({"url": url})
            moved_count += 1

        if moved_count > 0:
            log.info(f"批量备份完成: {moved_count} 条URL → {category}_crawled")
        return moved_count

    def get_crawled_urls(self, category: str, limit: int = 100, skip: int = 0) -> list[dict]:
        """获取已爬取的URL备份

        Args:
            category: 类目名称
            limit: 返回记录数限制
            skip: 跳过记录数

        Returns:
            已爬取URL列表
        """
        col = self.crawled_col(category)
        docs = col.find({}, {"_id": 0}).sort("crawled_at", -1).skip(skip).limit(limit)
        return list(docs)

    def get_crawled_count(self, category: str) -> int:
        """获取已爬取URL数量"""
        return self.crawled_col(category).estimated_document_count()

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

    def add_filtered_manual(self, category: str, store_url: str, collection_url: str,
                           domain: str = "", collection_title: str = "", collection_handle: str = "") -> bool:
        """手动添加单条记录到 {category}_filtered

        Args:
            category: 类目名称
            store_url: 店铺 URL
            collection_url: collection 完整 URL
            domain: 店铺域名（可选，从 URL 自动提取）
            collection_title: collection 标题（可选）
            collection_handle: collection handle（可选，从 URL 自动提取）
        """
        from urllib.parse import urlparse
        col = self.filtered_col(category)
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
                "source": "manual",
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        return result.upserted_id is not None or result.modified_count > 0

    def add_filtered_batch(self, category: str, urls: list[dict]) -> dict:
        """批量添加记录到 {category}_filtered

        Args:
            category: 类目名称
            urls: [{"store_url": str, "collection_url": str}, ...] 
                  支持纯域名、网站首页URL、集合URL

        Returns:
            {"created": int, "updated": int, "errors": list}
        """
        from urllib.parse import urlparse
        col = self.filtered_col(category)
        ts = datetime.utcnow().isoformat()
        created = 0
        updated = 0
        errors = []

        for item in urls:
            try:
                store_url = item.get("store_url", "").strip()
                collection_url = item.get("collection_url", "").strip()

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

                    existing = col.find_one({"domain": domain, "collection_handle": collection_handle})
                    result = col.update_one(
                        {"domain": domain, "collection_handle": collection_handle},
                        {"$set": {
                            "domain": domain,
                            "store_url": store_url,
                            "url": collection_url,
                            "collection_title": collection_title,
                            "collection_handle": collection_handle,
                            "category": category,
                            "source": "manual",
                            "updated_at": ts,
                        }, "$setOnInsert": {
                            "created_at": ts,
                        }},
                        upsert=True,
                    )
                else:
                    # 网站首页链接
                    if not domain:
                        parsed = urlparse(store_url)
                        domain = parsed.netloc or parsed.path.split('/')[0]
                    
                    existing = col.find_one({"domain": domain, "collection_handle": ""})
                    result = col.update_one(
                        {"domain": domain, "collection_handle": ""},
                        {"$set": {
                            "domain": domain,
                            "store_url": store_url,
                            "url": store_url,
                            "collection_title": "",
                            "collection_handle": "",
                            "category": category,
                            "source": "manual",
                            "updated_at": ts,
                        }, "$setOnInsert": {
                            "created_at": ts,
                        }},
                        upsert=True,
                    )

                if existing:
                    updated += 1
                else:
                    created += 1
            except Exception as e:
                errors.append(f"添加失败: {item} - {e}")

        log.info(f"批量添加 filtered [{category}]: 新增 {created}, 更新 {updated}, 错误 {len(errors)}")
        return {"created": created, "updated": updated, "errors": errors}

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

    # ── 单条记录操作（CRUD） ──────────────────────────────

    def get_unfiltered_by_domain(self, category: str, domain: str) -> Optional[dict]:
        """根据域名获取单条unfiltered记录"""
        col = self.unfiltered_col(category)
        doc = col.find_one({"domain": domain}, {"_id": 0})
        return doc

    def add_unfiltered(self, category: str, store_data: dict) -> bool:
        """添加单条unfiltered记录"""
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
                "updated_at": ts,
            }, "$setOnInsert": {
                "created_at": ts,
            }},
            upsert=True,
        )
        return result.upserted_id is not None or result.modified_count > 0

    def update_unfiltered(self, category: str, domain: str, update_data: dict) -> bool:
        """更新单条unfiltered记录"""
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
        """删除单条unfiltered记录"""
        col = self.unfiltered_col(category)
        result = col.delete_one({"domain": domain})
        return result.deleted_count > 0

    def delete_unfiltered_many(self, category: str, domains: list[str]) -> int:
        """批量删除unfiltered记录"""
        col = self.unfiltered_col(category)
        result = col.delete_many({"domain": {"$in": domains}})
        return result.deleted_count

    def get_filtered_stores(self, category: str, limit: int = 100, skip: int = 0) -> list[dict]:
        """获取指定类目的 filtered 店铺数据
        
        Args:
            category: 类目名称
            limit: 返回记录数限制
            skip: 跳过记录数（分页用）
            
        Returns:
            店铺数据列表
        """
        col = self.filtered_col(category)
        docs = col.find({}).sort("created_at", -1).skip(skip).limit(limit)
        return list(docs)

    def get_filtered_count(self, category: str) -> int:
        """获取指定类目的 filtered 数据总数"""
        return self.filtered_col(category).estimated_document_count()

    def get_filtered_by_id(self, category: str, doc_id: str) -> Optional[dict]:
        """根据 _id 获取 filtered 记录
        
        Args:
            category: 类目名称
            doc_id: 文档 _id
            
        Returns:
            文档数据或 None
        """
        from bson import ObjectId
        col = self.filtered_col(category)
        return col.find_one({"_id": ObjectId(doc_id)})

    def update_filtered_by_id(self, category: str, doc_id: str, updates: dict) -> bool:
        """更新 filtered 记录
        
        Args:
            category: 类目名称
            doc_id: 文档 _id
            updates: 要更新的字段
            
        Returns:
            是否更新成功
        """
        from bson import ObjectId
        col = self.filtered_col(category)
        updates["updated_at"] = datetime.utcnow().isoformat()
        result = col.update_one(
            {"_id": ObjectId(doc_id)},
            {"$set": updates}
        )
        return result.modified_count > 0

    def delete_filtered_by_id(self, category: str, doc_id: str) -> bool:
        """删除 filtered 记录
        
        Args:
            category: 类目名称
            doc_id: 文档 _id
            
        Returns:
            是否删除成功
        """
        from bson import ObjectId
        col = self.filtered_col(category)
        result = col.delete_one({"_id": ObjectId(doc_id)})
        return result.deleted_count > 0

    def delete_filtered_many(self, category: str, doc_ids: list[str]) -> int:
        """批量删除 filtered 记录
        
        Args:
            category: 类目名称
            doc_ids: 文档 _id 列表
            
        Returns:
            删除的记录数
        """
        from bson import ObjectId
        col = self.filtered_col(category)
        object_ids = [ObjectId(id) for id in doc_ids]
        result = col.delete_many({"_id": {"$in": object_ids}})
        return result.deleted_count

    # ── 批量导入 ──────────────────────────────────────────────

    def import_from_excel(self, category: str, filepath: str) -> dict:
        """从Excel文件批量导入店铺数据到 {category}_unfiltered

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
