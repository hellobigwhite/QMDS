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
        # 去重索引
        col.create_index([
            ("domain", ASCENDING),
            ("customer_email", ASCENDING),
            ("order_amount", ASCENDING)
        ], name="idx_dedup")
        # 详情页查询索引
        col.create_index([("customer_email", ASCENDING)], name="idx_email", sparse=True)
        col.create_index([("items", ASCENDING)], name="idx_has_details", sparse=True)

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

    def delete_all_servers(self) -> int:
        """删除所有服务器"""
        result = self.servers_col.delete_many({})
        return result.deleted_count

    def delete_all_orders(self) -> int:
        """清空所有订单数据"""
        deleted = 0
        # 获取所有订单集合（以orders_开头的集合）
        for col_name in self.db.list_collection_names():
            if col_name.startswith("orders_"):
                result = self.db[col_name].drop()
                deleted += 1
        log.info(f"已清空 {deleted} 个订单集合")
        return deleted

    def get_all_ips(self) -> List[str]:
        """获取所有 IP 列表"""
        ips = self.servers_col.distinct("ip", {"ip": {"$ne": ""}})
        return sorted(ips)

    # ── 订单操作 ──────────────────────────────────────

    def insert_order(self, ip: str, domain: str, order_time: str, order_status: str, order_amount: float, order_category: str = "", order_view_id: str = "") -> bool:
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
                    "order_view_id": order_view_id,
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
                    "$gte": f"{date_from} 00:00:00",
                    "$lte": f"{date_to} 23:59:59",
                }
            }
        elif year and month:
            start = f"{year}-{month:02d}-01 00:00:00"
            if month == 12:
                end = f"{year + 1}-01-01 00:00:00"
            else:
                end = f"{year}-{month + 1:02d}-01 00:00:00"
            return {"order_time": {"$gte": start, "$lt": end}}
        return {}

    def get_orders(self, ip: str = "", page: int = 1, limit: int = 30,
                   year: int = None, month: int = None, date_from: str = "", date_to: str = "",
                   sort_by: str = "order_time", sort_order: int = -1) -> dict:
        """获取订单列表
        
        Args:
            sort_by: 排序字段，可选值: order_time, domain, order_amount, order_category
            sort_order: 排序方向，1=升序, -1=降序
        """
        time_filter = self._build_time_filter(date_from, date_to, year, month)
        
        # 验证排序字段
        allowed_sort_fields = {"order_time", "domain", "order_amount", "order_category"}
        if sort_by not in allowed_sort_fields:
            sort_by = "order_time"
        if sort_order not in (1, -1):
            sort_order = -1

        if ip:
            col = self.get_orders_col(ip)
            total = col.count_documents(time_filter)
            skip = (page - 1) * limit
            cursor = col.find(time_filter, {"_id": 0}).sort(sort_by, sort_order).skip(skip).limit(limit)
            rows = list(cursor)
            return {"total": total, "page": page, "limit": limit, "data": rows, "ip": ip}
        else:
            all_ips = self.get_all_ips()
            all_rows = []
            for pip in all_ips:
                col = self.get_orders_col(pip)
                cursor = col.find(time_filter, {"_id": 0})
                all_rows.extend(cursor)
            all_rows.sort(key=lambda r: r.get(sort_by) or (0 if sort_by == "order_amount" else ""), reverse=(sort_order == -1))
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

    def import_servers(self, items: list, mode: str = "skip") -> dict:
        """批量导入服务器

        Args:
            items: [{"domain": str, "ip": str, "main_category": str}, ...]
            mode: "skip" 已存在的域名跳过；"update" 已存在的域名更新 IP/主类目

        Returns:
            {"added": 新增数, "updated": 更新数, "skipped": 跳过数,
             "errors": [错误信息], "total": 输入总行数}
        """
        added = 0
        updated = 0
        skipped = 0
        errors = []
        ts = datetime.utcnow().isoformat()
        seen_domains = set()

        for i, item in enumerate(items, 1):
            domain = str(item.get("domain") or "").strip().lower()
            if not domain:
                errors.append(f"第{i}行: 缺少域名")
                continue
            # 移除协议前缀与结尾斜杠
            domain = domain.replace("https://", "").replace("http://", "").strip("/")

            ip = str(item.get("ip") or "").strip()
            main_category = str(item.get("main_category") or "").strip()

            if domain in seen_domains:
                errors.append(f"第{i}行: 域名 {domain} 在本次导入中重复")
                continue
            seen_domains.add(domain)

            existing = self.servers_col.find_one({"domain": domain})
            if existing:
                if mode == "update":
                    update_fields = {"updated_at": ts}
                    if ip and ip != existing.get("ip"):
                        update_fields["ip"] = ip
                    if main_category and main_category != existing.get("main_category"):
                        update_fields["main_category"] = main_category
                    if len(update_fields) > 1:
                        self.servers_col.update_one({"domain": domain}, {"$set": update_fields})
                        updated += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
            else:
                name = domain.replace("www.", "").split(".")[0].strip()
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

        return {"added": added, "updated": updated, "skipped": skipped,
                "errors": errors, "total": len(items)}

    # ── 订单详情操作 ──────────────────────────────────────

    def update_order_details(self, ip: str, domain: str, order_time: str,
                             customer_email: str = "", order_amount: float = 0,
                             items: list = None, billing_address: dict = None,
                             shipping_address: dict = None,
                             customer_name: str = "") -> dict:
        """更新订单详情，同时进行去重检查
        
        Args:
            ip: 服务器IP
            domain: 域名
            order_time: 订单时间
            customer_email: 客户邮箱
            order_amount: 订单金额
            items: 商品列表
            billing_address: 账单地址
            shipping_address: 收货地址
            customer_name: 客户姓名
        
        Returns:
            {"updated": bool, "deduplicated": int, "duplicate_of": str}
        """
        if not ip:
            return {"updated": False, "deduplicated": 0, "duplicate_of": ""}
        
        col = self.get_orders_col(ip)
        order_date = order_time[:10] if order_time else ""
        deduplicated = 0
        duplicate_of = ""
        
        # 去重检查（同域名、同日期、同邮箱、同金额）
        if customer_email and order_date and order_amount:
            duplicate_filter = {
                "domain": domain,
                "order_time": {"$regex": f"^{order_date}"},
                "customer_email": customer_email,
                "order_amount": order_amount,
            }
            current_doc = col.find_one({"domain": domain, "order_time": order_time}, {"_id": 1})
            current_id = current_doc["_id"] if current_doc else None
            if current_id:
                duplicate_filter["_id"] = {"$ne": current_id}
            duplicates = list(col.find(duplicate_filter, {"_id": 1, "order_time": 1}))

            if duplicates:
                # 按订单时间排序，保留最新的
                duplicates.sort(key=lambda x: x.get("order_time", ""), reverse=True)

                # 删除重复记录（保留当前订单，因为它是最新的）
                dup_ids = [dup["_id"] for dup in duplicates]
                result = col.delete_many({"_id": {"$in": dup_ids}})
                deduplicated = result.deleted_count

                if duplicates:
                    duplicate_of = duplicates[0].get("order_time", "")

                log.info(f"去重: 删除 {deduplicated} 条重复订单 (域名={domain}, 日期={order_date}, 邮箱={customer_email}, 金额={order_amount})")
        
        # 更新详情
        update_fields = {}
        if customer_email:
            update_fields["customer_email"] = customer_email
        if customer_name:
            update_fields["customer_name"] = customer_name
        if items is not None:
            update_fields["items"] = items
        if billing_address:
            update_fields["billing_address"] = billing_address
        if shipping_address:
            update_fields["shipping_address"] = shipping_address
        
        updated = False
        if update_fields:
            update_fields["updated_at"] = datetime.utcnow().isoformat()
            result = col.update_one(
                {"domain": domain, "order_time": order_time},
                {"$set": update_fields}
            )
            updated = result.modified_count > 0
        
        return {
            "updated": updated,
            "deduplicated": deduplicated,
            "duplicate_of": duplicate_of
        }

    def get_orders_without_details(self, ip: str, domain: str = None,
                                   year: int = None, month: int = None) -> list:
        """获取没有详情信息的订单
        
        Args:
            ip: 服务器IP
            domain: 域名（可选）
            year: 年份（可选）
            month: 月份（可选）
        
        Returns:
            订单列表 [{"order_id": ..., "order_time": ..., "domain": ...}, ...]
        """
        if not ip:
            return []
        
        col = self.get_orders_col(ip)
        
        # 构建查询条件
        query = {
            "$or": [
                {"items": {"$exists": False}},
                {"items": None},
                {"items": []}
            ]
        }
        
        if domain:
            query["domain"] = domain
        
        if year and month:
            start = f"{year}-{month:02d}-01 00:00:00"
            if month == 12:
                end = f"{year + 1}-01-01 00:00:00"
            else:
                end = f"{year}-{month + 1:02d}-01 00:00:00"
            query["order_time"] = {"$gte": start, "$lt": end}
        
        # 查询订单
        cursor = col.find(query, {
            "_id": 0,
            "domain": 1,
            "order_time": 1,
            "order_status": 1,
            "order_amount": 1,
            "order_view_id": 1
        }).sort("order_time", -1)
        
        return list(cursor)

    def get_order_by_domain_time(self, ip: str, domain: str, order_time: str) -> dict:
        """根据域名和订单时间获取订单"""
        if not ip:
            return None
        col = self.get_orders_col(ip)
        return col.find_one(
            {"domain": domain, "order_time": order_time},
            {"_id": 0}
        )

    def deduplicate_orders(self, ip: str, domain: str = None,
                           year: int = None, month: int = None) -> dict:
        """执行订单去重任务
        
        Args:
            ip: 服务器IP（如果为空则处理所有IP）
            domain: 域名（可选）
            year: 年份（可选）
            month: 月份（可选）
        
        Returns:
            {"duplicates_found": int, "deleted": int, "ips_processed": int}
        """
        total_duplicates = 0
        total_deleted = 0
        ips_processed = 0
        
        # 获取要处理的IP列表
        if ip:
            ips = [ip]
        else:
            ips = self.get_all_ips()
        
        for current_ip in ips:
            col = self.get_orders_col(current_ip)
            
            # 构建匹配条件
            match_stage = {
                "customer_email": {"$exists": True, "$ne": ""}
            }
            
            if domain:
                match_stage["domain"] = domain
            
            if year and month:
                start = f"{year}-{month:02d}-01 00:00:00"
                if month == 12:
                    end = f"{year + 1}-01-01 00:00:00"
                else:
                    end = f"{year}-{month + 1:02d}-01 00:00:00"
                match_stage["order_time"] = {"$gte": start, "$lt": end}
            
            # 聚合查找重复订单
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": {
                        "domain": "$domain",
                        "date": {"$substr": ["$order_time", 0, 10]},
                        "email": "$customer_email",
                        "amount": "$order_amount"
                    },
                    "count": {"$sum": 1},
                    "docs": {"$push": {
                        "_id": "$_id",
                        "order_time": "$order_time"
                    }}
                }},
                {"$match": {"count": {"$gt": 1}}}
            ]
            
            duplicates = list(col.aggregate(pipeline))
            total_duplicates += len(duplicates)
            
            for dup in duplicates:
                # 按订单时间排序，保留最新的
                docs = dup["docs"]
                docs.sort(key=lambda x: x.get("order_time", ""), reverse=True)
                to_delete = docs[1:]  # 保留第一个（最新的）
                
                if to_delete:
                    delete_ids = [doc["_id"] for doc in to_delete]
                    result = col.delete_many({"_id": {"$in": delete_ids}})
                    total_deleted += result.deleted_count
            
            ips_processed += 1
            
            if duplicates:
                log.info(f"IP {current_ip}: 发现 {len(duplicates)} 组重复，删除 {total_deleted} 条")
        
        return {
            "duplicates_found": total_duplicates,
            "deleted": total_deleted,
            "ips_processed": ips_processed
        }

    def get_dedup_stats(self, ip: str = None, domain: str = None,
                        year: int = None, month: int = None) -> dict:
        """获取去重统计信息（不执行删除）
        
        Returns:
            {"total_orders": int, "potential_duplicates": int, "groups": list}
        """
        total_orders = 0
        potential_duplicates = 0
        groups = []
        
        # 获取要处理的IP列表
        if ip:
            ips = [ip]
        else:
            ips = self.get_all_ips()
        
        for current_ip in ips:
            col = self.get_orders_col(current_ip)
            
            # 构建匹配条件
            match_stage = {
                "customer_email": {"$exists": True, "$ne": ""}
            }
            
            if domain:
                match_stage["domain"] = domain
            
            if year and month:
                start = f"{year}-{month:02d}-01 00:00:00"
                if month == 12:
                    end = f"{year + 1}-01-01 00:00:00"
                else:
                    end = f"{year}-{month + 1:02d}-01 00:00:00"
                match_stage["order_time"] = {"$gte": start, "$lt": end}
            
            # 统计总订单数
            total_orders += col.count_documents(match_stage)
            
            # 聚合查找重复组
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": {
                        "domain": "$domain",
                        "date": {"$substr": ["$order_time", 0, 10]},
                        "email": "$customer_email",
                        "amount": "$order_amount"
                    },
                    "count": {"$sum": 1},
                    "order_times": {"$push": "$order_time"}
                }},
                {"$match": {"count": {"$gt": 1}}}
            ]
            
            dup_groups = list(col.aggregate(pipeline))
            potential_duplicates += len(dup_groups)
            
            for group in dup_groups:
                groups.append({
                    "ip": current_ip,
                    "domain": group["_id"]["domain"],
                    "date": group["_id"]["date"],
                    "email": group["_id"]["email"],
                    "amount": group["_id"]["amount"],
                    "count": group["count"],
                    "order_times": sorted(group["order_times"], reverse=True)
                })
        
        return {
            "total_orders": total_orders,
            "potential_duplicates": potential_duplicates,
            "groups": groups
        }
