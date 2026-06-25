import os
import threading
from datetime import datetime
from typing import Optional, List

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("order_db")


class OrderDBClient:
    """订单数据库客户端 - 使用 MongoDB 存储服务器和订单数据"""

    def __init__(self, uri: str = None, db_name: str = "server_order"):
        self._uri = uri or settings.mongo_uri
        self._db_name = db_name
        self._client: Optional[MongoClient] = None
        self._lock = threading.Lock()

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        return self._client

    @property
    def db(self):
        return self.client[self._db_name]

    @property
    def servers_col(self) -> Collection:
        return self.db["servers"]

    def _ip_to_col_name(self, ip: str) -> str:
        """将 IP 转换为集合名称，如 192.168.1.1 -> orders_192_168_1_1"""
        return f"orders_{ip.replace('.', '_')}" if ip else ""

    def get_orders_col(self, ip: str) -> Collection:
        """获取指定 IP 的订单集合"""
        col_name = self._ip_to_col_name(ip)
        if not col_name:
            raise ValueError("IP 不能为空")
        return self.db[col_name]

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def init_db(self):
        """初始化数据库和索引"""
        self.servers_col.create_index([("domain", ASCENDING)], unique=True, name="idx_domain")
        self.servers_col.create_index([("ip", ASCENDING)], name="idx_ip")
        log.info("订单数据库初始化完成")

    def ensure_orders_indexes(self, ip: str):
        """确保订单集合索引存在"""
        col = self.get_orders_col(ip)
        col.create_index([("domain", ASCENDING), ("order_time", ASCENDING)], unique=True, name="idx_domain_time")
        col.create_index([("order_time", ASCENDING)], name="idx_order_time")
        col.create_index([("order_status", ASCENDING)], name="idx_order_status")

    # ── 服务器 CRUD ──────────────────────────────────────

    def get_servers(self, page: int = None, limit: int = 100) -> dict:
        """获取服务器列表"""
        if page:
            total = self.servers_col.count_documents({})
            skip = (page - 1) * limit
            cursor = self.servers_col.find({}, {"_id": 0}).sort("id", 1).skip(skip).limit(limit)
            rows = list(cursor)
            return {"total": total, "page": page, "limit": limit, "data": rows}
        else:
            cursor = self.servers_col.find({}, {"_id": 0}).sort("id", 1)
            return list(cursor)

    def _next_server_id(self) -> int:
        """获取下一个服务器 ID"""
        last = self.servers_col.find_one(sort=[("id", -1)])
        return (last.get("id", 0) + 1) if last else 1

    def add_server(self, domain: str, ip: str = "", main_category: str = "", name: str = "") -> int:
        """添加服务器"""
        if not domain:
            raise ValueError("请填写域名")
        if not name:
            name = domain.replace("www.", "").split(".")[0].strip()

        server_id = self._next_server_id()
        ts = datetime.utcnow().isoformat()
        self.servers_col.insert_one({
            "id": server_id,
            "name": name,
            "domain": domain,
            "ip": ip,
            "main_category": main_category,
            "created_at": ts,
        })

        if ip:
            self.ensure_orders_indexes(ip)

        return server_id

    def update_server(self, server_id: int, data: dict) -> bool:
        """更新服务器"""
        update_fields = {}
        for k in ["name", "domain", "ip", "main_category"]:
            if k in data:
                update_fields[k] = data[k].strip()
        if not update_fields:
            return False
        result = self.servers_col.update_one({"id": server_id}, {"$set": update_fields})
        return result.modified_count > 0

    def delete_server(self, server_id: int) -> bool:
        """删除服务器"""
        result = self.servers_col.delete_one({"id": server_id})
        return result.deleted_count > 0

    def get_all_ips(self) -> List[str]:
        """获取所有 IP 列表"""
        ips = self.servers_col.distinct("ip", {"ip": {"$ne": ""}})
        return sorted(ips)

    # ── 订单操作 ──────────────────────────────────────

    def insert_order(self, ip: str, domain: str, order_time: str, order_status: str, order_amount: float, order_category: str = "") -> bool:
        """插入订单数据"""
        if not ip:
            return False
        col = self.get_orders_col(ip)
        ts = datetime.utcnow().isoformat()
        try:
            col.update_one(
                {"domain": domain, "order_time": order_time},
                {"$set": {
                    "domain": domain,
                    "order_time": order_time,
                    "order_status": order_status,
                    "order_amount": order_amount,
                    "order_category": order_category,
                    "updated_at": ts,
                }, "$setOnInsert": {
                    "created_at": ts,
                }},
                upsert=True,
            )
            return True
        except Exception as e:
            log.error(f"插入订单失败: {e}")
            return False

    def _build_time_filter(self, date_from: str = "", date_to: str = "", year: int = None, month: int = None) -> dict:
        """构建时间过滤条件"""
        if date_from and date_to:
            return {
                "order_time": {
                    "$gte": f"{date_from}T00:00:00",
                    "$lte": f"{date_to}T23:59:59",
                }
            }
        elif year and month:
            start = f"{year}-{month:02d}-01T00:00:00"
            if month == 12:
                end = f"{year + 1}-01-01T00:00:00"
            else:
                end = f"{year}-{month + 1:02d}-01T00:00:00"
            return {"order_time": {"$gte": start, "$lt": end}}
        return {}

    def get_orders(self, ip: str = "", page: int = 1, limit: int = 30,
                   year: int = None, month: int = None, date_from: str = "", date_to: str = "") -> dict:
        """获取订单列表"""
        time_filter = self._build_time_filter(date_from, date_to, year, month)

        if ip:
            col = self.get_orders_col(ip)
            total = col.count_documents(time_filter)
            skip = (page - 1) * limit
            cursor = col.find(time_filter, {"_id": 0}).sort("order_time", -1).skip(skip).limit(limit)
            rows = list(cursor)
            return {"total": total, "page": page, "limit": limit, "data": rows, "ip": ip}
        else:
            all_ips = self.get_all_ips()
            all_rows = []
            for pip in all_ips:
                col = self.get_orders_col(pip)
                cursor = col.find(time_filter, {"_id": 0}).sort("order_time", -1)
                all_rows.extend(cursor)
            all_rows.sort(key=lambda r: r.get("order_time") or "", reverse=True)
            total = len(all_rows)
            skip = (page - 1) * limit
            return {"total": total, "page": page, "limit": limit, "data": all_rows[skip:skip + limit], "ip": ""}

    def get_order_stats(self, ip: str = "", year: int = None, month: int = None,
                        date_from: str = "", date_to: str = "") -> list:
        """获取订单每日统计"""
        time_filter = self._build_time_filter(date_from, date_to, year, month)

        def aggregate_daily(col):
            pipeline = [
                {"$match": time_filter} if time_filter else {"$match": {}},
                {
                    "$group": {
                        "_id": {
                            "$dateToString": {"format": "%Y-%m-%d", "date": {"$toDate": "$order_time"}}
                        },
                        "cnt": {"$sum": 1},
                        "revenue": {"$sum": "$order_amount"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            return list(col.aggregate(pipeline))

        if ip:
            col = self.get_orders_col(ip)
            results = aggregate_daily(col)
            return [{"d": r["_id"], "cnt": r["cnt"], "revenue": str(r["revenue"])} for r in results]
        else:
            all_ips = self.get_all_ips()
            agg = {}
            for pip in all_ips:
                col = self.get_orders_col(pip)
                for r in aggregate_daily(col):
                    key = r["_id"]
                    if key not in agg:
                        agg[key] = {"cnt": 0, "revenue": 0}
                    agg[key]["cnt"] += r["cnt"]
                    agg[key]["revenue"] += float(r["revenue"] or 0)
            return [{"d": k, "cnt": v["cnt"], "revenue": str(v["revenue"])} for k, v in sorted(agg.items())]

    def get_order_status_stats(self, ip: str = "", year: int = None, month: int = None,
                               date_from: str = "", date_to: str = "") -> list:
        """获取订单状态统计"""
        time_filter = self._build_time_filter(date_from, date_to, year, month)

        def aggregate_status(col):
            pipeline = [
                {"$match": time_filter} if time_filter else {"$match": {}},
                {
                    "$group": {
                        "_id": {
                            "order_category": "$order_category",
                            "order_status": "$order_status",
                        },
                        "cnt": {"$sum": 1},
                        "revenue": {"$sum": "$order_amount"},
                    }
                },
                {"$sort": {"_id.order_category": 1, "cnt": -1}},
            ]
            return list(col.aggregate(pipeline))

        if ip:
            col = self.get_orders_col(ip)
            results = aggregate_status(col)
            return [
                {
                    "order_category": r["_id"]["order_category"],
                    "order_status": r["_id"]["order_status"],
                    "cnt": r["cnt"],
                    "revenue": str(r["revenue"]),
                }
                for r in results
            ]
        else:
            all_ips = self.get_all_ips()
            agg = {}
            for pip in all_ips:
                col = self.get_orders_col(pip)
                for r in aggregate_status(col):
                    key = (r["_id"]["order_category"], r["_id"]["order_status"])
                    if key not in agg:
                        agg[key] = {"cnt": 0, "revenue": 0}
                    agg[key]["cnt"] += r["cnt"]
                    agg[key]["revenue"] += float(r["revenue"] or 0)
            return [
                {"order_category": k[0], "order_status": k[1], "cnt": v["cnt"], "revenue": str(v["revenue"])}
                for k, v in sorted(agg.items())
            ]

    # ── 从上报平台同步服务器数据 ──────────────────────────────────────

    # 类目ID到名称的映射
    CATEGORY_MAP = {
        "1": "五金",
        "2": "交通工具",
        "3": "体育用品",
        "4": "保健",
        "5": "办公用品",
        "6": "动物",
        "7": "商业",
        "8": "婴幼儿用品",
        "9": "媒体",
        "10": "宗教",
        "11": "家具",
        "12": "家居与园艺",
        "13": "成人",
        "14": "服饰与配饰",
        "15": "玩具",
        "16": "电子产品",
        "17": "相机与光学器件",
        "18": "箱包",
        "19": "艺术与娱乐",
        "20": "软件",
        "21": "饮食",
    }

    def sync_from_reporter(self, domains_data: list, categories: dict = None) -> dict:
        """从上报平台同步服务器数据
        
        Args:
            domains_data: 上报平台返回的域名列表
            categories: 类目映射 {id: name}
        
        Returns:
            {"added": 新增数, "updated": 更新数, "total": 总数}
        """
        added = 0
        updated = 0
        ts = datetime.utcnow().isoformat()
        categories = categories or {}

        for item in domains_data:
            # 域名字段：尝试多种可能的字段名
            domain = str(
                item.get("name") or 
                item.get("domain") or 
                item.get("domainName") or 
                item.get("url") or 
                item.get("域名") or
                ""
            ).strip().lower()
            if not domain:
                continue
            # 移除协议前缀
            domain = domain.replace("https://", "").replace("http://", "").strip("/")
            
            # IP 字段：尝试多种可能的字段名
            ip = str(
                item.get("serverip") or
                item.get("serverIp") or
                item.get("server_ip") or 
                item.get("ip") or 
                item.get("host") or 
                item.get("服务器IP") or
                item.get("服务器ip") or
                ""
            ).strip()
            
            # 主类目字段：获取类目ID，然后通过映射获取名称
            category_id = str(
                item.get("category") or 
                item.get("main_category") or 
                item.get("mainCategory") or 
                item.get("mainCate") or 
                item.get("cate") or 
                item.get("主类目") or
                ""
            ).strip()
            # 通过类目映射获取名称，优先使用传入的映射，其次使用内置映射
            cat_map = categories or self.CATEGORY_MAP
            main_category = cat_map.get(category_id, category_id) if category_id else ""
            
            name = domain.replace("www.", "").split(".")[0].strip()

            existing = self.servers_col.find_one({"domain": domain})
            if existing:
                update_fields = {"updated_at": ts}
                if ip and ip != existing.get("ip"):
                    update_fields["ip"] = ip
                if main_category and main_category != existing.get("main_category"):
                    update_fields["main_category"] = main_category
                if len(update_fields) > 1:
                    self.servers_col.update_one({"domain": domain}, {"$set": update_fields})
                    updated += 1
            else:
                server_id = self._next_server_id()
                self.servers_col.insert_one({
                    "id": server_id,
                    "name": name,
                    "domain": domain,
                    "ip": ip,
                    "main_category": main_category,
                    "created_at": ts,
                })
                added += 1
                if ip:
                    self.ensure_orders_indexes(ip)

        return {"added": added, "updated": updated, "total": len(domains_data)}
