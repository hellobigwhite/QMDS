"""修复 filtered 集合的索引：删除旧的单字段 idx_domain，创建正确的复合索引 idx_domain_collection"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.db import MongoDBClient
from qmds.config.categories import SHOPIFY_CATEGORIES
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("fix_indexes")


def main():
    setup_logger()

    db = MongoDBClient()
    if not db.ping():
        log.error("MongoDB 连接失败")
        sys.exit(1)

    database = db.db

    for cat in SHOPIFY_CATEGORIES:
        coll_name = f"{cat}_filtered"
        coll = database[coll_name]

        # 获取现有索引
        existing_indexes = {idx["name"]: idx for idx in coll.list_indexes()}

        # 删除旧的单字段 idx_domain 索引
        if "idx_domain" in existing_indexes:
            log.info(f"删除旧索引: {coll_name}.idx_domain")
            coll.drop_index("idx_domain")

        # 创建正确的复合索引
        if "idx_domain_collection" not in existing_indexes:
            log.info(f"创建索引: {coll_name}.idx_domain_collection")
            coll.create_index(
                [("domain", 1), ("collection_handle", 1)],
                unique=True,
                name="idx_domain_collection",
            )
        else:
            log.info(f"索引已存在: {coll_name}.idx_domain_collection")

    log.info("索引修复完成")
    db.close()


if __name__ == "__main__":
    main()
