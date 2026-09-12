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


# 分类字段无效名判定用分隔符（与 data_cleaner._NUM_SEP_RE 一致）
_INVALID_CAT_SEP_RE = re.compile(r'[\s\-_.,/\\|:;]+')
# 分类多级分隔符（||| -> > , / : ： 等）
_INVALID_CAT_LEVEL_RE = re.compile(r'\s*\|\|\|\s*|\s*->\s*|\s*>\s*|\s*,\s*|\s*/\s*|\s*[:：]\s*')
# 无效分类名归入的公共类（清洗流程无公共类参数，使用默认）
DEFAULT_INVALID_CATEGORY = "Other"


def is_invalid_category_name(cat_name) -> bool:
    """判定无效分类名：simple、含 undefined、整值为纯数字、或任一级为纯数字

    返回 True 表示该分类名属于无效值，应由调用方清理（覆盖为有效分类）。
    """
    if not cat_name:
        return False
    cat_lower = str(cat_name).strip().lower()
    if cat_lower == "simple":
        return True
    if "undefined" in cat_lower:
        return True
    cleaned = _INVALID_CAT_SEP_RE.sub('', str(cat_name).strip())
    if cleaned.isdigit():
        return True
    parts = _INVALID_CAT_LEVEL_RE.split(str(cat_name).strip())
    for part in parts:
        part_cleaned = _INVALID_CAT_SEP_RE.sub('', part)
        if part_cleaned and part_cleaned.isdigit():
            return True
    return False


from pymongo import MongoClient, ASCENDING, UpdateOne, InsertOne
from pymongo.errors import ConnectionFailure, BulkWriteError
from pymongo.collection import Collection

