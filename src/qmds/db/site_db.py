from datetime import datetime
from typing import Optional

import time
import threading

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("site_db")


class _TTLCache:
    """简单的线程安全 TTL 缓存"""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: dict = {}  # key -> (value, expire_ts)
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


class SiteDBClient:
    """站点管理数据库客户端 - 使用MongoDB存储站点数据"""

    _stats_cache = _TTLCache(ttl_seconds=60)  # 类级别缓存

    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self._uri = uri or settings.mongo_uri
        self._db_name = db_name or "qmds_site_management"
        self._client: Optional[MongoClient] = None

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        return self._client

    @property
    def db(self):
        return self.client[self._db_name]

    @property
    def sites(self) -> Collection:
        return self.db["sites"]

    @property
    def settings(self) -> Collection:
        return self.db["settings"]

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def ensure_indexes(self):
        """创建索引"""
        self.sites.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        self.sites.create_index([("report_status", ASCENDING)], name="idx_report_status")
        self.sites.create_index([("build_status", ASCENDING)], name="idx_build_status")
        self.sites.create_index([("schedule_enabled", ASCENDING)], name="idx_schedule_enabled")
        self.sites.create_index([("category", ASCENDING)], name="idx_category")
        log.info("站点数据库索引已创建")

    # ── 站点 CRUD 操作 ──────────────────────────────────────

    def add_site(self, site_data: dict) -> str:
        """添加新站点"""
        ts = datetime.utcnow().isoformat()
        site_data.setdefault("updated_at", ts)
        site_data.setdefault("report_status", "未报")
        site_data.setdefault("build_status", "")
        site_data.setdefault("schedule_enabled", "0")
        site_data.setdefault("schedule_time", "")

        # 将created_at从site_data中分离，避免$set和$setOnInsert冲突
        created_at = site_data.pop("created_at", ts)

        result = self.sites.update_one(
            {"domain": site_data.get("domain", "")},
            {"$set": site_data, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )
        self._stats_cache.invalidate()
        log.info(f"添加/更新站点: {site_data.get('domain', '')}")
        return str(result.upserted_id or site_data.get("domain", ""))

    def update_site(self, domain: str, updates: dict) -> bool:
        """更新站点信息"""
        updates["updated_at"] = datetime.utcnow().isoformat()
        result = self.sites.update_one(
            {"domain": domain},
            {"$set": updates}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count > 0

    def update_site_by_id(self, site_id: str, updates: dict) -> bool:
        """通过ID更新站点信息"""
        from bson import ObjectId
        updates["updated_at"] = datetime.utcnow().isoformat()
        try:
            result = self.sites.update_one(
                {"_id": ObjectId(site_id)},
                {"$set": updates}
            )
            if result.modified_count > 0:
                self._stats_cache.invalidate()
            return result.modified_count > 0
        except Exception:
            return False

    def delete_site(self, domain: str) -> bool:
        """删除站点"""
        result = self.sites.delete_one({"domain": domain})
        if result.deleted_count > 0:
            self._stats_cache.invalidate()
        return result.deleted_count > 0

    def delete_sites_by_ids(self, site_ids: list[str]) -> int:
        """批量删除站点"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue
        result = self.sites.delete_many({"_id": {"$in": object_ids}})
        if result.deleted_count > 0:
            self._stats_cache.invalidate()
        return result.deleted_count

    def get_site(self, domain: str) -> Optional[dict]:
        """获取单个站点"""
        return self.sites.find_one({"domain": domain})

    def get_site_by_id(self, site_id: str) -> Optional[dict]:
        """通过ID获取站点"""
        from bson import ObjectId
        try:
            return self.sites.find_one({"_id": ObjectId(site_id)})
        except Exception:
            return None

    # ── 查询操作 ──────────────────────────────────────────────

    def _paginate(self, query: dict, keyword: str, sort_field: str, sort_dir: int,
                  page: int = 1, page_size: int = 20, category: str = "") -> dict:
        """通用分页查询，返回 {"items": [...], "total": int, "page": int, "page_size": int}"""
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        if category:
            query["category"] = {"$regex": f"^{category}$", "$options": "i"}
        total = self.sites.count_documents(query)
        skip = (page - 1) * page_size
        items = list(self.sites.find(query).sort(sort_field, sort_dir).skip(skip).limit(page_size))
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def list_all_sites(self, keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
        """列出所有站点（分页）"""
        return self._paginate({}, keyword, "created_at", -1, page, page_size)

    def list_active_sites(self, keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
        """列出活跃站点（排除已建站，分页）"""
        return self._paginate({"build_status": {"$ne": "已建站"}}, keyword, "created_at", -1, page, page_size)

    def list_local_sites(self, keyword: str = "", page: int = 1, page_size: int = 20, category: str = "") -> dict:
        """列出本地站点（未上报的站点，分页）"""
        return self._paginate({"report_status": {"$ne": "已报"}}, keyword, "created_at", -1, page, page_size, category)

    def list_local_categories(self) -> list[str]:
        """列出本地站点（未上报）中实际存在的大类，去除空值，按名称排序"""
        try:
            names = self.sites.distinct("category", {"report_status": {"$ne": "已报"}})
        except Exception:
            return []
        return sorted({n for n in names if n and str(n).strip()})

    def list_reported_sites(self, keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
        """列出已报域名（分页），待建站排最上面，然后按建站时间倒序"""
        query = {"report_status": "已报"}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        total = self.sites.count_documents(query)
        skip = (page - 1) * page_size
        items = list(self.sites.find(query).sort([("build_status", 1), ("build_time", -1)]).skip(skip).limit(page_size))
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def list_scheduled_sites(self, keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
        """列出计划上报的站点（分页）"""
        return self._paginate({"schedule_enabled": "1", "report_status": {"$ne": "已报"}}, keyword, "schedule_time", 1, page, page_size)

    def list_built_sites(self, keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
        """列出已建站的站点（分页），未处理的排最前面"""
        from bson import SON

        query = {"build_status": "已建站"}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}

        pipeline = [
            {"$match": query},
            {"$addFields": {
                "pending_score": {
                    "$add": [
                        {"$cond": [{"$ne": [{"$ifNull": ["$health_status", ""]}, "正常"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$main_data_status", ""]}, "已上传"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$extra_data_status", ""]}, "已上传"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$main_category_status", ""]}, "已上传"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$auto_category_status", ""]}, "已配置"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$plugin_status", ""]}, "已配置"]}, 1, 0]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$media_status", ""]}, "已配置"]}, 1, 0]},
                    ]
                }
            }},
            {"$sort": SON([("pending_score", -1), ("build_time", -1)])},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]

        total = self.sites.count_documents(query)
        items = list(self.sites.aggregate(pipeline))
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    # ── 统计操作 ──────────────────────────────────────────────

    def get_stats(self, use_cache: bool = True) -> dict:
        """获取站点统计信息（单次聚合查询 + 缓存）"""
        cache_key = "site_stats"

        if use_cache:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                return cached

        # 用单次聚合查询获取总数、report_status、build_status 分布
        pipeline = [
            {"$facet": {
                "total": [{"$count": "count"}],
                "report_status": [
                    {"$group": {"_id": "$report_status", "count": {"$sum": 1}}}
                ],
                "build_status": [
                    {"$group": {"_id": "$build_status", "count": {"$sum": 1}}}
                ],
                "schedule_enabled": [
                    {"$match": {"schedule_enabled": "1", "report_status": {"$ne": "已报"}}},
                    {"$count": "count"}
                ]
            }}
        ]

        result = list(self.sites.aggregate(pipeline))
        if not result:
            stats = {"total_sites": 0, "local_sites": 0, "reported_sites": 0,
                     "scheduled_sites": 0, "built_sites": 0}
        else:
            facet = result[0]
            total = facet["total"][0]["count"] if facet.get("total") else 0
            report_map = {doc["_id"]: doc["count"] for doc in facet.get("report_status", [])}
            build_map = {doc["_id"]: doc["count"] for doc in facet.get("build_status", [])}
            scheduled_list = facet.get("schedule_enabled", [])

            reported = report_map.get("已报", 0)
            built = build_map.get("已建站", 0)
            scheduled = scheduled_list[0]["count"] if scheduled_list else 0

            stats = {
                "total_sites": total,
                "local_sites": total - reported,
                "reported_sites": reported,
                "scheduled_sites": scheduled,
                "built_sites": built,
            }

        if use_cache:
            self._stats_cache.set(cache_key, stats)

        return stats

    # ── 批量操作 ──────────────────────────────────────────────

    def batch_update_report_status(self, site_ids: list[str], status: str) -> int:
        """批量更新上报状态"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"report_status": status, "updated_at": ts}}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def batch_update_build_status(self, site_ids: list[str], status: str) -> int:
        """批量更新建站状态"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"build_status": status, "build_time": ts, "updated_at": ts}}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def batch_set_schedule(self, site_ids: list[str], schedule_time: str) -> int:
        """批量设置计划时间"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"schedule_enabled": "1", "schedule_time": schedule_time, "updated_at": ts}}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def batch_clear_schedule(self, site_ids: list[str]) -> int:
        """批量清除计划"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"schedule_enabled": "0", "schedule_time": "", "updated_at": ts}}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    # ── 导入导出 ──────────────────────────────────────────────

    @staticmethod
    def _clean_data_source_id(raw) -> str:
        """清洗数据源ID：去空格、去首尾逗号、浮点数转整数、NaN转空"""
        import math
        if raw is None:
            return ""
        try:
            if isinstance(raw, float) and math.isnan(raw):
                return ""
        except Exception:
            pass
        s = str(raw).strip()
        if not s or s.lower() in ("nan", "none"):
            return ""
        parts = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if part.replace(".", "", 1).isdigit() and "." in part:
                part = part.split(".")[0]
            parts.append(part)
        result = ",".join(parts)
        return result.strip(",")

    def import_from_excel(self, filepath: str) -> dict:
        """从Excel文件导入站点数据"""
        import pandas as pd
        df = pd.read_excel(filepath)
        created = 0
        updated = 0
        skipped = 0
        errors = []

        for _, row in df.iterrows():
            domain = str(row.get("域名", "") or row.get("domain", "")).strip()
            if not domain:
                continue

            # 如果"是否建站"列为"是"，跳过该行
            build_flag = str(row.get("是否建站", "")).strip()
            if build_flag == "是":
                skipped += 1
                continue

            site_data = {
                "domain": domain,
                "template": str(row.get("底板", "") or row.get("模板", "") or row.get("template", "")),
                "server": str(row.get("服务器", "") or row.get("server", "")),
                "category": str(row.get("大类", "") or row.get("category", "")),
                "main_category": str(row.get("主分类", "") or row.get("main_category", "")),
                "main_data_source_id": self._clean_data_source_id(row.get("主分类数据码", "") or row.get("main_data_source_id", "")),
                "extra_data_source_id": self._clean_data_source_id(row.get("站群数据码", "") or row.get("extra_data_source_id", "")),
                "title": str(row.get("SEO Title", "") or row.get("SEO Title（最大58字符）", "") or row.get("title", "")),
                "description": str(row.get("Meta Description", "") or row.get("description", "")),
                "address": str(row.get("地址", "") or row.get("address", "")),
            }

            existing = self.get_site(domain)
            if existing:
                # 检查数据是否一致
                needs_update = False
                for key, value in site_data.items():
                    if key == "domain":
                        continue
                    if existing.get(key, "") != value:
                        needs_update = True
                        break
                
                if needs_update:
                    self.update_site(domain, site_data)
                    updated += 1
                else:
                    skipped += 1
            else:
                self.add_site(site_data)
                created += 1

        log.info(f"Excel导入完成: 新增 {created}, 更新 {updated}, 跳过 {skipped}, 错误 {len(errors)}")
        if created > 0 or updated > 0:
            self._stats_cache.invalidate()
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        }

    def export_reported_weekly(self, keyword: str = "") -> list[dict]:
        """导出本周已报域名数据"""
        from datetime import timedelta
        now = datetime.utcnow()
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)

        query = {
            "report_status": "已报",
            "report_time": {
                "$gte": week_start.isoformat(),
                "$lt": week_end.isoformat()
            }
        }
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}

        sites = list(self.sites.find(query, {
            "domain": 1, "template": 1, "server": 1, "report_time": 1, "_id": 0
        }).sort("report_time", -1))

        result = []
        for site in sites:
            result.append({
                "创建时间": site.get("report_time", ""),
                "域名": site.get("domain", ""),
                "模板": site.get("template", ""),
                "服务器": site.get("server", ""),
            })
        return result

    def batch_update_fields(self, site_ids: list[str], field: str, value: str) -> int:
        """批量更新指定字段"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {field: value, "updated_at": ts}}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def get_site_count(self) -> int:
        """获取站点总数（精确计数）"""
        pipeline = [{"$count": "count"}]
        result = list(self.sites.aggregate(pipeline))
        return result[0]["count"] if result else 0

    # ── 配置管理 ──────────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        """获取配置项"""
        doc = self.settings.find_one({"key": key})
        return doc.get("value", default) if doc else default

    def set_setting(self, key: str, value: str) -> bool:
        """设置配置项"""
        result = self.settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

    def get_all_settings(self) -> dict:
        """获取所有配置"""
        docs = self.settings.find({}, {"key": 1, "value": 1, "_id": 0})
        return {doc["key"]: doc.get("value", "") for doc in docs}

    def init_default_settings(self):
        """初始化默认配置"""
        defaults = {
            "report_username": "liwei",
            "report_password": "123456",
            "erp_username": "",
            "erp_password": "",
            "wp_password": "",
            "media_root": "logo",
        }
        for key, value in defaults.items():
            existing = self.get_setting(key)
            if not existing:
                self.set_setting(key, value)
        log.info("默认配置已初始化")

    # ── 选项管理 ──────────────────────────────────────────────

    @property
    def template_options(self) -> Collection:
        return self.db["template_options"]

    @property
    def server_options(self) -> Collection:
        return self.db["server_options"]

    @property
    def main_category_options(self) -> Collection:
        return self.db["main_category_options"]

    def get_template_options(self) -> list[str]:
        """获取模板选项列表"""
        docs = self.template_options.find({}, {"name": 1, "_id": 0}).sort("name", 1)
        return [doc["name"] for doc in docs]

    def add_template_option(self, name: str) -> bool:
        """添加模板选项"""
        try:
            self.template_options.insert_one({"name": name})
            return True
        except Exception:
            return False

    def delete_template_option(self, name: str) -> bool:
        """删除模板选项"""
        result = self.template_options.delete_one({"name": name})
        return result.deleted_count > 0

    def get_server_options(self) -> list[str]:
        """获取服务器选项列表"""
        docs = self.server_options.find({}, {"name": 1, "_id": 0}).sort("name", 1)
        return [doc["name"] for doc in docs]

    def add_server_option(self, name: str) -> bool:
        """添加服务器选项"""
        try:
            self.server_options.insert_one({"name": name})
            return True
        except Exception:
            return False

    def delete_server_option(self, name: str) -> bool:
        """删除服务器选项"""
        result = self.server_options.delete_one({"name": name})
        return result.deleted_count > 0

    def get_main_category_options(self) -> list[dict]:
        """获取主分类选项列表"""
        docs = self.main_category_options.find({}, {"name": 1, "parent_id": 1, "_id": 0}).sort("name", 1)
        return [{"name": doc["name"], "parent_id": doc.get("parent_id", 0)} for doc in docs]

    def add_main_category_option(self, name: str, parent_id: int = 0) -> bool:
        """添加主分类选项"""
        try:
            self.main_category_options.insert_one({"name": name, "parent_id": parent_id})
            return True
        except Exception:
            return False

    def delete_main_category_option(self, name: str) -> bool:
        """删除主分类选项"""
        result = self.main_category_options.delete_one({"name": name})
        return result.deleted_count > 0

    # ── 已建站配置状态管理 ──────────────────────────────────────

    def _batch_update_field(self, site_ids: list[str], field: str, value, extra_fields: dict = None) -> int:
        """通用批量更新字段（内部方法）"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        set_data = {field: value, "updated_at": ts}
        if extra_fields:
            set_data.update(extra_fields)

        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": set_data}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def batch_update_health_status(self, site_ids: list[str], status: str) -> int:
        """批量更新健康检查状态"""
        return self._batch_update_field(site_ids, "health_status", status, {"health_time": datetime.utcnow().isoformat()})

    def batch_update_main_data_status(self, site_ids: list[str], status: str) -> int:
        """批量更新主数据上传状态"""
        return self._batch_update_field(site_ids, "main_data_status", status, {"main_data_time": datetime.utcnow().isoformat()})

    def batch_update_extra_data_status(self, site_ids: list[str], status: str) -> int:
        """批量更新补充数据上传状态"""
        return self._batch_update_field(site_ids, "extra_data_status", status, {"extra_data_time": datetime.utcnow().isoformat()})

    def batch_update_main_category_status(self, site_ids: list[str], status: str) -> int:
        """批量更新主分类设置状态"""
        return self._batch_update_field(site_ids, "main_category_status", status, {"main_category_time": datetime.utcnow().isoformat()})

    def batch_update_plugin_status(self, site_ids: list[str], status: str) -> int:
        """批量更新插件配置状态"""
        return self._batch_update_field(site_ids, "plugin_status", status, {"plugin_time": datetime.utcnow().isoformat()})

    def batch_update_media_status(self, site_ids: list[str], status: str) -> int:
        """批量更新媒体配置状态"""
        return self._batch_update_field(site_ids, "media_status", status, {"media_time": datetime.utcnow().isoformat()})

    def batch_update_auto_category_status(self, site_ids: list[str], status: str) -> int:
        """批量更新菜单/自动分类状态"""
        return self._batch_update_field(site_ids, "auto_category_status", status, {"auto_category_time": datetime.utcnow().isoformat()})

    def update_one_click_progress(self, domain: str, step: str, status: str, detail: str = "") -> bool:
        """更新一键建站单步进度（断点记忆）

        step 取值: configure_sites | upload_main | set_main_category | upload_extra | ai_configure_menu
        status 取值: pending | running | success | failed
        """
        ts = datetime.utcnow().isoformat()
        field = f"one_click_{step}"
        updates = {
            field: status,
            "one_click_current_step": step,
            "one_click_last_update": ts,
            "updated_at": ts,
        }
        if detail:
            updates[f"one_click_{step}_detail"] = detail
        result = self.sites.update_one(
            {"domain": domain},
            {"$set": updates}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count > 0

    def reset_one_click_progress(self, domain: str) -> bool:
        """重置一键建站进度（清除所有步骤状态）"""
        ts = datetime.utcnow().isoformat()
        unset_fields = {
            f"one_click_{s}": ""
            for s in ("configure_sites", "upload_main", "set_main_category",
                      "upload_extra", "ai_configure_menu")
        }
        unset_fields.update({
            f"one_click_{s}_detail": ""
            for s in ("configure_sites", "upload_main", "set_main_category",
                      "upload_extra", "ai_configure_menu")
        })
        result = self.sites.update_one(
            {"domain": domain},
            {
                "$unset": unset_fields,
                "$set": {"one_click_current_step": "", "one_click_last_update": ts, "updated_at": ts},
            }
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count > 0

    def batch_reset_one_click_progress(self, site_ids: list[str]) -> int:
        """批量重置一键建站进度"""
        from bson import ObjectId
        object_ids = []
        for sid in site_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                continue

        ts = datetime.utcnow().isoformat()
        unset_fields = {
            f"one_click_{s}": ""
            for s in ("configure_sites", "upload_main", "set_main_category",
                      "upload_extra", "ai_configure_menu")
        }
        unset_fields.update({
            f"one_click_{s}_detail": ""
            for s in ("configure_sites", "upload_main", "set_main_category",
                      "upload_extra", "ai_configure_menu")
        })
        result = self.sites.update_many(
            {"_id": {"$in": object_ids}},
            {
                "$unset": unset_fields,
                "$set": {"one_click_current_step": "", "one_click_last_update": ts, "updated_at": ts},
            }
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count

    def update_image_status(self, domain: str, has_banner: bool, has_icon: bool, has_logo: bool) -> bool:
        """更新站点图片生成状态"""
        ts = datetime.utcnow().isoformat()
        updates = {
            "has_banner": has_banner,
            "has_icon": has_icon,
            "has_logo": has_logo,
            "image_status_time": ts,
            "updated_at": ts,
        }
        result = self.sites.update_one(
            {"domain": domain},
            {"$set": updates}
        )
        if result.modified_count > 0:
            self._stats_cache.invalidate()
        return result.modified_count > 0

    def update_domain_status(self, domain: str, report_id: str, domain_status: str) -> bool:
        """更新域名状态（从上报API同步）"""
        ts = datetime.utcnow().isoformat()
        updates = {
            "report_id": report_id,
            "domain_status": domain_status,
            "domain_status_time": ts,
            "updated_at": ts,
        }
        result = self.sites.update_one(
            {"domain": domain, "report_status": "已报"},
            {"$set": updates}
        )
        return result.modified_count > 0

    def list_reported_domains_for_sync(self) -> list[dict]:
        """列出所有已报域名用于同步状态"""
        query = {"report_status": "已报"}
        return list(self.sites.find(query, {"domain": 1, "report_id": 1, "domain_status": 1}))

    def list_domains_with_empty_status_today(self) -> list[dict]:
        """列出当天上报且域名状态为空的域名（用于自动更新状态）"""
        from datetime import timedelta
        now = datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        query = {
            "report_status": "已报",
            "report_time": {"$gte": day_start.isoformat(), "$lt": day_end.isoformat()},
            "$or": [
                {"domain_status": {"$in": ["", None]}},
                {"domain_status": {"$exists": False}},
            ],
        }
        return list(self.sites.find(query, {"domain": 1, "report_id": 1, "domain_status": 1, "_id": 0}))

    def check_all_today_reported_resolved(self) -> dict:
        """检查当天上报的域名是否全部已解析（status 为 2 或 3）。
        返回 {total, resolved, has_empty, all_resolved, ready_to_build}"""
        from datetime import timedelta
        now = datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        query = {
            "report_status": "已报",
            "report_time": {"$gte": day_start.isoformat(), "$lt": day_end.isoformat()},
        }
        docs = list(self.sites.find(query, {"domain_status": 1, "_id": 0}))
        total = len(docs)
        if total == 0:
            return {"total": 0, "resolved": 0, "has_empty": False,
                    "all_resolved": False, "ready_to_build": False}
        resolved = 0
        has_empty = False
        for d in docs:
            s = d.get("domain_status")
            if s in (None, ""):
                has_empty = True
            elif str(s) in ("2", "3"):
                resolved += 1
        all_resolved = (resolved == total) and not has_empty
        return {"total": total, "resolved": resolved, "has_empty": has_empty,
                "all_resolved": all_resolved, "ready_to_build": all_resolved}

    def batch_update_login_path(self, site_ids: list[str], login_path: str) -> int:
        """批量更新登录路径"""
        return self._batch_update_field(site_ids, "login_path", login_path)

    def get_built_stats(self, use_cache: bool = True) -> dict:
        """获取已建站统计信息（单次聚合查询 + 缓存）"""
        cache_key = "built_stats"

        if use_cache:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                return cached

        pipeline = [
            {"$match": {"build_status": "已建站"}},
            {"$facet": {
                "total": [{"$count": "count"}],
                "health": [
                    {"$group": {"_id": "$health_status", "count": {"$sum": 1}}}
                ],
                "main_data": [
                    {"$group": {"_id": "$main_data_status", "count": {"$sum": 1}}}
                ],
                "extra_data": [
                    {"$group": {"_id": "$extra_data_status", "count": {"$sum": 1}}}
                ],
                "main_category": [
                    {"$group": {"_id": "$main_category_status", "count": {"$sum": 1}}}
                ],
                "plugin": [
                    {"$group": {"_id": "$plugin_status", "count": {"$sum": 1}}}
                ],
                "media": [
                    {"$group": {"_id": "$media_status", "count": {"$sum": 1}}}
                ],
                "auto_category": [
                    {"$group": {"_id": "$auto_category_status", "count": {"$sum": 1}}}
                ],
            }}
        ]

        result = list(self.sites.aggregate(pipeline))
        if not result:
            stats = {"built_sites": 0, "health_ok": 0, "main_data_ok": 0,
                     "extra_data_ok": 0, "main_category_ok": 0, "plugin_ok": 0,
                     "media_ok": 0, "auto_category_ok": 0}
        else:
            facet = result[0]
            built = facet["total"][0]["count"] if facet.get("total") else 0

            def _count_by_value(facet_key, target_value):
                for doc in facet.get(facet_key, []):
                    if doc["_id"] == target_value:
                        return doc["count"]
                return 0

            stats = {
                "built_sites": built,
                "health_ok": _count_by_value("health", "正常"),
                "main_data_ok": _count_by_value("main_data", "已上传"),
                "extra_data_ok": _count_by_value("extra_data", "已上传"),
                "main_category_ok": _count_by_value("main_category", "已上传"),
                "plugin_ok": _count_by_value("plugin", "已配置"),
                "media_ok": _count_by_value("media", "已配置"),
                "auto_category_ok": _count_by_value("auto_category", "已配置"),
            }

        if use_cache:
            self._stats_cache.set(cache_key, stats)

        return stats
