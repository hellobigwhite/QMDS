from datetime import datetime
from typing import Optional

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("site_db")


class SiteDBClient:
    """站点管理数据库客户端 - 使用MongoDB存储站点数据"""

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
        log.info(f"添加/更新站点: {site_data.get('domain', '')}")
        return str(result.upserted_id or site_data.get("domain", ""))

    def update_site(self, domain: str, updates: dict) -> bool:
        """更新站点信息"""
        updates["updated_at"] = datetime.utcnow().isoformat()
        result = self.sites.update_one(
            {"domain": domain},
            {"$set": updates}
        )
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
            return result.modified_count > 0
        except Exception:
            return False

    def delete_site(self, domain: str) -> bool:
        """删除站点"""
        result = self.sites.delete_one({"domain": domain})
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

    def list_all_sites(self, keyword: str = "") -> list[dict]:
        """列出所有站点"""
        query = {}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        return list(self.sites.find(query).sort("created_at", -1))

    def list_local_sites(self, keyword: str = "") -> list[dict]:
        """列出本地站点（未上报的站点）"""
        query = {"report_status": {"$ne": "已报"}}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        return list(self.sites.find(query).sort("created_at", -1))

    def list_reported_sites(self, keyword: str = "") -> list[dict]:
        """列出已报域名"""
        query = {"report_status": "已报"}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        return list(self.sites.find(query).sort("report_time", -1))

    def list_scheduled_sites(self, keyword: str = "") -> list[dict]:
        """列出计划上报的站点"""
        query = {"schedule_enabled": "1", "report_status": {"$ne": "已报"}}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        return list(self.sites.find(query).sort("schedule_time", 1))

    def list_built_sites(self, keyword: str = "") -> list[dict]:
        """列出已建站的站点"""
        query = {"build_status": "已建站"}
        if keyword:
            query["domain"] = {"$regex": keyword, "$options": "i"}
        return list(self.sites.find(query).sort("build_time", -1))

    # ── 统计操作 ──────────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取站点统计信息"""
        total = self.sites.estimated_document_count()
        reported = self.sites.count_documents({"report_status": "已报"})
        built = self.sites.count_documents({"build_status": "已建站"})
        scheduled = self.sites.count_documents({"schedule_enabled": "1", "report_status": {"$ne": "已报"}})
        local = total - reported

        return {
            "total_sites": total,
            "local_sites": local,
            "reported_sites": reported,
            "scheduled_sites": scheduled,
            "built_sites": built,
        }

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
        return result.modified_count

    # ── 导入导出 ──────────────────────────────────────────────

    def import_from_excel(self, filepath: str) -> dict:
        """从Excel文件导入站点数据"""
        import pandas as pd
        df = pd.read_excel(filepath)
        created = 0
        updated = 0

        for _, row in df.iterrows():
            domain = str(row.get("域名", "") or row.get("domain", "")).strip()
            if not domain:
                continue

            site_data = {
                "domain": domain,
                "template": str(row.get("底板", "") or row.get("模板", "") or row.get("template", "")),
                "server": str(row.get("服务器", "") or row.get("server", "")),
                "category": str(row.get("大类", "") or row.get("category", "")),
                "main_category": str(row.get("主分类", "") or row.get("main_category", "")),
                "main_data_source_id": str(row.get("主分类数据码", "") or row.get("main_data_source_id", "")),
                "extra_data_source_id": str(row.get("站群数据码", "") or row.get("extra_data_source_id", "")),
                "title": str(row.get("SEO Title", "") or row.get("title", "")),
                "description": str(row.get("Meta Description", "") or row.get("description", "")),
                "address": str(row.get("地址", "") or row.get("address", "")),
            }

            existing = self.get_site(domain)
            if existing:
                self.update_site(domain, site_data)
                updated += 1
            else:
                self.add_site(site_data)
                created += 1

        log.info(f"Excel导入完成: 新增 {created}, 更新 {updated}")
        return {"created": created, "updated": updated}

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
        return result.modified_count

    def get_site_count(self) -> int:
        """获取站点总数"""
        return self.sites.estimated_document_count()

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
            "erp_username": "linwei",
            "erp_password": "linwei123",
            "wp_password": "",
            "media_root": "D:\\logo",
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

    def batch_update_health_status(self, site_ids: list[str], status: str) -> int:
        """批量更新健康检查状态"""
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
            {"$set": {"health_status": status, "health_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_main_data_status(self, site_ids: list[str], status: str) -> int:
        """批量更新主数据上传状态"""
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
            {"$set": {"main_data_status": status, "main_data_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_extra_data_status(self, site_ids: list[str], status: str) -> int:
        """批量更新补充数据上传状态"""
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
            {"$set": {"extra_data_status": status, "extra_data_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_main_category_status(self, site_ids: list[str], status: str) -> int:
        """批量更新主分类设置状态"""
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
            {"$set": {"main_category_status": status, "main_category_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_plugin_status(self, site_ids: list[str], status: str) -> int:
        """批量更新插件配置状态"""
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
            {"$set": {"plugin_status": status, "plugin_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_media_status(self, site_ids: list[str], status: str) -> int:
        """批量更新媒体配置状态"""
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
            {"$set": {"media_status": status, "media_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_auto_category_status(self, site_ids: list[str], status: str) -> int:
        """批量更新菜单/自动分类状态"""
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
            {"$set": {"auto_category_status": status, "auto_category_time": ts, "updated_at": ts}}
        )
        return result.modified_count

    def batch_update_login_path(self, site_ids: list[str], login_path: str) -> int:
        """批量更新登录路径"""
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
            {"$set": {"login_path": login_path, "updated_at": ts}}
        )
        return result.modified_count

    def get_built_stats(self) -> dict:
        """获取已建站统计信息"""
        built = self.sites.count_documents({"build_status": "已建站"})
        health_ok = self.sites.count_documents({"build_status": "已建站", "health_status": "正常"})
        main_data_ok = self.sites.count_documents({"build_status": "已建站", "main_data_status": "已上传"})
        extra_data_ok = self.sites.count_documents({"build_status": "已建站", "extra_data_status": "已上传"})
        main_category_ok = self.sites.count_documents({"build_status": "已建站", "main_category_status": "已上传"})
        plugin_ok = self.sites.count_documents({"build_status": "已建站", "plugin_status": "已配置"})
        media_ok = self.sites.count_documents({"build_status": "已建站", "media_status": "已配置"})
        auto_category_ok = self.sites.count_documents({"build_status": "已建站", "auto_category_status": "已配置"})

        return {
            "built_sites": built,
            "health_ok": health_ok,
            "main_data_ok": main_data_ok,
            "extra_data_ok": extra_data_ok,
            "main_category_ok": main_category_ok,
            "plugin_ok": plugin_ok,
            "media_ok": media_ok,
            "auto_category_ok": auto_category_ok,
        }
