"""将所有集合中 crawl_status=crawl_failed 的文档重置为 uncrawled。

用法:
    python scripts/reset_crawl_status.py
"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.db.mongodb import (
    MongoDBClient,
    CRAWL_STATUS_UNCRAWLED,
    CRAWL_STATUS_FAILED,
)


def main():
    print("开始重置 crawl_status: crawl_failed -> uncrawled ...")
    db = MongoDBClient()
    total_reset = 0

    try:
        # 1. 遍历所有 {category}__{subcategory} 集合
        for prefix in db.list_filtered_categories():
            col = db.db[prefix]
            result = col.update_many(
                {"crawl_status": CRAWL_STATUS_FAILED},
                {"$set": {"crawl_status": CRAWL_STATUS_UNCRAWLED}},
            )
            if result.modified_count:
                print(f"  {prefix}: 重置 {result.modified_count} 条")
                total_reset += result.modified_count

        # 2. 处理 comprehensive_stores 集合
        col = db.comprehensive_col()
        result = col.update_many(
            {"crawl_status": CRAWL_STATUS_FAILED},
            {"$set": {"crawl_status": CRAWL_STATUS_UNCRAWLED}},
        )
        if result.modified_count:
            print(f"  comprehensive_stores: 重置 {result.modified_count} 条")
            total_reset += result.modified_count

        print(f"\n共重置 {total_reset} 条文档")
    finally:
        db.close()


if __name__ == "__main__":
    main()
