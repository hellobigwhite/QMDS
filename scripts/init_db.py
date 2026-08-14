"""初始化 MongoDB 数据库和索引

注意: 单一集合模式下：
- qmds_url_stores: {category} 集合（unfiltered），{category}__{subcategory} 集合（filtered/crawled 合一）
- qmds_product_data: {category}__{subcategory} 单一集合（clean_status/export_status 字段区分状态）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.db import MongoDBClient
from qmds.db.product_db import ProductDBClient
from qmds.config.categories import SHOPIFY_CATEGORIES
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("init_db")


def main():
    setup_logger()

    # 初始化 qmds_url_stores
    db = MongoDBClient()
    if not db.ping():
        log.error("MongoDB 连接失败")
        sys.exit(1)

    database = db.db
    # 为每个一级分类创建 unfiltered 集合（{category}）
    for cat in SHOPIFY_CATEGORIES:
        if cat not in database.list_collection_names():
            database.create_collection(cat)
            log.info(f"创建集合: {cat}")
        db.ensure_indexes(cat)

    log.info(f"qmds_url_stores 初始化完成（{len(SHOPIFY_CATEGORIES)} 个一级分类）")
    db.close()

    # 初始化 qmds_product_data（无需预创建集合，爬取时自动创建并建索引）
    product_db = ProductDBClient()
    if not product_db.ping():
        log.error("qmds_product_data 连接失败")
        sys.exit(1)
    log.info("qmds_product_data 初始化完成（集合在首次写入时自动创建）")
    product_db.close()


if __name__ == "__main__":
    main()
