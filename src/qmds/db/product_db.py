"""产品数据管理数据库客户端"""

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
from qmds.utils.logger import get_logger

log = get_logger("product_db")


class _TTLCache:
    """简单的线程安全 TTL 缓存"""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}  # key -> (value, expire_ts)
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

# 集合后缀
RAW_SUFFIX = "_raw"
CLEAN_SUFFIX = "_clean"

# 清洗状态
CLEAN_STATUS_UNCLEAN = "unclean"  # 未清洗
CLEAN_STATUS_CLEANED = "cleaned"  # 已清洗
CLEAN_STATUS_FAILED = "failed"    # 清洗失败

# 导出字段配置
EXPORT_COLUMNS = [
    "SKU", "标题", "描述", "子描述", "图片",
    "原价", "折扣价", "变体", "分类",
    "currency", "source_domain", "source_category",
]


class ProductDBClient:
    """产品数据管理数据库客户端
    
    数据库结构:
    - 数据库: qmds_product_data
    - 集合命名: {category}_raw (原始数据), {category}_clean (清洗后数据)
    """

    _stats_cache = _TTLCache(ttl_seconds=60)  # 类级别缓存，60秒过期
    
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
    
    # ── 集合命名 ──────────────────────────────────────────
    
    def raw_col(self, category: str) -> Collection:
        """获取 {category}_raw 集合（原始数据）"""
        return self.db[f"{category}{RAW_SUFFIX}"]
    
    def clean_col(self, category: str) -> Collection:
        """获取 {category}_clean 集合（清洗后数据）"""
        return self.db[f"{category}{CLEAN_SUFFIX}"]
    
    def get_raw_col_name(self, category: str) -> str:
        """获取原始数据集合名称"""
        return f"{category}{RAW_SUFFIX}"
    
    def get_clean_col_name(self, category: str) -> str:
        """获取清洗后数据集合名称"""
        return f"{category}{CLEAN_SUFFIX}"
    
    def parse_category_from_col(self, col_name: str) -> Optional[str]:
        """从集合名称解析类目"""
        if col_name.endswith(RAW_SUFFIX):
            return col_name[:-len(RAW_SUFFIX)]
        if col_name.endswith(CLEAN_SUFFIX):
            return col_name[:-len(CLEAN_SUFFIX)]
        return None
    
    # ── 索引 ──────────────────────────────────────────────
    
    def ensure_product_indexes(self, category: str):
        """为产品数据集合创建索引"""
        # 原始数据索引
        raw = self.raw_col(category)
        raw.create_index([("unique_key", ASCENDING)], name="idx_unique_key")
        raw.create_index([("source_url", ASCENDING)], name="idx_source_url")
        raw.create_index([("source_domain", ASCENDING)], name="idx_source_domain")
        raw.create_index([("crawl_time", ASCENDING)], name="idx_crawl_time")
        raw.create_index([("分类", ASCENDING)], name="idx_category")
        raw.create_index([("clean_status", ASCENDING)], name="idx_clean_status")
        
        # 清洗后数据索引
        clean = self.clean_col(category)
        clean.create_index([("unique_key", ASCENDING)], name="idx_unique_key")
        clean.create_index([("source_url", ASCENDING)], name="idx_source_url")
        clean.create_index([("分类", ASCENDING)], name="idx_category")
        clean.create_index([("clean_time", ASCENDING)], name="idx_clean_time")
        clean.create_index([("export_count", ASCENDING)], name="idx_export_count")
        clean.create_index([("last_export_time", ASCENDING)], name="idx_last_export_time")
        
        log.info(f"索引已创建: {category}{RAW_SUFFIX}, {category}{CLEAN_SUFFIX}")
    
    # ── 写入（原始数据） ──────────────────────────────────
    
    def save_raw_products(self, category: str, products: List[dict]) -> int:
        """保存原始商品数据到 {category}_raw
        
        Args:
            category: 类目名称
            products: 商品数据列表
            
        Returns:
            新增商品数量
        """
        if not products:
            return 0
        
        col = self.raw_col(category)
        self.ensure_product_indexes(category)
        
        # 去重：基于unique_key
        unique_map = {}
        for product in products:
            unique_key = product.get("unique_key")
            if unique_key:
                # 添加清洗状态字段
                product.setdefault("clean_status", CLEAN_STATUS_UNCLEAN)
                product.setdefault("clean_time", None)
                unique_map[unique_key] = product
        
        deduped_batch = list(unique_map.values())
        if not deduped_batch:
            return 0
        
        # 检查已存在的记录（分批查询，避免 $in 列表过大导致 BSON 超限）
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
            self._stats_cache.invalidate()
            return len(to_insert)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", []) if exc.details else []
            return max(len(to_insert) - len(write_errors), 0)
    
    # ── 写入（清洗后数据） ────────────────────────────────
    
    def save_clean_products(self, category: str, products: List[dict]) -> int:
        """保存清洗后的商品数据到 {category}_clean
        
        Args:
            category: 类目名称
            products: 清洗后的商品数据列表
            
        Returns:
            新增商品数量
        """
        if not products:
            return 0
        
        col = self.clean_col(category)
        
        # 添加清洗时间
        clean_time = datetime.utcnow().isoformat()
        for product in products:
            product["clean_time"] = clean_time
        
        # 去重：基于unique_key
        unique_map = {}
        for product in products:
            unique_key = product.get("unique_key")
            if unique_key:
                unique_map[unique_key] = product
        
        deduped_batch = list(unique_map.values())
        if not deduped_batch:
            return 0
        
        # 检查已存在的记录（分批查询，避免 $in 列表过大导致 BSON 超限）
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
            self._stats_cache.invalidate()
            return len(to_insert)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", []) if exc.details else []
            return max(len(to_insert) - len(write_errors), 0)
    
    # ── 清洗操作 ──────────────────────────────────────────
    
    def clean_category(self, category: str, force: bool = False) -> Dict[str, int]:
        """清洗指定类目的原始数据
        
        Args:
            category: 类目名称
            force: 是否强制清洗所有数据（包括已清洗的）
            
        Returns:
            {"processed": 处理数量, "cleaned": 清洗后数量, "removed": 移除数量}
        """
        from qmds.modules.data_scraper.pipeline.filters import (
            PLACEHOLDER_IMAGES, PROHIBITED_KEYWORDS, MIN_TITLE_LENGTH, MIN_PRICE, MAX_PRICE
        )
        from qmds.utils.language import is_non_english_text

        raw_col = self.raw_col(category)
        
        # 只查询未清洗的数据（除非强制清洗）
        if force:
            query = {}
        else:
            query = {"clean_status": CLEAN_STATUS_UNCLEAN}
        
        products = list(raw_col.find(query))
        total = len(products)
        
        if not products:
            log.info(f"[{category}] 无待清洗数据")
            return {"processed": 0, "cleaned": 0, "removed": 0}
        
        log.info(f"[{category}] 待清洗数据: {total} 条")

        clean_time = datetime.utcnow().isoformat()
        final_products = []
        all_keys = set()
        stats = {"价格超范围": 0, "标题过短": 0, "占位图": 0, "非英文": 0, "违禁词": 0, "无key": 0}

        for idx, p in enumerate(products, 1):
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
            p["clean_time"] = clean_time
            p.pop("_id", None)
            final_products.append(p)

            # 每 10000 条打印一次进度
            if idx % 10000 == 0:
                log.info(f"[{category}] 进度: {idx}/{total}")

        # 打印过滤统计
        log.info(f"[{category}] 过滤统计:")
        for reason, count in stats.items():
            if count > 0:
                log.info(f"  ├─ {reason}: {count} 条")
        log.info(f"  └─ 通过: {len(final_products)} 条")

        # 保存清洗后的数据
        if final_products:
            saved = self.save_clean_products(category, final_products)
            log.info(f"[{category}] 写入 {category}_clean: {saved} 条（去重后新增）")
        
        # 更新原始数据的清洗状态（分批更新）
        cleaned_keys = {p["unique_key"] for p in final_products if p.get("unique_key")}
        if cleaned_keys:
            cleaned_keys_list = list(cleaned_keys)
            for i in range(0, len(cleaned_keys_list), 10000):
                batch = cleaned_keys_list[i:i + 10000]
                raw_col.update_many(
                    {"unique_key": {"$in": batch}},
                    {"$set": {"clean_status": CLEAN_STATUS_CLEANED, "clean_time": clean_time}}
                )
        
        failed_keys = all_keys - cleaned_keys
        if failed_keys:
            failed_keys_list = list(failed_keys)
            for i in range(0, len(failed_keys_list), 10000):
                batch = failed_keys_list[i:i + 10000]
                raw_col.update_many(
                    {"unique_key": {"$in": batch}},
                    {"$set": {"clean_status": CLEAN_STATUS_FAILED, "clean_time": clean_time}}
                )

        self._stats_cache.invalidate()

        log.info(f"[{category}] 清洗完成: 处理 {total} 条，通过 {len(final_products)} 条，移除 {total - len(final_products)} 条")

        return {
            "processed": total,
            "cleaned": len(final_products),
            "removed": total - len(final_products),
            "stats": stats,
        }

    # ── 查询 ──────────────────────────────────────────────
    
    def list_categories(self) -> List[str]:
        """列出所有有原始数据的类目"""
        categories = set()
        for name in self.db.list_collection_names():
            if name.endswith(RAW_SUFFIX):
                category = name[:-len(RAW_SUFFIX)]
                if category:
                    categories.add(category)
        return sorted(categories)
    
    def list_all_collections(self, with_counts: bool = True) -> List[Dict[str, Any]]:
        """列出所有产品数据集合及其统计

        Args:
            with_counts: 是否查询每个集合的文档数量（设为False可跳过）
        """
        col_names = sorted(self.db.list_collection_names())
        collections = []

        if with_counts:
            for name in col_names:
                # 用聚合 $count 替代 estimated_document_count，保证精确
                pipeline = [{"$count": "count"}]
                result = list(self.db[name].aggregate(pipeline))
                count = result[0]["count"] if result else 0
                category = self.parse_category_from_col(name)
                col_type = "raw" if name.endswith(RAW_SUFFIX) else ("clean" if name.endswith(CLEAN_SUFFIX) else "other")
                collections.append({
                    "name": name,
                    "count": count,
                    "category": category,
                    "type": col_type
                })
        else:
            for name in col_names:
                category = self.parse_category_from_col(name)
                col_type = "raw" if name.endswith(RAW_SUFFIX) else ("clean" if name.endswith(CLEAN_SUFFIX) else "other")
                collections.append({
                    "name": name,
                    "count": 0,
                    "category": category,
                    "type": col_type
                })
        return collections
    
    def get_category_stats(self, category: str) -> Dict[str, int]:
        """获取指定类目的统计数据（使用聚合管道，单次查询）"""
        raw_col = self.raw_col(category)
        clean_col = self.clean_col(category)

        # raw 聚合：按 clean_status 分组
        raw_pipeline = [
            {"$group": {"_id": "$clean_status", "count": {"$sum": 1}}}
        ]
        status_counts = {doc["_id"]: doc["count"] for doc in raw_col.aggregate(raw_pipeline)}
        raw_count = sum(status_counts.values())

        # clean 聚合：总数 + 已导出数（export_count > 0）
        clean_pipeline = [
            {"$facet": {
                "total": [{"$count": "count"}],
                "exported": [
                    {"$match": {"export_count": {"$gt": 0}}},
                    {"$count": "count"}
                ]
            }}
        ]
        clean_result = list(clean_col.aggregate(clean_pipeline))
        if clean_result:
            facet = clean_result[0]
            clean_count = facet["total"][0]["count"] if facet.get("total") else 0
            exported_count = facet["exported"][0]["count"] if facet.get("exported") else 0
        else:
            clean_count = 0
            exported_count = 0

        return {
            "category": category,
            "raw_count": raw_count,
            "clean_count": clean_count,
            "exported_count": exported_count,
            "unclean_count": status_counts.get(CLEAN_STATUS_UNCLEAN, 0),
            "cleaned_count": status_counts.get(CLEAN_STATUS_CLEANED, 0),
            "failed_count": status_counts.get(CLEAN_STATUS_FAILED, 0)
        }
    
    def get_unclean_products(self, category: str, limit: Optional[int] = None) -> List[Dict]:
        """获取未清洗的商品数据
        
        Args:
            category: 类目名称
            limit: 返回数量限制
            
        Returns:
            未清洗的商品数据列表
        """
        raw_col = self.raw_col(category)
        query = {"clean_status": CLEAN_STATUS_UNCLEAN}
        cursor = raw_col.find(query)
        if limit:
            cursor = cursor.limit(limit)
        return list(cursor)
    
    def get_unclean_count(self, category: str) -> int:
        """获取未清洗的商品数量"""
        raw_col = self.raw_col(category)
        return raw_col.count_documents({"clean_status": CLEAN_STATUS_UNCLEAN})
    
    def update_clean_status(self, category: str, unique_keys: List[str], 
                           status: str, clean_time: Optional[str] = None) -> int:
        """更新商品的清洗状态
        
        Args:
            category: 类目名称
            unique_keys: 商品unique_key列表
            status: 清洗状态
            clean_time: 清洗时间
            
        Returns:
            更新的文档数量
        """
        if not unique_keys:
            return 0
        
        raw_col = self.raw_col(category)
        update_data = {"clean_status": status}
        if clean_time:
            update_data["clean_time"] = clean_time
        
        result = raw_col.update_many(
            {"unique_key": {"$in": unique_keys}},
            {"$set": update_data}
        )
        self._stats_cache.invalidate()
        return result.modified_count
    
    def reset_clean_status(self, category: str) -> int:
        """重置指定类目的清洗状态为未清洗
        
        Args:
            category: 类目名称
            
        Returns:
            重置的文档数量
        """
        raw_col = self.raw_col(category)
        result = raw_col.update_many(
            {"clean_status": {"$ne": CLEAN_STATUS_UNCLEAN}},
            {"$set": {"clean_status": CLEAN_STATUS_UNCLEAN, "clean_time": None}}
        )
        self._stats_cache.invalidate()
        return result.modified_count
    
    def get_all_stats(self, use_cache: bool = True) -> Dict[str, Any]:
        """获取所有产品数据统计（带缓存）

        Args:
            use_cache: 是否使用缓存（默认60秒TTL）
        """
        cache_key = "product_all_stats"

        if use_cache:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                return cached

        categories = self.list_categories()
        total_raw = 0
        total_clean = 0
        total_exported = 0
        total_unclean = 0
        total_cleaned = 0
        total_failed = 0
        category_stats = []

        for category in categories:
            stats = self.get_category_stats(category)
            category_stats.append(stats)
            total_raw += stats["raw_count"]
            total_clean += stats["clean_count"]
            total_exported += stats.get("exported_count", 0)
            total_unclean += stats.get("unclean_count", 0)
            total_cleaned += stats.get("cleaned_count", 0)
            total_failed += stats.get("failed_count", 0)

        result = {
            "total_categories": len(categories),
            "total_raw": total_raw,
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
    
    # ── 导出 ──────────────────────────────────────────────
    
    def export_category_to_excel(self, category: str, export_dir: str, 
                                  limit: Optional[int] = None, progress_callback=None) -> Optional[str]:
        """导出指定类目的数据到Excel
        
        Args:
            category: 类目名称
            export_dir: 导出目录
            limit: 导出数量限制，None表示全部导出
            progress_callback: 进度回调函数
            
        Returns:
            导出文件路径，失败返回None
        """
        import os
        import pandas as pd
        
        clean_col = self.clean_col(category)
        cursor = clean_col.find({}).sort("export_count", ASCENDING)
        if limit:
            cursor = cursor.limit(limit)
        products = list(cursor)
        
        if not products:
            return None
        
        # 只保留指定的导出字段
        rows = []
        for doc in products:
            row = {}
            for col in EXPORT_COLUMNS:
                value = doc.get(col)
                if isinstance(value, list):
                    cell = ", ".join(str(item).strip() for item in value if str(item).strip())
                elif value is None:
                    cell = ""
                else:
                    cell = str(value).strip()
                # 清除 openpyxl 不允许的控制字符
                cell = _clean_excel_illegal_chars(cell)
                # Excel单元格限制32767字符，截断超长内容
                if len(cell) > 32000:
                    cell = cell[:32000] + "...[truncated]"
                row[col] = cell
            rows.append(row)
        
        # 创建导出目录（按类目分文件夹）
        category_dir = os.path.join(export_dir, category)
        os.makedirs(category_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{category}_clean_{timestamp}.xlsx"
        filepath = os.path.join(category_dir, filename)
        
        # 导出到Excel
        df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
        df.to_excel(filepath, index=False, engine="openpyxl")
        
        # 更新导出次数
        export_time = datetime.utcnow().isoformat()
        exported_ids = [doc.get("_id") for doc in products if doc.get("_id")]
        if exported_ids:
            clean_col.update_many(
                {"_id": {"$in": exported_ids}},
                [
                    {"$set": {
                        "export_count": {"$add": [{"$ifNull": ["$export_count", 0]}, 1]},
                        "last_export_time": export_time
                    }}
                ]
            )
        
        log.info(f"导出Excel: {filepath} ({len(rows)} 条)")
        return filepath
