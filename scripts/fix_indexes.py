"""修复索引脚本（单一集合模式）

扫描 qmds_url_stores 的所有 {category}__{subcategory} 集合，确保 filter_status/crawl_status 索引存在。
同时扫描 {category} 集合（unfiltered），确保 filter_status 索引存在。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.db import MongoDBClient
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("fix_indexes")


def main():
    setup_logger()

    db = MongoDBClient()
    if not db.ping():
        log.error("MongoDB 连接失败")
        sys.exit(1)

    database = db.db

    # 扫描所有集合
    for coll_name in sorted(database.list_collection_names()):
        if coll_name.startswith("system."):
            continue

        coll = database[coll_name]

        # 跳过旧格式集合（带 _filtered/_crawled/_unfiltered 后缀）
        if coll_name.endswith("_filtered") or coll_name.endswith("_crawled") or coll_name.endswith("_unfiltered"):
            log.info(f"跳过旧格式集合: {coll_name}")
            continue

        existing_indexes = {idx["name"]: idx for idx in coll.list_indexes()}

        # 删除旧的单字段 idx_domain 索引（filtered 集合的历史遗留）
        if "idx_domain" in existing_indexes:
            log.info(f"删除旧索引: {coll_name}.idx_domain")
            coll.drop_index("idx_domain")

        # 单一集合（含 __ 分隔符）：filtered/crawled 合一集合
        if "__" in coll_name:
            if "idx_domain_collection" not in existing_indexes:
                log.info(f"创建索引: {coll_name}.idx_domain_collection")
                coll.create_index(
                    [("domain", 1), ("collection_handle", 1)],
                    unique=True,
                    name="idx_domain_collection",
                )
            if "idx_filter_status" not in existing_indexes:
                log.info(f"创建索引: {coll_name}.idx_filter_status")
                coll.create_index([("filter_status", 1)], name="idx_filter_status")
            if "idx_crawl_status" not in existing_indexes:
                log.info(f"创建索引: {coll_name}.idx_crawl_status")
                coll.create_index([("crawl_status", 1)], name="idx_crawl_status")
        else:
            # unfiltered 集合（{category}，一级分类维度）
            if "idx_filter_status" not in existing_indexes:
                log.info(f"创建索引: {coll_name}.idx_filter_status")
                coll.create_index([("filter_status", 1)], name="idx_filter_status")

    log.info("索引修复完成")
    db.close()


if __name__ == "__main__":
    main()
