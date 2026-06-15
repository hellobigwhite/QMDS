"""MongoDB 初始化脚本"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.db import MongoDBClient
from qmds.config import settings
from qmds.config.categories import SHOPIFY_CATEGORIES
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("init_db")


def main():
    setup_logger()

    db = MongoDBClient()
    if db.ping():
        log.info("MongoDB 连接成功")

        # 为每个类目创建 unfiltered / filtered 集合
        database = db.db
        existing = database.list_collection_names()
        for cat in SHOPIFY_CATEGORIES:
            for suffix in ("_unfiltered", "_filtered"):
                coll_name = f"{cat}{suffix}"
                if coll_name not in existing:
                    database.create_collection(coll_name)
                    log.info(f"创建集合: {settings.mongo_db_url}.{coll_name}")
                else:
                    log.info(f"集合已存在: {settings.mongo_db_url}.{coll_name}")

        db.close()
    else:
        log.error("MongoDB 连接失败，请确保 MongoDB 已启动")
        sys.exit(1)


if __name__ == "__main__":
    main()
