"""产品数据管理数据库客户端"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure, BulkWriteError
from pymongo.collection import Collection

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("product_db")

# 数据库名称
PRODUCT_DB_NAME = "qmds_product_data"

# 集合后缀
RAW_SUFFIX = "_raw"
CLEAN_SUFFIX = "_clean"


class ProductDBClient:
    """产品数据管理数据库客户端
    
    数据库结构:
    - 数据库: qmds_product_data
    - 集合命名: {category}_raw (原始数据), {category}_clean (清洗后数据)
    """
    
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
        
        # 清洗后数据索引
        clean = self.clean_col(category)
        clean.create_index([("unique_key", ASCENDING)], name="idx_unique_key")
        clean.create_index([("source_url", ASCENDING)], name="idx_source_url")
        clean.create_index([("分类", ASCENDING)], name="idx_category")
        clean.create_index([("clean_time", ASCENDING)], name="idx_clean_time")
        
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
                unique_map[unique_key] = product
        
        deduped_batch = list(unique_map.values())
        if not deduped_batch:
            return 0
        
        # 检查已存在的记录
        candidate_keys = list(unique_map.keys())
        existing_keys = {
            item["unique_key"]
            for item in col.find({"unique_key": {"$in": candidate_keys}}, {"unique_key": 1})
        }
        to_insert = [item for item in deduped_batch if item["unique_key"] not in existing_keys]
        
        if not to_insert:
            return 0
        
        try:
            col.insert_many(to_insert, ordered=False)
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
        
        # 检查已存在的记录
        candidate_keys = list(unique_map.keys())
        existing_keys = {
            item["unique_key"]
            for item in col.find({"unique_key": {"$in": candidate_keys}}, {"unique_key": 1})
        }
        to_insert = [item for item in deduped_batch if item["unique_key"] not in existing_keys]
        
        if not to_insert:
            return 0
        
        try:
            col.insert_many(to_insert, ordered=False)
            return len(to_insert)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", []) if exc.details else []
            return max(len(to_insert) - len(write_errors), 0)
    
    # ── 清洗操作 ──────────────────────────────────────────
    
    def clean_category(self, category: str) -> Dict[str, int]:
        """清洗指定类目的原始数据
        
        Args:
            category: 类目名称
            
        Returns:
            {"processed": 处理数量, "cleaned": 清洗后数量, "removed": 移除数量}
        """
        raw_col = self.raw_col(category)
        products = list(raw_col.find({}))
        
        if not products:
            return {"processed": 0, "cleaned": 0, "removed": 0}
        
        # 转换为Product对象进行清洗
        from qmds.modules.data_scraper.models.schemas import Product
        from qmds.modules.data_scraper.pipeline import ProductProcessor, ProductFilter
        
        product_objects = []
        for p in products:
            try:
                # 移除MongoDB的_id字段
                if "_id" in p:
                    del p["_id"]
                product_objects.append(Product(**p))
            except Exception:
                continue
        
        # 清洗处理
        processor = ProductProcessor()
        filter_obj = ProductFilter()
        
        cleaned_products = processor.process_all(product_objects)
        filtered_products = filter_obj.filter(cleaned_products)
        final_products = [p for p in filtered_products if not filter_obj.has_prohibited_content(p)]
        
        # 保存清洗后的数据
        if final_products:
            clean_data = [p.__dict__ for p in final_products]
            self.save_clean_products(category, clean_data)
        
        return {
            "processed": len(products),
            "cleaned": len(final_products),
            "removed": len(products) - len(final_products)
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
    
    def list_all_collections(self) -> List[Dict[str, Any]]:
        """列出所有产品数据集合及其统计"""
        collections = []
        for name in sorted(self.db.list_collection_names()):
            count = self.db[name].estimated_document_count()
            category = self.parse_category_from_col(name)
            col_type = "raw" if name.endswith(RAW_SUFFIX) else ("clean" if name.endswith(CLEAN_SUFFIX) else "other")
            collections.append({
                "name": name,
                "count": count,
                "category": category,
                "type": col_type
            })
        return collections
    
    def get_category_stats(self, category: str) -> Dict[str, int]:
        """获取指定类目的统计数据"""
        raw_count = self.raw_col(category).estimated_document_count()
        clean_count = self.clean_col(category).estimated_document_count()
        return {
            "category": category,
            "raw_count": raw_count,
            "clean_count": clean_count
        }
    
    def get_all_stats(self) -> Dict[str, Any]:
        """获取所有产品数据统计"""
        categories = self.list_categories()
        total_raw = 0
        total_clean = 0
        category_stats = []
        
        for category in categories:
            stats = self.get_category_stats(category)
            category_stats.append(stats)
            total_raw += stats["raw_count"]
            total_clean += stats["clean_count"]
        
        return {
            "total_categories": len(categories),
            "total_raw": total_raw,
            "total_clean": total_clean,
            "categories": category_stats
        }
    
    # ── 导出 ──────────────────────────────────────────────
    
    def export_category_to_excel(self, category: str, export_dir: str) -> Optional[str]:
        """导出指定类目的数据到Excel
        
        Args:
            category: 类目名称
            export_dir: 导出目录
            
        Returns:
            导出文件路径，失败返回None
        """
        import os
        import pandas as pd
        
        clean_col = self.clean_col(category)
        products = list(clean_col.find({}))
        
        if not products:
            return None
        
        # 移除MongoDB的_id字段
        for p in products:
            if "_id" in p:
                del p["_id"]
        
        # 创建导出目录
        os.makedirs(export_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{category}_clean_{timestamp}.xlsx"
        filepath = os.path.join(export_dir, filename)
        
        # 导出到Excel
        df = pd.DataFrame(products)
        df.to_excel(filepath, index=False, engine="openpyxl")
        
        log.info(f"导出Excel: {filepath} ({len(products)} 条)")
        return filepath
