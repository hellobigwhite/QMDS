"""产品数据管理数据库客户端

单一集合模式：每个 {category}__{subcategory} 前缀对应一个集合，用 clean_status / export_status 字段区分状态。
"""

import re
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

# Excel 写入前兜底清洗：移除 openpyxl 不允许写入工作表的控制字符
_ILLEGAL_EXCEL_CHARS_RE = re.compile(r'[\000-\010]|[\013-\014]|[\016-\037]')


def _clean_excel_illegal_chars(value):
    """清理 Excel/openpyxl 不允许写入的非法控制字符。"""
    if isinstance(value, str):
        value = _ILLEGAL_EXCEL_CHARS_RE.sub('', value)
        value = value.replace('\ufffd', '')
    return value


from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure, BulkWriteError
from pymongo.collection import Collection

from qmds.config import settings
from qmds.config.categories import (
    make_collection_prefix,
    parse_collection_prefix,
    normalize_subcategory,
    DEFAULT_SUBCATEGORY,
)
from qmds.utils.logger import get_logger

log = get_logger("product_db")


class _TTLCache:
    """简单的线程安全 TTL 缓存"""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}
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


# 数据库名称
PRODUCT_DB_NAME = "qmds_product_data"

# 计数器集合名（与 mongodb.py 保持一致，存放各集合的状态计数）
COUNTERS_COLLECTION = "_counters"

# 向后兼容的旧后缀常量（已废弃，仅为避免外部脚本引用报错；新代码不应使用）
RAW_SUFFIX = "_raw"
CLEAN_SUFFIX = "_clean"
EXPORT_SUFFIX = "_export"

# 清洗状态
CLEAN_STATUS_UNCLEAN = "unclean"      # 未清洗
CLEAN_STATUS_CLEANED = "cleaned"      # 已清洗
CLEAN_STATUS_FAILED = "failed"        # 清洗失败

# 导出状态
EXPORT_STATUS_UNEXPORTED = "unexported"  # 未导出
EXPORT_STATUS_EXPORTED = "exported"      # 已导出

# 单次 $in / update_many 批量操作的最大文档数（过大的批量会使 mongod 内存剧烈尖峰，曾导致 OOM 崩溃）
DB_BATCH_SIZE = 500

# 导出字段配置
EXPORT_COLUMNS = [
    "SKU", "标题", "描述", "子描述", "图片",
    "原价", "折扣价", "变体", "分类",
    "currency", "source_domain", "source_category", "source_subcategory",
]


