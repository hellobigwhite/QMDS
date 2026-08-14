"""[已废弃] 将 {category}_clean 中已导出的数据移至 {category}_export 集合

此脚本用于旧版本（三后缀集合模式）的迁移。
当前版本已采用单一集合模式（clean_status/export_status 字段区分状态），此脚本保留仅供历史参考。

迁移条件（旧）: clean 集合中 export_count > 0 的文档
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pymongo import MongoClient
from qmds.config import settings
from qmds.utils.logger import setup_logger, get_logger
from qmds.db.product_db import PRODUCT_DB_NAME, CLEAN_SUFFIX, EXPORT_SUFFIX

log = get_logger("migrate_exported")


def collect_tasks(db) -> list:
    """扫描所有 _clean 集合，找出含有已导出数据的类目"""
    categories = set()
    for name in db.list_collection_names():
        if name.endswith(CLEAN_SUFFIX):
            categories.add(name[: -len(CLEAN_SUFFIX)])

    tasks = []
    for cat in sorted(categories):
        clean_col = db[f"{cat}{CLEAN_SUFFIX}"]
        count = clean_col.count_documents({"export_count": {"$gt": 0}})
        if count > 0:
            tasks.append((cat, count))
    return tasks


def migrate(dry_run: bool = True):
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[PRODUCT_DB_NAME]

    tasks = collect_tasks(db)

    if not tasks:
        log.info("没有需要迁移的已导出数据")
        return

    total_docs = sum(c for _, c in tasks)
    log.info(
        f"{'[DRY-RUN] ' if dry_run else ''}共 {len(tasks)} 个类目，{total_docs} 条文档需要迁移:"
    )
    for cat, count in tasks:
        log.info(f"  {cat}: {count} 条 (→ {cat}{EXPORT_SUFFIX})")

    if dry_run:
        log.info("这是 dry-run 模式，未做任何修改。加 --execute 参数执行实际迁移。")
        return

    for cat, _ in tasks:
        clean_col = db[f"{cat}{CLEAN_SUFFIX}"]
        export_col = db[f"{cat}{EXPORT_SUFFIX}"]

        # 建索引（幂等）
        export_col.create_index([("标题", 1)], name="idx_title")
        export_col.create_index([("export_time", 1)], name="idx_export_time")

        # 分批次查询已导出的文档
        cursor = clean_col.find({"export_count": {"$gt": 0}}, no_cursor_timeout=True)

        batch = []
        id_batch = []
        moved = 0

        for doc in cursor:
            doc_id = doc["_id"]
            doc.pop("_id", None)
            doc["export_time"] = doc.get("last_export_time") or datetime.utcnow().isoformat()
            batch.append(doc)
            id_batch.append(doc_id)

            if len(batch) >= 1000:
                export_col.insert_many(batch, ordered=False)
                clean_col.delete_many({"_id": {"$in": id_batch}})
                moved += len(batch)
                log.info(f"  {cat}: 已迁移 {moved} 条...")
                batch = []
                id_batch = []

        # 处理剩余批次
        if batch:
            export_col.insert_many(batch, ordered=False)
            clean_col.delete_many({"_id": {"$in": id_batch}})
            moved += len(batch)

        cursor.close()
        log.info(f"  {cat}: 迁移完成，共 {moved} 条")

    log.info("迁移完成")


def main():
    parser = argparse.ArgumentParser(description="迁移已导出数据到 _export 集合")
    parser.add_argument("--execute", action="store_true", help="实际执行迁移（默认 dry-run）")
    args = parser.parse_args()

    setup_logger()
    migrate(dry_run=not args.execute)


if __name__ == "__main__":
    main()
