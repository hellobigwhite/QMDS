"""一次性脚本：为 qmds_product_data 下所有现有产品集合补建复合索引
(clean_status, export_status)，消除导出查询的全索引扫描慢查询。

用法: python scripts/add_status_compound_index.py [--dry-run]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pymongo import ASCENDING

from qmds.db.product_db import ProductDBClient

INDEX_NAME = "idx_clean_export_status"


def main():
    dry_run = "--dry-run" in sys.argv
    client = ProductDBClient()
    if not client.ping():
        print("ERROR: MongoDB 不可达")
        sys.exit(1)

    db = client.db
    names = [n for n in db.list_collection_names()
             if not n.startswith("system.") and not n.startswith("_")]
    print(f"共 {len(names)} 个产品集合")
    created, skipped = 0, 0
    for name in sorted(names):
        col = db[name]
        existing = {ix.get("name") for ix in col.list_indexes()}
        if INDEX_NAME in existing:
            skipped += 1
            continue
        if dry_run:
            print(f"[dry-run] 将创建: {name}.{INDEX_NAME}")
            created += 1
            continue
        col.create_index(
            [("clean_status", ASCENDING), ("export_status", ASCENDING)],
            name=INDEX_NAME,
            background=True,
        )
        print(f"已创建: {name}.{INDEX_NAME}")
        created += 1

    print(f"完成: 新建 {created}, 已存在跳过 {skipped}")
    client.close()


if __name__ == "__main__":
    main()
