"""MongoDB 初始化脚本"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.db import MongoDBClient
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("init_db")


def main():
    setup_logger()

    db = MongoDBClient()
    if db.ping():
        log.info("MongoDB 连接成功")

        collections = {
            "shopify_url": ["camera_Unfiltered_URLs", "camera_Filtered_URLs"],
            "shopify_data_new": ["camera", "beauty", "sports", "electronics"],
        }

        for db_name, colls in collections.items():
            database = db.get_db(db_name)
            existing = database.list_collection_names()
            for coll in colls:
                if coll not in existing:
                    database.create_collection(coll)
                    log.info(f"创建集合: {db_name}.{coll}")
                else:
                    log.info(f"集合已存在: {db_name}.{coll}")

        db.close()
    else:
        log.error("MongoDB 连接失败，请确保 MongoDB 已启动")
        sys.exit(1)


if __name__ == "__main__":
    main()