from qmds.config import settings
from qmds.config.categories import (
    make_collection_prefix,
    parse_collection_prefix,
    normalize_subcategory,
    SHOPIFY_TO_GOOGLE_CATEGORY,
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

# 分类数据处理状态（低频合并等分类处理步骤，作用于已清洗未导出数据）
CATEGORY_STATUS_UNPROCESSED = "unprocessed"  # 未处理
CATEGORY_STATUS_PROCESSED = "processed"      # 已处理

# 模型优化分类状态（LLM 同义合并/补全父级，作用于已清洗未导出数据）
OPTIMIZE_STATUS_UNOPTIMIZED = "unoptimized"  # 未优化
OPTIMIZE_STATUS_OPTIMIZED = "optimized"      # 已优化

# 单次 $in / update_many 批量操作的最大文档数（过大的批量会使 mongod 内存剧烈尖峰，曾导致 OOM 崩溃）
DB_BATCH_SIZE = 500

# 导出字段配置（与 BB_Data_Tool 清洗输出列名完全一致）
EXPORT_COLUMNS = [
    "SKU", "Name", "Description", "Regular price", "Categories", "Images",
    "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言",
]


def _build_export_row(doc: Dict[str, Any],
                     fallback_category: str = "") -> Dict[str, Any]:
    """将产品文档映射为 BB_Data_Tool 兼容的导出行

    映射规则（与 BB 清洗输出语义一致）：
    - Name           <- 标题
    - Description    <- 描述 + <br> + 子描述（合并）
    - Regular price  <- max(原价, 折扣价)，数值型
    - Categories     <- 分类
    - Images         <- 图片
    - cf_opingts     <- 变体
    - 自定义分类      <- source_category 的英文简化名映射为中文一级分类名；
                        映射不出来的（含空值）改为按 fallback_category
                        （文档所在集合的一级分类）映射，保证该列始终是
                        站群分类树认可的中文分类名，不透传英文原值
    - 原站域名        <- source_domain
    - 分布网站识别    <- 固定 0
    - 语言           <- 固定 "en"

    Args:
        doc: 产品文档
        fallback_category: 文档所在集合的一级分类（大类，如
            animals_pet_supplies），source_category 无法映射时兜底
    """
    from qmds.config.categories import get_cn_category_name

    def _as_str(value) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    def _price_float(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    desc = _as_str(doc.get("描述"))
    sub_desc = _as_str(doc.get("子描述"))
    description = f"{desc}<br>{sub_desc}" if sub_desc else desc

    regular_price = max(_price_float(doc.get("原价")), _price_float(doc.get("折扣价")))

    source_category = _as_str(doc.get("source_category"))
    custom_category = get_cn_category_name(source_category) if source_category else ""
    # 映射不出来的值（get_cn_category_name 原样返回 == 输入；空值返回 ""）
    # -> 按所在集合的大类映射，避免英文原样透传进 自定义分类 列
    if fallback_category and custom_category == source_category:
        custom_category = get_cn_category_name(fallback_category)

    return {
        "SKU": _as_str(doc.get("SKU")),
        "Name": _as_str(doc.get("标题")),
        "Description": description,
        "Regular price": round(regular_price, 2),
        "Categories": _as_str(doc.get("分类")),
        "Images": _as_str(doc.get("图片")),
        "cf_opingts": _as_str(doc.get("变体")),
        "自定义分类": custom_category,
        "原站域名": _as_str(doc.get("source_domain")),
        "分布网站识别": 0,
        "语言": "en",
    }


def _sanitize_export_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """对导出行做兜底清理：Excel 非法控制字符 + 超长截断（保留数值类型）"""
    for key, value in list(row.items()):
        if isinstance(value, str):
            value = _clean_excel_illegal_chars(value)
            if len(value) > 32000:
                value = value[:32000] + "...[truncated]"
            row[key] = value
    return row


class ProductDBClient:
    """产品数据管理数据库客户端（单一集合模式）

    数据库结构:
    - 数据库: qmds_product_data
    - 集合命名: {category}__{subcategory}（无后缀，单一集合）
    - 文档通过 clean_status / export_status 字段区分状态：
        clean_status: unclean | cleaned | failed
        export_status: unexported | exported
    - 分类处理/模型优化 状态标识（作用于"已清洗未导出"数据）：
        category_process_status: unprocessed | processed（分类数据处理）
        optimize_status: unoptimized | optimized（模型优化分类）
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

    def _inc_counters(self, collection_key: str, increments: dict, doc_delta: int = 0):
        """批量增减多个状态计数（单次 $inc 操作）

        Args:
            collection_key: 集合名（如 "hardware__tools"）
            increments: {"unclean": -1, "cleaned": 1} -- 各状态的增减量
            doc_delta: 集合文档总数变化量。同一文档的状态迁移传 0（默认），
                       新增/删除文档时传 ±N，保证 total 与集合文档数一致
                       （与 rebuild_product_counters 的 total = sum(clean_counts) 对齐）
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

    def _dec_pool_status_counters(self, prefix: str, category: str, subcategory: str,
                                  cat_processed: int, optimized: int):
        """文档离开"已清洗未导出"池（导出/删除/重置清洗状态）时，扣减状态标识计数

        category_processed / optimized 计数口径为"已清洗未导出且已标记"的文档数。
        计数器文档缺少对应字段时（旧计数器未含新口径），改为整集合重建，
        避免 $inc 对缺失字段产生负数。
        """
        if cat_processed <= 0 and optimized <= 0:
            return
        doc = self._counters_col().find_one(
            {"_id": prefix},
            {"counts.category_processed": 1, "counts.optimized": 1, "_id": 0}
        )
        counts = (doc or {}).get("counts", {}) or {}
        if (cat_processed > 0 and "category_processed" not in counts) or \
                (optimized > 0 and "optimized" not in counts):
            self._rebuild_single_product_counter(category, subcategory)
            return
        incs = {}
        if cat_processed > 0:
            incs["category_processed"] = -cat_processed
        if optimized > 0:
            incs["optimized"] = -optimized
        self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
        self._inc_counters(prefix, incs)

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
        base_pool = {
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        }
        counts = {
            CLEAN_STATUS_UNCLEAN: clean_counts.get(CLEAN_STATUS_UNCLEAN, 0),
            CLEAN_STATUS_CLEANED: clean_counts.get(CLEAN_STATUS_CLEANED, 0),
            CLEAN_STATUS_FAILED: clean_counts.get(CLEAN_STATUS_FAILED, 0),
            "exported": export_counts.get(EXPORT_STATUS_EXPORTED, 0),
            "unexported": export_counts.get(EXPORT_STATUS_UNEXPORTED, 0),
            # 页面“未导出”必须与实际导出查询完全一致，而不是所有状态的 unexported 总数。
            "exportable": col.count_documents(base_pool),
            # 分类数据处理 / 模型优化 状态标识：统计“已清洗未导出”池中已处理/已优化的数量
            "category_processed": col.count_documents({**base_pool,
                "category_process_status": CATEGORY_STATUS_PROCESSED}),
            "optimized": col.count_documents({**base_pool,
                "optimize_status": OPTIMIZE_STATUS_OPTIMIZED}),
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

    def rebuild_product_counters(self, progress_callback=None) -> dict:
        """全量重建所有产品集合的计数器，修复 $inc 漂移

        遍历所有产品集合，用 aggregate $group 统计各状态计数，覆盖写入 _counters。

        Args:
            progress_callback: 进度回调 fn(processed, total, message)，可选

        Returns:
            {"rebuilt": N, "errors": [...]} -- 重建的计数器数量和错误列表
        """
        self.ensure_counters_indexes()
        # 只删除 product 类型的计数器，不影响同库可能存在的其它类型
        self._counters_col().delete_many({"collection_type": "product"})

        rebuilt = 0
        errors = []
        items = self.list_categories_with_sub()
        step_total = len(items)

        for idx, item in enumerate(items, 1):
            category = item["category"]
            subcategory = item["subcategory"]
            try:
                self._rebuild_single_product_counter(category, subcategory)
                rebuilt += 1
            except Exception as e:
                errors.append(f"product {item['prefix']}: {e}")
            if progress_callback:
                # InterruptedError 向上传播以支持任务停止；其它回调异常忽略
                progress_callback(idx, step_total, f"product: {item['prefix']}")

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

        # 分类数据处理 / 模型优化分类 状态标识索引（用于已清洗未导出数据的状态统计与查询）
        col.create_index(
            [("clean_status", ASCENDING), ("export_status", ASCENDING),
             ("category_process_status", ASCENDING)],
            name="idx_clean_export_category_status",
        )
        col.create_index(
            [("clean_status", ASCENDING), ("export_status", ASCENDING),
             ("optimize_status", ASCENDING)],
            name="idx_clean_export_optimize_status",
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
            inserted = len(to_insert)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", []) if exc.details else []
            inserted = max(len(to_insert) - len(write_errors), 0)

        # 同步 _counters：新文档为 unclean + unexported
        if inserted > 0:
            prefix = make_collection_prefix(category, subcategory)
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix,
                               {CLEAN_STATUS_UNCLEAN: inserted, "unexported": inserted},
                               doc_delta=inserted)
        return inserted

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
        # 该公共方法也可能被清洗流程之外调用，更新后重建单集合计数器保证一致。
        if modified:
            self._rebuild_single_product_counter(category, subcategory)
        self._stats_cache.invalidate()
        return modified

    # ── 清洗操作 ──────────────────────────────────────────

    def clean_category(self, category: str, subcategory: str = "", force: bool = False,
                       regenerate_sku: bool = True) -> Dict[str, int]:
        """清洗指定分类的未清洗数据（在同一集合内更新状态，不跨集合移动）

        清洗过程中对通过的数据执行通用标准化并写回：
        标题净化与站点名/域名移除、描述 HTML 白名单清洗、图片取首图、价格规范化、
        分类标准化（分隔符统一|||/单数化/首字母大写/纯数字用 source_category 覆盖）、
        无效分类名清理（simple / 含 undefined / 整值或任一级纯数字 ->
        有 source_category 用它覆盖，否则归入 Other）、
        source_category 下划线转空格、变体截断前2段属性并规范化；
        标题/描述命中品牌黑名单、描述为空或非空变体格式非法的数据直接判为 failed。
        通过的商品 SKU 全部重新生成递增编号。

        Args:
            category: 一级分类名称
            subcategory: 二级分类名称
            force: 是否强制清洗所有数据（包括已清洗的）
            regenerate_sku: 是否为通过的商品重新生成 SKU（默认开启）

        Returns:
            {"processed": 处理数量, "cleaned": 清洗后数量, "removed": 移除数量, "sku_generated": 生成SKU数量}
        """
        from qmds.modules.data_scraper.pipeline.filters import (
            PLACEHOLDER_IMAGES, PROHIBITED_KEYWORDS, MIN_TITLE_LENGTH, MIN_PRICE, MAX_PRICE
        )
        from qmds.utils.language import is_non_english_text
        from qmds.utils.text_cleaner import (
            clean_title_text, clean_description_html, clean_price_value,
            pick_first_image, hit_brand_blacklist,
            remove_site_name, remove_site_domain, site_name_from_domain,
        )
        from qmds.utils.data_cleaner import normalize_categories, is_pure_numeric_category
        from qmds.utils.text_cleaner import (
            SKUGenerator, generate_reference_sku, clean_text,
            normalize_variant_field, truncate_variant_field, is_valid_variant_format,
        )
        from html import unescape

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
            "SKU": 1,
            "折扣价": 1,
            "原价": 1,
            "标题": 1,
            "图片": 1,
            "描述": 1,
            "子描述": 1,
            "分类": 1,
            "变体": 1,
            "source_domain": 1,
            "source_category": 1,
        }

        # SKU 重新编号生成器（regenerate_sku 开启时使用）
        sku_generator = SKUGenerator(generate_reference_sku()) if regenerate_sku else None

        clean_time = datetime.utcnow().isoformat()
        passed_keys = []
        all_keys = set()
        pending_updates = []
        updates_flushed = 0
        sku_generated_count = 0
        stats = {"价格超范围": 0, "标题过短": 0, "描述为空": 0, "占位图": 0, "非英文": 0,
                 "违禁词": 0, "品牌词": 0, "变体无效": 0, "无key": 0}

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

            # ── 数据标准化（通用清洗）──
            # 对应 BB 流程：clean_text(全表净化) -> 各列清洗 -> 站点名/域名移除 -> replace_entities
            source_domain = str(p.get("source_domain", "") or "").strip()
            site_name = site_name_from_domain(source_domain)

            title = clean_title_text(p.get("标题"))
            title = remove_site_name(title, site_name).strip()
            title = remove_site_domain(title, source_domain).strip()

            desc = clean_description_html(clean_text(p.get("描述")))
            sub_desc = clean_description_html(clean_text(p.get("子描述")))
            desc = remove_site_domain(remove_site_name(desc, site_name), source_domain).strip()
            sub_desc = remove_site_domain(remove_site_name(sub_desc, site_name), source_domain).strip()

            img = pick_first_image(p.get("图片"))
            discount_price = clean_price_value(p.get("折扣价"))
            original_price = clean_price_value(p.get("原价"))

            variant = unescape(normalize_variant_field(truncate_variant_field(clean_text(p.get("变体")))))

            # 子描述可能是字符串形式的列表（"[a, b]"），先还原为纯文本用于检测
            if sub_desc.startswith("["):
                try:
                    import ast
                    tags_list = ast.literal_eval(sub_desc)
                    tags_str = " ".join(str(t) for t in tags_list) if isinstance(tags_list, list) else sub_desc
                except Exception:
                    tags_str = sub_desc
            else:
                tags_str = sub_desc

            # 分类标准化：ASCII净化 -> 实体解码 -> 分隔符统一|||/单数化/首字母大写；纯数字分类用 source_category 覆盖
            raw_cat = str(p.get("分类") or "")
            cat_value = normalize_categories(unescape(clean_text(raw_cat))).strip()
            source_cat = clean_text(str(p.get("source_category") or "")).replace("_", " ").strip()
            if cat_value and is_pure_numeric_category(cat_value) and source_cat:
                cat_value = source_cat
            # 无效分类名清理：simple / 含 undefined / 整值或任一级纯数字
            # （用标准化前的原始值判定，避免单数化等转换破坏 undefined 等关键词的检测）
            # -> 有 source_category 用它覆盖，否则归入公共类 Other
            if raw_cat and is_invalid_category_name(raw_cat):
                cat_value = source_cat if source_cat else DEFAULT_INVALID_CATEGORY

            # ── 品牌黑名单（检测范围与违禁词一致：标题+描述+子描述）──
            if hit_brand_blacklist(f"{title} {desc} {tags_str}"):
                stats["品牌词"] += 1
                continue

            # ── 价格 ──
            price = discount_price if discount_price > 0 else original_price
            if not (MIN_PRICE <= price <= MAX_PRICE):
                stats["价格超范围"] += 1
                continue

            # ── 标题长度 ──
            if len(title.strip()) < MIN_TITLE_LENGTH:
                stats["标题过短"] += 1
                continue

            # ── 描述为空（对应表格二次清洗的删空描述规则）──
            if not desc.strip():
                stats["描述为空"] += 1
                continue

            # ── 图片有效性 ──
            if not img or PLACEHOLDER_IMAGES.search(img):
                stats["占位图"] += 1
                continue

            # ── 英文检测 ──
            text = f"{title} {desc}"
            if is_non_english_text(text):
                stats["非英文"] += 1
                continue

            # ── 违禁词 ──
            check_text = f"{title} {desc} {tags_str}".lower()
            if any(kw in check_text for kw in PROHIBITED_KEYWORDS):
                stats["违禁词"] += 1
                continue

            # ── 变体有效性（对应 variants_clean 校验：非空时段数≤2、须为 属性^值 且各值非空）──
            if not is_valid_variant_format(variant):
                stats["变体无效"] += 1
                continue

            # ── 通过：收集标准化后的字段写回 ──
            passed_keys.append(unique_key)
            update_doc = {
                "标题": title,
                "描述": desc,
                "子描述": sub_desc,
                "图片": img,
                "分类": cat_value,
                "source_category": source_cat,
                "变体": variant,
                "原价": f"{original_price:.2f}" if original_price > 0 else "",
                "折扣价": f"{discount_price:.2f}" if discount_price > 0 else "",
            }
            # SKU 重新编号：为通过的商品生成全新递增 SKU（覆盖原值）
            if sku_generator:
                update_doc["SKU"] = sku_generator.generate_sku()
                sku_generated_count += 1
            pending_updates.append(UpdateOne(
                {"unique_key": unique_key},
                {"$set": update_doc}
            ))

            # 分批刷盘，避免大集合时 update 文档（含标题/描述全文）在内存中无限累积
            if len(pending_updates) >= DB_BATCH_SIZE:
                col.bulk_write(pending_updates, ordered=False)
                updates_flushed += len(pending_updates)
                pending_updates = []

            if total % 10000 == 0:
                log.info(f"[{prefix}] 进度: {total}")

        if total == 0:
            log.info(f"[{prefix}] 无待清洗数据")
            return {"processed": 0, "cleaned": 0, "removed": 0, "sku_generated": 0}

        log.info(f"[{prefix}] 待清洗数据: {total} 条")

        # 打印过滤统计
        log.info(f"[{prefix}] 过滤统计:")
        for reason, count in stats.items():
            if count > 0:
                log.info(f"  ├─ {reason}: {count} 条")
        log.info(f"  └─ 通过: {len(passed_keys)} 条")

        # 写回剩余批次（循环内已按 DB_BATCH_SIZE 分批刷盘）
        if pending_updates:
            col.bulk_write(pending_updates, ordered=False)
            updates_flushed += len(pending_updates)
            pending_updates = []
        if updates_flushed:
            log.info(f"[{prefix}] 已写回标准化数据: {updates_flushed} 条")
        if sku_generated_count:
            log.info(f"[{prefix}] 已生成 {sku_generated_count} 个新 SKU")

        # 更新通过的为 cleaned（重新清洗会重写分类字段，状态标识同时重置为未处理/未优化）
        if passed_keys:
            for i in range(0, len(passed_keys), DB_BATCH_SIZE):
                batch = passed_keys[i:i + DB_BATCH_SIZE]
                col.update_many(
                    {"unique_key": {"$in": batch}},
                    {"$set": {
                        "clean_status": CLEAN_STATUS_CLEANED,
                        "clean_time": clean_time,
                        "category_process_status": CATEGORY_STATUS_UNPROCESSED,
                        "optimize_status": OPTIMIZE_STATUS_UNOPTIMIZED,
                    }}
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
            "sku_generated": sku_generated_count,
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
                "raw_count": doc.get("total", (
                counts.get(CLEAN_STATUS_UNCLEAN, 0)
                + counts.get(CLEAN_STATUS_CLEANED, 0)
                + counts.get(CLEAN_STATUS_FAILED, 0)
            )),
                "clean_count": counts.get(CLEAN_STATUS_CLEANED, 0),
                "exported_count": counts.get("exported", 0),
                "unexported_count": counts.get("exportable", 0),
                "unclean_count": counts.get(CLEAN_STATUS_UNCLEAN, 0),
                "cleaned_count": counts.get(CLEAN_STATUS_CLEANED, 0),
                "failed_count": counts.get(CLEAN_STATUS_FAILED, 0),
                "category_processed_count": counts.get("category_processed", 0),
                "optimized_count": counts.get("optimized", 0),
            }

        # 回退：实时 aggregate（计数器缺失时）
        col = self.collection(category, subcategory)
        pipeline = [{"$group": {"_id": "$clean_status", "count": {"$sum": 1}}}]
        status_counts = {doc["_id"]: doc["count"] for doc in col.aggregate(pipeline)}
        total_count = sum(status_counts.values())

        export_pipeline = [{"$group": {"_id": "$export_status", "count": {"$sum": 1}}}]
        export_counts = {doc["_id"]: doc["count"] for doc in col.aggregate(export_pipeline)}

        base_pool = {
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        }
        return {
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "prefix": prefix,
            "raw_count": total_count,  # 集合总数
            "clean_count": status_counts.get(CLEAN_STATUS_CLEANED, 0),
            "exported_count": export_counts.get(EXPORT_STATUS_EXPORTED, 0),
            "unexported_count": col.count_documents(base_pool),
            "unclean_count": status_counts.get(CLEAN_STATUS_UNCLEAN, 0),
            "cleaned_count": status_counts.get(CLEAN_STATUS_CLEANED, 0),
            "failed_count": status_counts.get(CLEAN_STATUS_FAILED, 0),
            "category_processed_count": col.count_documents({**base_pool,
                "category_process_status": CATEGORY_STATUS_PROCESSED}),
            "optimized_count": col.count_documents({**base_pool,
                "optimize_status": OPTIMIZE_STATUS_OPTIMIZED}),
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
            {"total": 1, "counts.cleaned": 1, "counts.unclean": 1,
             "counts.exportable": 1, "counts.exported": 1,
             "counts.category_processed": 1, "counts.optimized": 1, "_id": 0}
        )
        if doc:
            counts = doc.get("counts", {}) or {}
            total = doc.get("total", 0)
            clean_count = counts.get("cleaned", 0)
            unclean_count = counts.get("unclean", 0)
            # exportable = 已清洗且未导出（导出流程 _inc_counters 维护）
            unexported_count = counts.get("exportable", 0)
            category_processed_count = counts.get("category_processed", 0)
            optimized_count = counts.get("optimized", 0)
        else:
            # 回退：estimated_document_count + count_documents（计数器缺失时）
            col = self.collection(category, subcategory)
            total = col.estimated_document_count()
            clean_count = col.count_documents({"clean_status": CLEAN_STATUS_CLEANED})
            unclean_count = col.count_documents({"clean_status": CLEAN_STATUS_UNCLEAN})
            base_pool = {
                "clean_status": CLEAN_STATUS_CLEANED,
                "export_status": EXPORT_STATUS_UNEXPORTED,
            }
            unexported_count = col.count_documents(base_pool)
            category_processed_count = col.count_documents({**base_pool,
                "category_process_status": CATEGORY_STATUS_PROCESSED})
            optimized_count = col.count_documents({**base_pool,
                "optimize_status": OPTIMIZE_STATUS_OPTIMIZED})
        return {
            "category": category,
            "subcategory": normalize_subcategory(subcategory),
            "prefix": prefix,
            "raw_count": total,
            "clean_count": clean_count,
            "unclean_count": unclean_count,
            "unexported_count": unexported_count,
            "category_processed_count": category_processed_count,
            "optimized_count": optimized_count,
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
        if modified:
            self._rebuild_single_product_counter(category, subcategory)
        self._stats_cache.invalidate()
        return modified

    def reset_clean_status(self, category: str, subcategory: str = "") -> int:
        """重置指定分类的清洗状态为未清洗"""
        col = self.collection(category, subcategory)
        prefix = make_collection_prefix(category, subcategory)
        # 先统计将被重置的各状态数量，用于同步 _counters
        reset_cleaned = col.count_documents({"clean_status": CLEAN_STATUS_CLEANED})
        reset_failed = col.count_documents({"clean_status": CLEAN_STATUS_FAILED})
        reset_exportable = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        })
        reset_cat_processed = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
            "category_process_status": CATEGORY_STATUS_PROCESSED,
        })
        reset_optimized = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
            "optimize_status": OPTIMIZE_STATUS_OPTIMIZED,
        })
        result = col.update_many(
            {"clean_status": {"$ne": CLEAN_STATUS_UNCLEAN}},
            {"$set": {"clean_status": CLEAN_STATUS_UNCLEAN, "clean_time": None}}
        )
        modified = result.modified_count
        # 同步 _counters：cleaned/failed 减少对应数量，unclean 增加 modified
        if modified > 0:
            incs = {CLEAN_STATUS_UNCLEAN: modified,
                    CLEAN_STATUS_CLEANED: -reset_cleaned,
                    CLEAN_STATUS_FAILED: -reset_failed,
                    "exportable": -reset_exportable}
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, incs)
            # 重置后文档离开"已清洗未导出"池，同步扣减状态标识计数
            self._dec_pool_status_counters(prefix, category, subcategory,
                                           reset_cat_processed, reset_optimized)
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
        del_exportable = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        })
        del_cat_processed = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
            "category_process_status": CATEGORY_STATUS_PROCESSED,
        })
        del_optimized = col.count_documents({
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
            "optimize_status": OPTIMIZE_STATUS_OPTIMIZED,
        })
        result = col.delete_many({"clean_status": {"$in": [CLEAN_STATUS_CLEANED, CLEAN_STATUS_FAILED]}})
        deleted = result.deleted_count
        # 同步 _counters：各状态减少对应数量，total 减少实际删除数
        if deleted > 0:
            incs = {CLEAN_STATUS_CLEANED: -del_cleaned,
                    CLEAN_STATUS_FAILED: -del_failed,
                    "exported": -del_exported,
                    "unexported": -del_unexported,
                    "exportable": -del_exportable}
            self._set_counter_type(prefix, "product", category, normalize_subcategory(subcategory))
            self._inc_counters(prefix, incs, doc_delta=-deleted)
            # 删除后文档离开"已清洗未导出"池，同步扣减状态标识计数
            self._dec_pool_status_counters(prefix, category, subcategory,
                                           del_cat_processed, del_optimized)
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
        total_category_processed = 0
        total_optimized = 0
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
            total_category_processed += stats.get("category_processed_count", 0)
            total_optimized += stats.get("optimized_count", 0)

        result = {
            "total_categories": len(cat_list),
            "total_raw": total_count,
            "total_clean": total_clean,
            "total_exported": total_exported,
            "total_unclean": total_unclean,
            "total_cleaned": total_cleaned,
            "total_failed": total_failed,
            "total_category_processed": total_category_processed,
            "total_optimized": total_optimized,
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

        # 只保留指定的导出字段（BB 兼容列映射）
        rows = []
        exported_ids = []
        exported_cat_processed = 0
        exported_optimized = 0
        for doc in products:
            # 自定义分类 兜底：source_category 映射不出时按集合大类映射
            row = _sanitize_export_row(
                _build_export_row(doc, fallback_category=category))
            rows.append(row)
            if doc.get("category_process_status") == CATEGORY_STATUS_PROCESSED:
                exported_cat_processed += 1
            if doc.get("optimize_status") == OPTIMIZE_STATUS_OPTIMIZED:
                exported_optimized += 1
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
            self._inc_counters(prefix, {"unexported": -marked, "exported": marked,
                                         "exportable": -marked})
            # 已导出文档离开"已清洗未导出"池，同步扣减状态标识计数
            self._dec_pool_status_counters(prefix, category, subcategory,
                                           exported_cat_processed, exported_optimized)

        self._stats_cache.invalidate()
        log.info(f"导出Excel: {filepath} ({len(rows)} 条)，已标记为已导出")
        return {"filepath": filepath, "count": len(rows)}

    def merge_export_category(self, category: str, export_dir: str,
                              limit: Optional[int] = None,
                              progress_callback=None) -> Optional[Dict[str, Any]]:
        """合并导出一级分类下所有二级分类的已清洗未导出数据到一个 Excel

        遍历该一级分类下的所有二级集合，查询 clean_status=cleaned 且 export_status=unexported
        的文档，按 EXPORT_COLUMNS 提取字段（BB 兼容列映射），按 Name 去重后合并写入单个 Excel 文件，
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
        # 记录每个二级分类参与导出文档中已标记状态的数量，用于同步计数器
        exported_status_counts: Dict[str, Dict[str, int]] = {}

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
            cat_processed_cnt = 0
            optimized_cnt = 0
            for doc in docs:
                # 自定义分类 兜底：source_category 映射不出时按集合大类映射
                row = _sanitize_export_row(
                    _build_export_row(doc, fallback_category=category))
                all_rows.append(row)
                if doc.get("category_process_status") == CATEGORY_STATUS_PROCESSED:
                    cat_processed_cnt += 1
                if doc.get("optimize_status") == OPTIMIZE_STATUS_OPTIMIZED:
                    optimized_cnt += 1
                if doc.get("_id"):
                    ids.append(doc["_id"])
            if ids:
                exported_ids_by_prefix[prefix] = ids
                exported_status_counts[prefix] = {
                    "category_processed": cat_processed_cnt,
                    "optimized": optimized_cnt,
                }

            if progress_callback:
                # 统一进度回调协议：单参数 dict（见 task_manager.make_progress_callback）
                progress_callback({
                    "message": f"[{prefix}] 汇总完成（{idx + 1}/{len(sub_items)}）",
                    "progress": int((idx + 1) / len(sub_items) * 100),
                    "current": idx + 1,
                    "total": len(sub_items),
                })

        if not all_rows:
            log.info(f"合并导出 {category}: 无清洗后数据")
            return None

        # 合并并按 Name 去重
        df = pd.DataFrame(all_rows, columns=EXPORT_COLUMNS)
        before_dedup = len(df)
        if "Name" in df.columns:
            df = df.drop_duplicates(subset=["Name"], keep="first")
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
                self._inc_counters(prefix, {"unexported": -prefix_marked, "exported": prefix_marked,
                                             "exportable": -prefix_marked})
                # 已导出文档离开"已清洗未导出"池，同步扣减状态标识计数
                status_cnt = exported_status_counts.get(prefix, {})
                self._dec_pool_status_counters(
                    prefix, cat, sub,
                    status_cnt.get("category_processed", 0),
                    status_cnt.get("optimized", 0))

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
    # ── 分类数据处理（已清洗未导出） ─────────────────────────

    def process_category_data(self, category: str, subcategory: str = "",
                              threshold: int = 10,
                              common_categories: Optional[List[str]] = None,
                              progress_callback=None,
                              stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        """处理单个集合中"已清洗且未导出"数据的分类字段（跨集合全局处理的单集合特例）

        见 process_category_data_global 的说明。本方法等价于以
        [(category, subcategory)] 调用全局方法，供单集合场景及外部兼容使用。
        """
        return self.process_category_data_global(
            [(category, subcategory)],
            threshold=threshold,
            common_categories=common_categories,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )

    def process_category_data_global(self, cat_list: List[tuple],
                                     threshold: int = 10,
                                     common_categories: Optional[List[str]] = None,
                                     progress_callback=None,
                                     stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        """处理多个集合中"已清洗且未导出"数据的分类字段（跨集合全局统计）

        低频分类合并：所选范围内所有集合**同名分类计数合并**后 < threshold 的，
        确定性轮询归入公共类（同名分类在所有集合分配同一个公共类）。
        （无效分类名清理：simple / undefined / 纯数字 已在数据清洗操作 clean_category
        中处理，此处不再重复。）

        状态标识：范围内所有"已清洗未导出"文档写入 category_process_status=processed
        （含本次未发生合并的文档，表示已纳入本次分类数据处理），同步 _counters 的
        category_processed 计数；不改变 clean_status / export_status，不增删文档。

        Args:
            cat_list: [(category, subcategory), ...] 待处理的集合列表
            threshold: 低频阈值，全局分类计数小于该值的将被合并
            common_categories: 公共类列表（空则默认 ["Other"]）
            progress_callback: 进度回调（接收 dict 或 str）
            stop_event: 停止事件（设置后抛 InterruptedError）

        Returns:
            {"processed": 全局遍历条数, "modified_rows": 全局修改行数,
             "status_marked": 全局新标记 processed 的行数,
             "category_count_before": 全局修改前分类数, "category_count_after": 全局修改后分类数,
             "merged": [(cat, global_count, new_cat), ...],
             "collections": [{"category", "subcategory", "modified_rows", "status_marked"}, ...]}
        """
        from collections import Counter

        if not common_categories:
            common_categories = ["Other"]
        common_categories = [c for c in common_categories if c] or ["Other"]

        query = {
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        }
        projection = {"分类": 1, "category_process_status": 1}

        def _check_stopped():
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("任务被用户停止")

        # 1) 全局统计分类计数（跨集合同名分类合并，流式游标避免 OOM）
        counts = Counter()
        total_processed = 0
        for col_idx, (category, subcategory) in enumerate(cat_list):
            prefix = make_collection_prefix(category, subcategory)
            col = self.collection(category, subcategory)
            cursor = col.find(query, projection).batch_size(2000)
            for doc in cursor:
                _check_stopped()
                total_processed += 1
                cat = doc.get("分类")
                if cat:
                    counts[str(cat)] += 1
            if progress_callback:
                progress_callback({
                    "message": f"[{prefix}] 统计完成（{col_idx + 1}/{len(cat_list)}）",
                    "progress": int((col_idx + 1) / len(cat_list) * 100),
                })

        if total_processed == 0:
            self._stats_cache.invalidate()
            return {"processed": 0, "modified_rows": 0, "status_marked": 0,
                    "category_count_before": 0, "category_count_after": 0,
                    "merged": [], "collections": []}

        # 2) 低频分类（基于全局计数，按 计数降序/名称 排序保证确定性轮询可复现）
        low_freq = [(cat, cnt) for cat, cnt in counts.items() if cat and cnt < threshold]
        low_freq.sort(key=lambda x: (-x[1], x[0]))

        # 构建 旧分类 -> 新公共类 映射（全局同一轮询索引，同名分类分配同一公共类）
        mapping: Dict[str, str] = {}
        idx = 0
        for cat, _cnt in low_freq:
            mapping[cat] = common_categories[idx % len(common_categories)]
            idx += 1

        if not mapping:
            # 无需合并，但仍继续走写回流程：为范围内未标记的文档补状态标识
            pass

        # 3) 逐集合分批写回"分类"字段 + 状态标识（同时增量统计全局修改后的分类数）
        now = datetime.utcnow().isoformat()
        total_modified = 0
        total_marked = 0
        after_counts = Counter()
        collections_detail = []
        for col_idx, (category, subcategory) in enumerate(cat_list):
            prefix = make_collection_prefix(category, subcategory)
            col = self.collection(category, subcategory)
            pending = []
            modified_rows = 0
            status_marked = 0
            cursor = col.find(query, projection).batch_size(2000)
            for doc in cursor:
                _check_stopped()
                cat = doc.get("分类")
                cat_str = str(cat) if cat else ""
                new_cat = mapping.get(cat_str) if cat_str else None
                need_mark = doc.get("category_process_status") != CATEGORY_STATUS_PROCESSED
                if new_cat is not None and new_cat != cat_str:
                    # 低频合并：改写分类并标记状态
                    set_doc = {"分类": new_cat,
                               "category_process_status": CATEGORY_STATUS_PROCESSED,
                               "category_process_time": now}
                    pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": set_doc}))
                    modified_rows += 1
                    if need_mark:
                        status_marked += 1
                    after_counts[new_cat] += 1
                else:
                    # 分类不变：仅补状态标识（已标记过的跳过，避免重复写）
                    if cat_str:
                        after_counts[cat_str] += 1
                    if need_mark:
                        set_doc = {"category_process_status": CATEGORY_STATUS_PROCESSED,
                                   "category_process_time": now}
                        pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": set_doc}))
                        status_marked += 1
                if len(pending) >= DB_BATCH_SIZE:
                    col.bulk_write(pending, ordered=False)
                    pending = []
                    if progress_callback:
                        progress_callback({
                            "message": f"[{prefix}] 已更新 {modified_rows} 条...",
                            "progress": int((col_idx + 1) / len(cat_list) * 100),
                        })
            if pending:
                col.bulk_write(pending, ordered=False)
                pending = []
            total_modified += modified_rows
            total_marked += status_marked
            collections_detail.append({
                "category": category,
                "subcategory": subcategory,
                "modified_rows": modified_rows,
                "status_marked": status_marked,
            })
            # 有写入则重建该集合计数器（含 category_processed 口径，避免增量漂移）
            if modified_rows or status_marked:
                self._rebuild_single_product_counter(category, subcategory)
            if progress_callback:
                progress_callback({
                    "message": f"[{prefix}] 写回完成（{col_idx + 1}/{len(cat_list)}）",
                    "progress": int((col_idx + 1) / len(cat_list) * 100),
                })

        self._stats_cache.invalidate()

        # 过滤映射到自身（如低频分类恰好等于公共类）的项：写回时被跳过，不计入实际合并
        merged = [(cat, cnt, new_cat) for cat, cnt, new_cat in
                  ((cat, cnt, mapping[cat]) for cat, cnt in low_freq)
                  if new_cat != cat]

        return {
            "processed": total_processed,
            "modified_rows": total_modified,
            "status_marked": total_marked,
            "category_count_before": len([c for c in counts if c]),
            "category_count_after": len([c for c in after_counts if c]),
            "merged": merged,
            "collections": collections_detail,
        }

    # ── 模型优化分类（已清洗未导出） ─────────────────────────

    def optimize_category_data_global(self, cat_list: List[tuple],
                                       log_callback=None,
                                       progress_callback=None,
                                       stop_event: Optional[threading.Event] = None,
                                       site_db=None) -> Dict[str, Any]:
        """模型优化分类（数据库版）：对多个集合中"已清洗且未导出"数据的分类字段调用 LLM 优化

        规则与表格版（tools/category-optimize 文件模式）一致：同义分类统一为单一标准
        表达、单级分类补全直接父级（||| 分隔、最多两级），21个一级大类保持不变。

        跨大类转移：映射带有效 top（所属一级大类）且与当前集合的大类不一致时，
        该文档转移到对应大类的 other 集合（保留 _id/清洗/导出状态，写
        optimize_moved_from 审计字段）。目标集合已存在同 unique_key 商品时跳过
        转移（仅原地标记），避免重复商品。

        状态标识：范围内所有"已清洗未导出"文档写入 optimize_status=optimized
        （含映射未命中的文档，表示已纳入本次模型优化），同步 _counters 的 optimized
        计数；不改变 clean_status / export_status。

        Args:
            cat_list: [(category, subcategory), ...] 待处理的集合列表
            log_callback: 日志回调 fn(message, level)
            progress_callback: 进度回调（接收 dict 或 str）
            stop_event: 停止事件（设置后抛 InterruptedError）
            site_db: SiteDBClient，用于读取 LLM 模型配置（可选）

        Returns:
            {"processed": 全局遍历条数, "unique_categories": 唯一分类数,
             "mappings_count": 有效映射数, "modified_rows": 全局修改行数,
             "status_marked": 全局新标记 optimized 的行数,
             "moved": 跨大类转移条数, "moved_skipped": 因目标重复跳过转移条数,
             "moved_targets": {目标集合前缀: 转移条数},
             "mappings": {原分类: {"optimized": 新分类, "top": 所属一级大类|None}},
             "collections": [{"category", "subcategory", "modified_rows",
                              "status_marked", "moved_out"}, ...]}
        """
        def _log(msg, level="info"):
            if log_callback:
                log_callback(msg, level)

        def _check_stopped():
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("任务被用户停止")

        query = {
            "clean_status": CLEAN_STATUS_CLEANED,
            "export_status": EXPORT_STATUS_UNEXPORTED,
        }

        # 1) 跨集合收集唯一分类（流式游标避免 OOM）
        unique_cats = set()
        total_processed = 0
        for col_idx, (category, subcategory) in enumerate(cat_list):
            prefix = make_collection_prefix(category, subcategory)
            col = self.collection(category, subcategory)
            cursor = col.find(query, {"分类": 1}).batch_size(2000)
            for doc in cursor:
                _check_stopped()
                total_processed += 1
                cat = doc.get("分类")
                if cat:
                    unique_cats.add(str(cat))
            if progress_callback:
                progress_callback({
                    "message": f"[{prefix}] 分类收集完成（{col_idx + 1}/{len(cat_list)}）",
                    "progress": int((col_idx + 1) / len(cat_list) * 50),
                })

        if total_processed == 0:
            self._stats_cache.invalidate()
            return {"processed": 0, "unique_categories": 0, "mappings_count": 0,
                    "modified_rows": 0, "status_marked": 0, "mappings": {},
                    "collections": []}

        # 2) 调用 LLM 构建优化映射（预过滤/参考路径/分批/校验/模糊二次判定与文件模式共用）
        _log(f"已清洗未导出 {total_processed} 条，唯一分类 {len(unique_cats)} 个")
        if progress_callback:
            progress_callback({"message": "正在调用模型构建分类优化映射...", "progress": 50})

        from qmds.utils.category_optimizer import build_optimize_mappings

        def _sample_fetcher(cat: str, n: int) -> List[str]:
            """模糊分类二次判定用：取该分类下 n 个已清洗未导出商品的文本样本

            同一分类名可能出现在多个集合，按顺序从第一个能取到样本的集合采样。
            返回 ["标题 | 描述前80字符", ...]（截断到 160 字符）
            """
            for category, subcategory in cat_list:
                col = self.collection(category, subcategory)
                cursor = col.find(
                    {**query, "分类": cat},
                    {"标题": 1, "描述": 1},
                ).limit(n)
                samples = []
                for doc in cursor:
                    _check_stopped()
                    title = str(doc.get("标题") or "").strip()
                    desc = str(doc.get("描述") or "").strip()
                    if not title and not desc:
                        continue
                    frag = (title + " | " + desc[:80]) if (title and desc) else (title or desc)
                    samples.append(frag[:160])
                if samples:
                    return samples
            return []

        valid_mappings = build_optimize_mappings(
            sorted(unique_cats), log_callback, site_db=site_db,
            sample_fetcher=_sample_fetcher)
        _check_stopped()
        _log(f"模型返回有效映射 {len(valid_mappings)} 个")

        # 3) 逐集合分批写回优化后分类 + 状态标识；跨大类商品转移到对应大类集合
        now = datetime.utcnow().isoformat()
        google_to_shopify = {v: k for k, v in SHOPIFY_TO_GOOGLE_CATEGORY.items()}
        total_modified = 0
        total_marked = 0
        total_moved = 0
        total_moved_skipped = 0
        moved_targets: Dict[str, int] = {}
        collections_detail = []

        def _flush_moves(moves, source_col):
            """执行跨大类转移：按目标集合分组，目标已有同款（unique_key）则跳过。

            先插入目标集合、成功后再删除源文档，保证不丢数据。
            返回 (实际转移数, 因目标重复跳过数)。
            """
            moved = 0
            skipped = 0
            by_target: Dict[str, list] = {}
            for doc in moves:
                by_target.setdefault(doc.pop("_move_target"), []).append(doc)
            for target_cat, docs in by_target.items():
                target_col = self.collection(target_cat, "other")
                self.ensure_product_indexes(target_cat, "other")
                # 目标集合已存在的 unique_key（分批 $in 查询）
                existing_keys = set()
                keys = [d.get("unique_key") for d in docs if d.get("unique_key")]
                for i in range(0, len(keys), DB_BATCH_SIZE):
                    batch = keys[i:i + DB_BATCH_SIZE]
                    for item in target_col.find(
                            {"unique_key": {"$in": batch}}, {"unique_key": 1}):
                        existing_keys.add(item["unique_key"])
                insert_ops = []
                delete_ids = []
                for d in docs:
                    uk = d.get("unique_key")
                    if uk and uk in existing_keys:
                        # 目标已有同款商品：不转移，原地写优化结果（保持数据一致）
                        skipped += 1
                        source_col.update_one(
                            {"_id": d["_id"]},
                            {"$set": {"分类": d["分类"],
                                      "optimize_status": OPTIMIZE_STATUS_OPTIMIZED,
                                      "optimize_time": now}},
                        )
                        continue
                    insert_ops.append(InsertOne(d))
                    delete_ids.append(d["_id"])
                if not insert_ops:
                    continue
                inserted = len(insert_ops)
                try:
                    target_col.bulk_write(insert_ops, ordered=False)
                except BulkWriteError as exc:
                    # 部分插入失败：只删除成功插入的源文档，失败的保留待下次重试
                    write_errors = exc.details.get("writeErrors", []) if exc.details else []
                    failed_ids = set()
                    for we in write_errors:
                        op = we.get("op")
                        if isinstance(op, dict) and "_id" in op:
                            failed_ids.add(op["_id"])
                    inserted = len(insert_ops) - len(failed_ids)
                    _log(f"跨大类转移部分失败: 目标 {target_cat}__other "
                         f"插入 {inserted} 条, 失败 {len(failed_ids)} 条（失败项源数据保留）",
                         "warning")
                    delete_ids = [i for i in delete_ids if i not in failed_ids]
                if inserted > 0:
                    moved += inserted
                    moved_targets[make_collection_prefix(target_cat, "other")] = \
                        moved_targets.get(make_collection_prefix(target_cat, "other"), 0) + inserted
                for i in range(0, len(delete_ids), DB_BATCH_SIZE):
                    source_col.delete_many(
                        {"_id": {"$in": delete_ids[i:i + DB_BATCH_SIZE]}})
            return moved, skipped

        for col_idx, (category, subcategory) in enumerate(cat_list):
            prefix = make_collection_prefix(category, subcategory)
            col = self.collection(category, subcategory)
            pending = []
            pending_moves = []
            modified_rows = 0
            status_marked = 0
            moved_out = 0
            moved_skipped = 0
            # 全量字段读取：跨大类转移需要完整文档
            cursor = col.find(query).batch_size(2000)
            for doc in cursor:
                _check_stopped()
                cat = doc.get("分类")
                cat_str = str(cat) if cat else ""
                entry = valid_mappings.get(cat_str) if cat_str else None
                new_cat = entry["optimized"] if entry else None
                top_google = entry.get("top") if entry else None
                target_shopify = google_to_shopify.get(top_google) if top_google else None
                need_mark = doc.get("optimize_status") != OPTIMIZE_STATUS_OPTIMIZED
                if entry is not None and target_shopify and target_shopify != category:
                    # 模型判定不属于当前大类：转移到对应大类的 other 集合
                    doc["分类"] = new_cat
                    doc["optimize_status"] = OPTIMIZE_STATUS_OPTIMIZED
                    doc["optimize_time"] = now
                    doc["optimize_moved_from"] = prefix
                    doc["_move_target"] = target_shopify
                    pending_moves.append(doc)
                elif new_cat is not None and new_cat != cat_str:
                    # 命中映射：改写分类并标记状态
                    set_doc = {"分类": new_cat,
                               "optimize_status": OPTIMIZE_STATUS_OPTIMIZED,
                               "optimize_time": now}
                    pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": set_doc}))
                    modified_rows += 1
                    if need_mark:
                        status_marked += 1
                elif need_mark:
                    # 映射未命中：仅补状态标识（已标记过的跳过，避免重复写）
                    set_doc = {"optimize_status": OPTIMIZE_STATUS_OPTIMIZED,
                               "optimize_time": now}
                    pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": set_doc}))
                    status_marked += 1
                if len(pending) >= DB_BATCH_SIZE:
                    col.bulk_write(pending, ordered=False)
                    pending = []
                    if progress_callback:
                        progress_callback({
                            "message": f"[{prefix}] 已更新 {modified_rows} 条...",
                            "progress": 50 + int((col_idx + 1) / len(cat_list) * 50),
                        })
                if len(pending_moves) >= DB_BATCH_SIZE:
                    m, s = _flush_moves(pending_moves, col)
                    moved_out += m
                    moved_skipped += s
                    pending_moves = []
            if pending:
                col.bulk_write(pending, ordered=False)
                pending = []
            if pending_moves:
                m, s = _flush_moves(pending_moves, col)
                moved_out += m
                moved_skipped += s
                pending_moves = []
            total_modified += modified_rows
            total_marked += status_marked
            total_moved += moved_out
            total_moved_skipped += moved_skipped
            collections_detail.append({
                "category": category,
                "subcategory": subcategory,
                "modified_rows": modified_rows,
                "status_marked": status_marked,
                "moved_out": moved_out,
            })
            # 有写入则重建该集合计数器（含 optimized 口径，避免增量漂移）
            if modified_rows or status_marked or moved_out:
                self._rebuild_single_product_counter(category, subcategory)
            if progress_callback:
                progress_callback({
                    "message": f"[{prefix}] 写回完成（{col_idx + 1}/{len(cat_list)}）",
                    "progress": 50 + int((col_idx + 1) / len(cat_list) * 50),
                })

        # 转移目标集合的计数器重建（源集合在上面循环内已重建）
        for target_prefix in moved_targets:
            t_cat, t_sub = parse_collection_prefix(target_prefix)
            self._rebuild_single_product_counter(t_cat, t_sub)

        self._stats_cache.invalidate()

        return {
            "processed": total_processed,
            "unique_categories": len(unique_cats),
            "mappings_count": len(valid_mappings),
            "modified_rows": total_modified,
            "status_marked": total_marked,
            "moved": total_moved,
            "moved_skipped": total_moved_skipped,
            "moved_targets": moved_targets,
            "mappings": valid_mappings,
            "collections": collections_detail,
        }