class ProductDBClient:
    """产品数据管理数据库客户端（单一集合模式）

    数据库结构:
    - 数据库: qmds_product_data
    - 集合命名: {category}__{subcategory}（无后缀，单一集合）
    - 文档通过 clean_status / export_status 字段区分状态：
        clean_status: unclean | cleaned | failed
        export_status: unexported | exported
    - 无二级分类时使用 "other" 作为 subcategory
    """

    _stats_cache = _TTLCache(ttl_seconds=60)

    def __init__(self, uri: Optional[str] = None):
        self._uri = uri or settings.mongo_uri
        self._client: Optional[MongoClient] = None

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        return self._client

    @property
    def db(self):
        return self.client[PRODUCT_DB_NAME]

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

    # ── 计数器（_counters 集合，用 $inc 原子维护各产品集合的清洗/导出状态计数） ──

    def _counters_col(self) -> Collection:
        """获取 _counters 集合（与产品数据同库 qmds_product_data）"""
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

    def _inc_counters(self, collection_key: str, increments: dict):
        """批量增减多个状态计数（单次 $inc 操作）

        Args:
            collection_key: 集合名（如 "hardware__tools"）
            increments: {"unclean": -1, "cleaned": 1} -- 各状态的增减量
        """
        inc_doc = {f"counts.{k}": v for k, v in increments.items()}
        inc_doc["total"] = sum(increments.values())
        self._counters_col().update_one(
            {"_id": collection_key},
            {
                "$inc": inc_doc,
                "$set": {"updated_at": datetime.utcnow().isoformat()},
            },
            upsert=True,
        )

    def _rebuild_single_product_counter(self, category: str, subcategory: str = ""):
        """重建单个产品集合的计数器（用 aggregate $group 统计，覆盖写入 _counters）

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称
        """
        prefix = make_collection_prefix(category, subcategory)
        col = self.collection(category, subcategory)

        # 按 clean_status 聚合
        clean_counts = {}
        for doc in col.aggregate([{"$group": {"_id": "$clean_status", "count": {"$sum": 1}}}]):
            status = doc["_id"] if doc["_id"] is not None else "unknown"
            clean_counts[status] = doc["count"]

        # 按 export_status 聚合
        export_counts = {}
        for doc in col.aggregate([{"$group": {"_id": "$export_status", "count": {"$sum": 1}}}]):
            status = doc["_id"] if doc["_id"] is not None else "unknown"
            export_counts[status] = doc["count"]

        # 合并到 counts：clean_status 用原名，export 加前缀避免冲突
        counts = {
            CLEAN_STATUS_UNCLEAN: clean_counts.get(CLEAN_STATUS_UNCLEAN, 0),
            CLEAN_STATUS_CLEANED: clean_counts.get(CLEAN_STATUS_CLEANED, 0),
            CLEAN_STATUS_FAILED: clean_counts.get(CLEAN_STATUS_FAILED, 0),
            "exported": export_counts.get(EXPORT_STATUS_EXPORTED, 0),
            "unexported": export_counts.get(EXPORT_STATUS_UNEXPORTED, 0),
        }
        total = sum(clean_counts.values()) if clean_counts else col.estimated_document_count()

        self._counters_col().update_one(
            {"_id": prefix},
            {"$set": {
                "collection_type": "product",
                "category": category,
                "subcategory": normalize_subcategory(subcategory),
                "counts": counts,
                "total": total,
                "updated_at": datetime.utcnow().isoformat(),
            }},
            upsert=True,
        )

    def rebuild_product_counters(self) -> dict:
        """全量重建所有产品集合的计数器，修复 $inc 漂移

        遍历所有产品集合，用 aggregate $group 统计各状态计数，覆盖写入 _counters。

        Returns:
            {"rebuilt": N, "errors": [...]} -- 重建的计数器数量和错误列表
        """
        self.ensure_counters_indexes()
        # 只删除 product 类型的计数器，不影响同库可能存在的其它类型
        self._counters_col().delete_many({"collection_type": "product"})

        rebuilt = 0
        errors = []

        for item in self.list_categories_with_sub():
            category = item["category"]
            subcategory = item["subcategory"]
            try:
                self._rebuild_single_product_counter(category, subcategory)
                rebuilt += 1
            except Exception as e:
                errors.append(f"product {item['prefix']}: {e}")

        log.info(f"产品计数器重建完成: {rebuilt} 个集合, {len(errors)} 个错误")
        return {"rebuilt": rebuilt, "errors": errors}

    def get_all_collection_counts(self) -> dict:
        """一次性返回所有产品集合的计数，供 API / 脚本使用

        Returns:
            按 collection_type 分组的计数:
            {
                "product": [{"_id": "hardware__tools", "category": "...", "subcategory": "...",
                              "counts": {...}, "total": N}, ...],
                ...（可能含同库其它类型，一并返回）
            }
        """
        col = self._counters_col()
        docs = list(col.find({}, {"_id": 1, "collection_type": 1, "category": 1,
                                   "subcategory": 1, "counts": 1, "total": 1}))
        result: Dict[str, list] = {}
        for doc in docs:
            ctype = doc.get("collection_type", "unknown")
            if ctype not in result:
                result[ctype] = []
            result[ctype].append(doc)
        return result

    # ── 集合命名 ──────────────────────────────────────────

    def collection(self, category: str, subcategory: str = "") -> Collection:
        """获取 {category}__{subcategory} 单一集合"""
        prefix = make_collection_prefix(category, subcategory)
        return self.db[prefix]

    # 向后兼容方法：均返回同一集合
    def raw_col(self, category: str, subcategory: str = "") -> Collection:
        """获取单一集合（兼容旧接口，等同于 collection）"""
        return self.collection(category, subcategory)

    def clean_col(self, category: str, subcategory: str = "") -> Collection:
        """获取单一集合（兼容旧接口，等同于 collection）"""
        return self.collection(category, subcategory)

    def export_col(self, category: str, subcategory: str = "") -> Collection:
        """获取单一集合（兼容旧接口，等同于 collection）"""
        return self.collection(category, subcategory)

    def get_raw_col_name(self, category: str, subcategory: str = "") -> str:
        """获取集合名称（兼容旧接口，返回单一集合名）"""
        return make_collection_prefix(category, subcategory)

    def get_clean_col_name(self, category: str, subcategory: str = "") -> str:
        """获取集合名称（兼容旧接口，返回单一集合名）"""
        return make_collection_prefix(category, subcategory)

    def parse_category_from_col(self, col_name: str) -> Optional[str]:
        """从集合名解析出集合前缀。

        单一集合模式下，集合名即为前缀本身（不含 _raw/_clean/_export 后缀）。
        旧格式带后缀的集合名会被解析出前缀部分。
        """
        # 兼容旧格式（带后缀）
        for suffix in (RAW_SUFFIX, CLEAN_SUFFIX, EXPORT_SUFFIX):
            if col_name.endswith(suffix):
                name = col_name[:-len(suffix)]
                return name if name else None
        # 单一集合：集合名即前缀
        return col_name if col_name else None

    # ── 索引 ──────────────────────────────────────────────

    def ensure_product_indexes(self, category: str, subcategory: str = ""):
        """为产品数据单一集合创建索引（含清洗、导出字段索引）"""
        prefix = make_collection_prefix(category, subcategory)
        col = self.collection(category, subcategory)

        # 产品字段索引
        col.create_index([("unique_key", ASCENDING)], name="idx_unique_key", unique=True)
        col.create_index([("source_url", ASCENDING)], name="idx_source_url")
        col.create_index([("source_domain", ASCENDING)], name="idx_source_domain")
        col.create_index([("crawl_time", ASCENDING)], name="idx_crawl_time")
        col.create_index([("分类", ASCENDING)], name="idx_category")

        # 清洗字段索引
        col.create_index([("clean_status", ASCENDING)], name="idx_clean_status")
        col.create_index([("clean_time", ASCENDING)], name="idx_clean_time")

        # 导出字段索引
        col.create_index([("export_status", ASCENDING)], name="idx_export_status")
        col.create_index([("export_time", ASCENDING)], name="idx_export_time")
        col.create_index([("export_count", ASCENDING)], name="idx_export_count")
        col.create_index([("last_export_time", ASCENDING)], name="idx_last_export_time")

        # 状态组合查询复合索引（导出流程查 clean_status=cleaned 且 export_status=unexported，
        # 原单字段索引需扫描数万条才能返回少量结果）
        col.create_index(
            [("clean_status", ASCENDING), ("export_status", ASCENDING)],
            name="idx_clean_export_status",
        )

        # 标题索引（用于导出查询）
        col.create_index([("标题", ASCENDING)], name="idx_title")

        log.info(f"索引已创建: {prefix}")

    def ensure_export_indexes(self, category: str, subcategory: str = ""):
        """为产品数据集合创建导出相关索引（兼容旧接口）"""
        col = self.collection(category, subcategory)
        col.create_index([("标题", ASCENDING)], name="idx_title")
        col.create_index([("export_time", ASCENDING)], name="idx_export_time")
        col.create_index([("export_status", ASCENDING)], name="idx_export_status")
        prefix = make_collection_prefix(category, subcategory)
        log.info(f"导出索引已创建: {prefix}")

    # ── 写入 ──────────────────────────────────────────────

    def save_raw_products(self, category: str, subcategory: str, products: List[dict]) -> int:
        """保存原始商品数据到单一集合，初始 clean_status=unclean, export_status=unexported

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称（空字符串归入 "other"）
            products: 商品数据列表

        Returns:
            新增商品数量
        """
        if not products:
            return 0

        col = self.collection(category, subcategory)
        self.ensure_product_indexes(category, subcategory)

        # 去重：基于 unique_key
        unique_map = {}
        for product in products:
            unique_key = product.get("unique_key")
            if unique_key:
                product.setdefault("clean_status", CLEAN_STATUS_UNCLEAN)
                product.setdefault("clean_time", None)
                product.setdefault("export_status", EXPORT_STATUS_UNEXPORTED)
                product.setdefault("export_time", None)
                product.setdefault("export_count", 0)
                product.setdefault("last_export_time", None)
                unique_map[unique_key] = product

        deduped_batch = list(unique_map.values())
        if not deduped_batch:
            return 0

        # 检查已存在的记录（分批查询，避免 $in 列表过大）
        candidate_keys = list(unique_map.keys())
        existing_keys = set()
        for i in range(0, len(candidate_keys), 10000):
            batch = candidate_keys[i:i + 10000]
            for item in col.find({"unique_key": {"$in": batch}}, {"unique_key": 1}):
                existing_keys.add(item["unique_key"])
        to_insert = [item for item in deduped_batch if item["unique_key"] not in existing_keys]

        if not to_insert:
            return 0

        try:
            col.insert_many(to_insert, ordered=False)
            return len(to_insert)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", []) if exc.details else []
            return max(len(to_insert) - len(write_errors), 0)

    def save_clean_products(self, category: str, subcategory: str, products: List[dict]) -> int:
        """将清洗通过的商品在同一集合中更新状态为 cleaned（不再跨集合移动）

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称
            products: 清洗后的商品数据列表

        Returns:
            更新为 cleaned 状态的文档数量
        """
        if not products:
            return 0

        col = self.collection(category, subcategory)
        clean_time = datetime.utcnow().isoformat()

        unique_keys = [p.get("unique_key") for p in products if p.get("unique_key")]
        if not unique_keys:
            return 0

        # 在同一集合中更新 clean_status 为 cleaned（分批，避免 $in 列表过大导致 mongod 内存尖峰）
        modified = 0
        for i in range(0, len(unique_keys), DB_BATCH_SIZE):
            batch = unique_keys[i:i + DB_BATCH_SIZE]
            result = col.update_many(
                {"unique_key": {"$in": batch}},
                {"$set": {
                    "clean_status": CLEAN_STATUS_CLEANED,
                    "clean_time": clean_time,
                }}
            )
            modified += result.modified_count
        self._stats_cache.invalidate()
        return modified

    # ── 清洗操作 ──────────────────────────────────────────

    def clean_category(self, category: str, subcategory: str = "", force: bool = False) -> Dict[str, int]:
        """清洗指定分类的未清洗数据（在同一集合内更新状态，不跨集合移动）

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称
            force: 是否强制清洗所有数据（包括已清洗的）

        Returns:
            {"processed": 处理数量, "cleaned": 清洗后数量, "removed": 移除数量}
        """
        from qmds.modules.data_scraper.pipeline.filters import (
            PLACEHOLDER_IMAGES, PROHIBITED_KEYWORDS, MIN_TITLE_LENGTH, MIN_PRICE, MAX_PRICE
        )
        from qmds.utils.language import is_non_english_text

        prefix = make_collection_prefix(category, subcategory)
        col = self.collection(category, subcategory)

        # 只查询未清洗的数据（除非强制清洗）
        if force:
            query = {}
        else:
            query = {"clean_status": CLEAN_STATUS_UNCLEAN}

        # 只投影清洗所需的字段，避免大字段（描述/HTML 等）撑爆内存
        projection = {
            "_id": 0,
            "unique_key": 1,
            "折扣价": 1,
            "原价": 1,
            "标题": 1,
            "图片": 1,
            "描述": 1,
            "子描述": 1,
        }

        clean_time = datetime.utcnow().isoformat()
        passed_keys = []
        all_keys = set()
        stats = {"价格超范围": 0, "标题过短": 0, "占位图": 0, "非英文": 0, "违禁词": 0, "无key": 0}

        # 流式遍历游标（按批次从服务器拉取），避免一次性 list() 把全部文档加载进内存导致 MemoryError
        total = 0
        cursor = col.find(query, projection).batch_size(2000)
        for p in cursor:
            total += 1
            unique_key = p.get("unique_key", "")
            if not unique_key:
                stats["无key"] += 1
                continue
            all_keys.add(unique_key)

            # ── 价格 ──
            discount_price = p.get("折扣价")
            original_price = p.get("原价")
            price = 0.0
            if discount_price not in (None, ""):
                price = float(discount_price)
            elif original_price not in (None, ""):
                price = float(original_price)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                stats["价格超范围"] += 1
                continue

            # ── 标题长度 ──
            title = str(p.get("标题", "") or "").strip()
            if len(title) < MIN_TITLE_LENGTH:
                stats["标题过短"] += 1
                continue

            # ── 图片有效性 ──
            img = str(p.get("图片", "") or "").strip()
            if not img or PLACEHOLDER_IMAGES.search(img):
                stats["占位图"] += 1
                continue

            # ── 英文检测 ──
            desc = str(p.get("描述", "") or "").strip()
            text = f"{title} {desc}"
            if is_non_english_text(text):
                stats["非英文"] += 1
                continue

            # ── 违禁词 ──
            tags_raw = p.get("子描述", "")
            if isinstance(tags_raw, str) and tags_raw.startswith("["):
                try:
                    import ast
                    tags_list = ast.literal_eval(tags_raw)
                    tags_str = " ".join(str(t) for t in tags_list) if isinstance(tags_list, list) else tags_raw
                except Exception:
                    tags_str = tags_raw
            else:
                tags_str = str(tags_raw or "")
            check_text = f"{title} {desc} {tags_str}".lower()
            if any(kw in check_text for kw in PROHIBITED_KEYWORDS):
                stats["违禁词"] += 1
                continue

            # ── 通过 ──
            passed_keys.append(unique_key)

            if total % 10000 == 0:
                log.info(f"[{prefix}] 进度: {total}")

        if total == 0:
            log.info(f"[{prefix}] 无待清洗数据")
            return {"processed": 0, "cleaned": 0, "removed": 0}

        log.info(f"[{prefix}] 待清洗数据: {total} 条")

        # 打印过滤统计
        log.info(f"[{prefix}] 过滤统计:")
        for reason, count in stats.items():
            if count > 0:
                log.info(f"  ├─ {reason}: {count} 条")
        log.info(f"  └─ 通过: {len(passed_keys)} 条")

        # 更新通过的为 cleaned
        if passed_keys:
            for i in range(0, len(passed_keys), DB_BATCH_SIZE):
                batch = passed_keys[i:i + DB_BATCH_SIZE]
                col.update_many(
                    {"unique_key": {"$in": batch}},
                    {"$set": {"clean_status": CLEAN_STATUS_CLEANED, "clean_time": clean_time}}
                )

        # 更新未通过的为 failed
        failed_keys = list(all_keys - set(passed_keys))
        if failed_keys:
            for i in range(0, len(failed_keys), DB_BATCH_SIZE):
                batch = failed_keys[i:i + DB_BATCH_SIZE]
                col.update_many(
                    {"unique_key": {"$in": batch}},
                    {"$set": {"clean_status": CLEAN_STATUS_FAILED, "clean_time": clean_time}}
                )

        # 同步 _counters（修复：之前漏掉导致前端计数不更新）
        # 统一用 _rebuild_single_product_counter 重建单集合计数器，避免增量计算导致 export 计数漂移
        # （清洗只改 clean_status 不改 export_status，增量更新难以准确推算 export 计数变化）
        self._rebuild_single_product_counter(category, subcategory)

        self._stats_cache.invalidate()

        log.info(f"[{prefix}] 清洗完成: 处理 {total} 条，通过 {len(passed_keys)} 条，移除 {total - len(passed_keys)} 条")

        return {
            "processed": total,
            "cleaned": len(passed_keys),
            "removed": total - len(passed_keys),
            "stats": stats,
        }

    # ── 查询 ──────────────────────────────────────────────

    def list_categories(self) -> List[str]:
        """列出所有产品数据集合的前缀（{category}__{subcategory}）

        单一集合模式下，扫描所有非系统集合，排除带旧后缀的集合，
        以及 _counters 等内部集合。
        """
        prefixes = set()
        for name in self.db.list_collection_names():
            if name.startswith("system."):
                continue
            # 排除内部集合（_counters 等）
            if name.startswith("_"):
                continue
            # 排除带旧后缀的集合（理论上不应存在）
            if name.endswith(RAW_SUFFIX) or name.endswith(CLEAN_SUFFIX) or name.endswith(EXPORT_SUFFIX):
                # 兼容：提取前缀
                for suffix in (RAW_SUFFIX, CLEAN_SUFFIX, EXPORT_SUFFIX):
                    if name.endswith(suffix):
                        prefix = name[:-len(suffix)]
                        if prefix:
                            prefixes.add(prefix)
                        break
                continue
            # 单一集合：包含 __ 分隔符的视为有效前缀
            if "__" in name:
                prefixes.add(name)
            else:
                # 也可能是无子分类的旧集合，仅当看起来像一级分类时加入
                prefixes.add(name)
        return sorted(prefixes)

    def list_categories_with_sub(self) -> List[Dict[str, str]]:
        """列出所有产品数据集合（包含一级和二级分类信息）"""
        results = []
        for prefix in self.list_categories():
            cat, sub = parse_collection_prefix(prefix)
            results.append({"category": cat, "subcategory": sub, "prefix": prefix})
        return results

    def list_all_collections(self, with_counts: bool = True) -> List[Dict[str, Any]]:
        """列出所有产品数据集合及其统计"""
        col_names = sorted(self.db.list_collection_names())
        collections = []

        for name in col_names:
            if name.startswith("system."):
                continue
            if with_counts:
                pipeline = [{"$count": "count"}]
                result = list(self.db[name].aggregate(pipeline))
                count = result[0]["count"] if result else 0
            else:
                count = 0

            prefix = self.parse_category_from_col(name)
            cat, sub = parse_collection_prefix(prefix) if prefix else ("", "")
            # 单一集合模式下 type 统一为 "product"
            col_type = "product"
            # 兼容旧后缀
            if name.endswith(RAW_SUFFIX):
                col_type = "raw"
            elif name.endswith(CLEAN_SUFFIX):
                col_type = "clean"
            elif name.endswith(EXPORT_SUFFIX):
                col_type = "export"

            collections.append({
                "name": name,
                "count": count,
                "category": cat,
                "subcategory": sub,
                "prefix": prefix,
                "type": col_type
            })
        return collections

    def get_category_stats(self, category: str, subcategory: str = "") -> Dict[str, int]:
        """获取指定分类的统计数据（优先读 _counters，O(1)；计数器缺失时回退到 aggregate）

        单集合内按 clean_status / export_status 统计。_counters 由 clean_category /
        rebuild_product_counters 维护，避免对大集合实时 aggregate 导致前端超时。
        """
        prefix = make_collection_prefix(category, subcategory)

        # 优先从 _counters 读取（O(1)）
        doc = self._counters_col().find_one(
            {"_id": prefix},
            {"counts": 1, "total": 1, "_id": 0}
        )
        if doc and doc.get("counts"):
            counts = doc["counts"]
            return {
                "category": category,
                "subcategory": normalize_subcategory(subcategory),
                "prefix": prefix,
                "raw_count": doc.get("total", sum(counts.values())),
                "clean_count": counts.get(CLEAN_STATUS_CLEANED, 0),
                "exported_count": counts.get("exported", 0),
                "unclean_count": counts.get(CLEAN_STATUS_UNCLEAN, 0),
                "cleaned_count": counts.get(CLEAN_STATUS_CLEANED, 0),
                "failed_count": counts.get(CLEAN_STATUS_FAILED, 0),
            }

        # 回退：实时 aggregate（计数器缺失时）
        col = self.collection(category, subcategory)
        pipeline = [{"$group": {"_id": "$clean_status", "count": {"$sum": 1}}}]
        status_counts = {doc["_id"]: doc["count"] for doc in col.aggregate(pipeline)}
        total_count = sum(status_counts.values())

        export_pipeline = [{"$group": {"_id": "$export_status", "count": {"$sum": 1}}}]
        export_counts = {doc["_id"]: doc["count"] for doc in col.aggregate(export_pipeline)}

        return {
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "prefix": prefix,
            "raw_count": total_count,  # 集合总数
            "clean_count": status_counts.get(CLEAN_STATUS_CLEANED, 0),
            "exported_count": export_counts.get(EXPORT_STATUS_EXPORTED, 0),
            "unclean_count": status_counts.get(CLEAN_STATUS_UNCLEAN, 0),
            "cleaned_count": status_counts.get(CLEAN_STATUS_CLEANED, 0),
            "failed_count": status_counts.get(CLEAN_STATUS_FAILED, 0)
        }

    def get_simple_category_stats(self, category: str, subcategory: str = "") -> Dict[str, int]:
        """获取指定分类的简单统计数据（优先读 _counters，O(1)）

        返回 raw_count（总数）和 clean_count（已清洗数）。两者均从 _counters 读取，
        避免 estimated_document_count / aggregate 扫描大集合。
        """
        prefix = make_collection_prefix(category, subcategory)
        # 优先从 _counters 读取 total 和 cleaned（O(1)）
        doc = self._counters_col().find_one(
            {"_id": prefix},
            {"total": 1, "counts.cleaned": 1, "counts.unclean": 1, "_id": 0}
        )
        if doc:
            counts = doc.get("counts", {}) or {}
            total = doc.get("total", 0)
            clean_count = counts.get("cleaned", 0)
            unclean_count = counts.get("unclean", 0)
        else:
            # 回退：estimated_document_count + count_documents（计数器缺失时）
            col = self.collection(category, subcategory)
            total = col.estimated_document_count()
            clean_count = col.count_documents({"clean_status": CLEAN_STATUS_CLEANED})
            unclean_count = col.count_documents({"clean_status": CLEAN_STATUS_UNCLEAN})
        return {
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "prefix": prefix,
            "raw_count": total,
            "clean_count": clean_count,
            "unclean_count": unclean_count,
        }

    def get_unclean_products(self, category: str, subcategory: str = "", limit: Optional[int] = None) -> List[Dict]:
        """获取未清洗的商品数据"""
        col = self.collection(category, subcategory)
        query = {"clean_status": CLEAN_STATUS_UNCLEAN}
        cursor = col.find(query)
        if limit:
            cursor = cursor.limit(limit)
        return list(cursor)

    def get_unclean_count(self, category: str, subcategory: str = "") -> int:
        """获取未清洗的商品数量"""
        col = self.collection(category, subcategory)
        return col.count_documents({"clean_status": CLEAN_STATUS_UNCLEAN})

    def update_clean_status(self, category: str, subcategory: str, unique_keys: List[str],
                            status: str, clean_time: Optional[str] = None) -> int:
        """更新商品的清洗状态"""
        if not unique_keys:
            return 0

        col = self.collection(category, subcategory)
        update_data = {"clean_status": status}
        if clean_time:
            update_data["clean_time"] = clean_time

        modified = 0
        for i in range(0, len(unique_keys), DB_BATCH_SIZE):
            batch = unique_keys[i:i + DB_BATCH_SIZE]
            result = col.update_many(
                {"unique_key": {"$in": batch}},
                {"$set": update_data}
            )
            modified += result.modified_count
        self._stats_cache.invalidate()
        return modified

    def reset_clean_status(self, category: str, subcategory: str = "") -> int:
        """重置指定分类的清洗状态为未清洗"""
        col = self.collection(category, subcategory)
        prefix = make_collection_prefix(category, subcategory)
        # 先统计将被重置的各状态数量，用于同步 _counters
        reset_cleaned = col.count_documents({"clean_status": CLEAN_STATUS_CLEANED})
        reset_failed = col.count_documents({"clean_status": CLEAN_STATUS_FAILED})
        result = col.update_many(
            {"clean_status": {"$ne": CLEAN_STATUS_UNCLEAN}},
            {"$set": {"clean_status": CLEAN_STATUS_UNCLEAN, "clean_time": None}}
        )
        modified = result.modified_count
        # 同步 _counters：cleaned/failed 减少对应数量，unclean 增加 modified
        if modified > 0:
            incs = {CLEAN_STATUS_UNCLEAN: modified,
                    CLEAN_STATUS_CLEANED: -reset_cleaned,
                    CLEAN_STATUS_FAILED: -reset_failed}
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, incs)
        self._stats_cache.invalidate()
        return modified

    def delete_cleaned_from_raw(self, category: str, subcategory: str = "") -> int:
        """从集合中删除已清洗和清洗失败的数据（clean_status=cleaned/failed）"""
        col = self.collection(category, subcategory)
        prefix = make_collection_prefix(category, subcategory)
        # 先统计将被删除的各状态数量，用于同步 _counters
        del_cleaned = col.count_documents({"clean_status": CLEAN_STATUS_CLEANED})
        del_failed = col.count_documents({"clean_status": CLEAN_STATUS_FAILED})
        del_exported = col.count_documents(
            {"clean_status": {"$in": [CLEAN_STATUS_CLEANED, CLEAN_STATUS_FAILED]},
             "export_status": EXPORT_STATUS_EXPORTED})
        del_unexported = col.count_documents(
            {"clean_status": {"$in": [CLEAN_STATUS_CLEANED, CLEAN_STATUS_FAILED]},
             "export_status": EXPORT_STATUS_UNEXPORTED})
        result = col.delete_many({"clean_status": {"$in": [CLEAN_STATUS_CLEANED, CLEAN_STATUS_FAILED]}})
        deleted = result.deleted_count
        # 同步 _counters：各状态减少对应数量
        if deleted > 0:
            incs = {CLEAN_STATUS_CLEANED: -del_cleaned,
                    CLEAN_STATUS_FAILED: -del_failed,
                    "exported": -del_exported,
                    "unexported": -del_unexported}
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, incs)
        self._stats_cache.invalidate()
        return deleted

    def get_all_stats(self, use_cache: bool = True) -> Dict[str, Any]:
        """获取所有产品数据统计（带缓存）"""
        cache_key = "product_all_stats"

        if use_cache:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                return cached

        cat_list = self.list_categories_with_sub()
        total_count = 0
        total_clean = 0
        total_exported = 0
        total_unclean = 0
        total_cleaned = 0
        total_failed = 0
        category_stats = []

        for item in cat_list:
            stats = self.get_category_stats(item["category"], item["subcategory"])
            category_stats.append(stats)
            total_count += stats["raw_count"]
            total_clean += stats["clean_count"]
            total_exported += stats.get("exported_count", 0)
            total_unclean += stats.get("unclean_count", 0)
            total_cleaned += stats.get("cleaned_count", 0)
            total_failed += stats.get("failed_count", 0)

        result = {
            "total_categories": len(cat_list),
            "total_raw": total_count,
            "total_clean": total_clean,
            "total_exported": total_exported,
            "total_unclean": total_unclean,
            "total_cleaned": total_cleaned,
            "total_failed": total_failed,
            "categories": category_stats
        }

        if use_cache:
            self._stats_cache.set(cache_key, result)

        return result

    def get_simple_all_stats(self) -> Dict[str, Any]:
        """获取所有分类的简单统计数据"""
        cat_list = self.list_categories_with_sub()
        total_raw = 0
        total_clean = 0
        category_stats = []
        for item in cat_list:
            stats = self.get_simple_category_stats(item["category"], item["subcategory"])
            category_stats.append(stats)
            total_raw += stats["raw_count"]
            total_clean += stats["clean_count"]
        return {
            "total_categories": len(cat_list),
            "total_raw": total_raw,
            "total_clean": total_clean,
            "categories": category_stats
        }

    # ── 导出 ──────────────────────────────────────────────

    def export_category_to_excel(self, category: str, subcategory: str, export_dir: str,
                                  limit: Optional[int] = None, progress_callback=None) -> Optional[Dict[str, Any]]:
        """导出指定分类的已清洗未导出数据到 Excel，并在集合中标记为已导出

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称
            export_dir: 导出目录
            limit: 导出数量限制，None 表示全部
            progress_callback: 进度回调函数

        Returns:
            成功返回 {"filepath": str, "count": int}，无数据或失败返回 None
        """
        import os
        import pandas as pd

        prefix = make_collection_prefix(category, subcategory)
        col = self.collection(category, subcategory)
        # 查询已清洗且未导出的数据
        query = {
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED
        }
        cursor = col.find(query)
        if limit:
            cursor = cursor.limit(limit)
        products = list(cursor)

        if not products:
            return None

        # 只保留指定的导出字段
        rows = []
        exported_ids = []
        for doc in products:
            row = {}
            for c in EXPORT_COLUMNS:
                value = doc.get(c)
                if isinstance(value, list):
                    cell = ", ".join(str(item).strip() for item in value if str(item).strip())
                elif value is None:
                    cell = ""
                else:
                    cell = str(value).strip()
                cell = _clean_excel_illegal_chars(cell)
                if len(cell) > 32000:
                    cell = cell[:32000] + "...[truncated]"
                row[c] = cell
            rows.append(row)
            if doc.get("_id"):
                exported_ids.append(doc["_id"])

        # 创建导出目录（按日期/分类前缀分文件夹，如 exports/20260807/cameras_optics__binoculars/）
        date_str = datetime.now().strftime("%Y%m%d")
        category_dir = os.path.join(export_dir, date_str, prefix)
        os.makedirs(category_dir, exist_ok=True)

        filename = f"{prefix}_{len(rows)}.xlsx"
        filepath = os.path.join(category_dir, filename)

        # 导出到 Excel
        df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
        df.to_excel(filepath, index=False, engine="openpyxl")

        # 在同一集合中标记为已导出（不再移动到 _export 集合）
        export_time = datetime.utcnow().isoformat()
        marked = 0
        if exported_ids:
            for i in range(0, len(exported_ids), DB_BATCH_SIZE):
                batch = exported_ids[i:i + DB_BATCH_SIZE]
                result = col.update_many(
                    {"_id": {"$in": batch}},
                    {"$set": {
                        "export_status": EXPORT_STATUS_EXPORTED,
                        "export_time": export_time,
                        "last_export_time": export_time,
                    }, "$inc": {"export_count": 1}}
                )
                marked += result.modified_count

        # 同步 _counters 的导出计数
        if marked > 0:
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, {"unexported": -marked, "exported": marked})

        self._stats_cache.invalidate()
        log.info(f"导出Excel: {filepath} ({len(rows)} 条)，已标记为已导出")
        return {"filepath": filepath, "count": len(rows)}

    def merge_export_category(self, category: str, export_dir: str,
                              limit: Optional[int] = None,
                              progress_callback=None) -> Optional[Dict[str, Any]]:
        """合并导出一级分类下所有二级分类的已清洗未导出数据到一个 Excel

        遍历该一级分类下的所有二级集合，查询 clean_status=cleaned 且 export_status=unexported
        的文档，按 EXPORT_COLUMNS 提取字段，按"标题"去重后合并写入单个 Excel 文件，
        并在各集合中标记为已导出。

        Args:
            category: 一级分类名称
            export_dir: 导出根目录
            limit: 每个二级分类的导出数量限制，None 表示全部
            progress_callback: 进度回调函数

        Returns:
            成功返回 {"filepath", "count", "dedup_count", "sub_count", "marked_count"}，
            无数据返回 None
        """
        import os
        import pandas as pd

        # 收集该一级分类下所有二级分类
        all_cats = self.list_categories_with_sub()
        sub_items = [item for item in all_cats if item["category"] == category]
        if not sub_items:
            log.info(f"合并导出 {category}: 无子分类")
            return None

        sub_count = 0
        all_rows = []
        # 记录每个二级分类参与导出的文档 _id，用于后续标记
        # 结构: {prefix: [_id, _id, ...]}
        exported_ids_by_prefix: Dict[str, list] = {}

        for idx, item in enumerate(sub_items):
            sub = item["subcategory"]
            prefix = item["prefix"]
            col = self.collection(category, sub)

            query = {
                "clean_status": CLEAN_STATUS_CLEANED,
                "export_status": EXPORT_STATUS_UNEXPORTED,
            }
            cursor = col.find(query)
            if limit:
                cursor = cursor.limit(limit)

            docs = list(cursor)
            if not docs:
                continue

            sub_count += 1
            ids = []
            for doc in docs:
                row = {}
                for c in EXPORT_COLUMNS:
                    value = doc.get(c)
                    if isinstance(value, list):
                        cell = ", ".join(str(it).strip() for it in value if str(it).strip())
                    elif value is None:
                        cell = ""
                    else:
                        cell = str(value).strip()
                    cell = _clean_excel_illegal_chars(cell)
                    if len(cell) > 32000:
                        cell = cell[:32000] + "...[truncated]"
                    row[c] = cell
                all_rows.append(row)
                if doc.get("_id"):
                    ids.append(doc["_id"])
            if ids:
                exported_ids_by_prefix[prefix] = ids

            if progress_callback:
                progress_callback(idx + 1, len(sub_items))

        if not all_rows:
            log.info(f"合并导出 {category}: 无清洗后数据")
            return None

        # 合并并按"标题"去重
        df = pd.DataFrame(all_rows, columns=EXPORT_COLUMNS)
        before_dedup = len(df)
        if "标题" in df.columns:
            df = df.drop_duplicates(subset=["标题"], keep="first")
        dedup_count = before_dedup - len(df)

        # 写入 Excel（按日期/一级分类，如 exports/20260807/）
        date_str = datetime.now().strftime("%Y%m%d")
        date_dir = os.path.join(export_dir, date_str)
        os.makedirs(date_dir, exist_ok=True)
        filename = f"{category}_merged_{len(df)}.xlsx"
        filepath = os.path.join(date_dir, filename)
        df.to_excel(filepath, index=False, engine="openpyxl")

        # 在各集合中标记为已导出
        export_time = datetime.utcnow().isoformat()
        marked_count = 0
        for prefix, ids in exported_ids_by_prefix.items():
            if not ids:
                continue
            # 按 prefix 反查 category/subcategory 以获取集合
            cat, sub = parse_collection_prefix(prefix)
            col = self.collection(cat, sub)
            prefix_marked = 0
            for i in range(0, len(ids), DB_BATCH_SIZE):
                batch = ids[i:i + DB_BATCH_SIZE]
                result = col.update_many(
                    {"_id": {"$in": batch}},
                    {"$set": {
                        "export_status": EXPORT_STATUS_EXPORTED,
                        "export_time": export_time,
                        "last_export_time": export_time,
                    }, "$inc": {"export_count": 1}}
                )
                prefix_marked += result.modified_count
            marked_count += prefix_marked
            # 同步该 prefix 的 _counters 导出计数
            if prefix_marked > 0:
                self._set_counter_type(prefix, "product", cat, normalize_subcategory(sub))
                self._inc_counters(prefix, {"unexported": -prefix_marked, "exported": prefix_marked})

        self._stats_cache.invalidate()
        log.info(f"合并导出Excel: {filepath} ({len(df)} 条，去重 {dedup_count} 条，"
                 f"{sub_count} 个二级分类，标记 {marked_count} 条)")
        return {
            "filepath": filepath,
            "count": len(df),
            "dedup_count": dedup_count,
            "sub_count": sub_count,
            "marked_count": marked_count,
        }
