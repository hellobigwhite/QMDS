"""请求级数据库连接管理 — Flask g 对象复用，请求结束自动关闭"""

from flask import g

from qmds.utils.logger import get_logger

log = get_logger("db_helpers")


def get_site_db():
    if "site_db" not in g:
        from qmds.db.site_db import SiteDBClient
        g.site_db = SiteDBClient()
    return g.site_db


def get_product_db():
    if "product_db" not in g:
        from qmds.db.product_db import ProductDBClient
        g.product_db = ProductDBClient()
    return g.product_db


def get_mongo_db():
    if "mongo_db" not in g:
        from qmds.db.mongodb import MongoDBClient
        g.mongo_db = MongoDBClient()
    return g.mongo_db


def get_order_db():
    try:
        from qmds.db.order_db import OrderDBClient
        return OrderDBClient()
    except Exception as e:
        log.error(f"订单数据库连接失败: {e}")
        return None


def close_db_connections(exception):
    """Flask teardown: 请求结束时关闭所有数据库连接"""
    for key in ("site_db", "product_db", "mongo_db"):
        client = g.pop(key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
