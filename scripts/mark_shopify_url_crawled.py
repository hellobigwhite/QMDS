"""将 qmds_url_stores 库中所有 source=shopify_url 的文档 crawl_status 改为 crawled。

仅处理含 crawl_status 字段的集合：
  - {category}__{subcategory} 集合（filtered/crawled 单一集合）
  - comprehensive_stores 集合

用法:
    python scripts/mark_shopify_url_crawled.py
"""
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.config import settings
from qmds.db.mongodb import (
    MongoDBClient,
    SOURCE_SHOPIFY_URL,
    CRAWL_STATUS_CRAWLED,
    CRAWL_STATUS_UNCRAWLED,
    CRAWL_STATUS_FAILED,
)

# 仅这些集合使用 crawl_status 字段（其他如 shopify_url_info、filtered_failed 不应被修改）


def _is_target_collection(name: str) -> bool:
    """判断集合是否应被处理（含 crawl_status 字段的集合）"""
    if name == settings.comprehensive_collection:
        return True
    # {category}__{subcategory} 形式的 filtered 集合
    if "__" in name and not name.startswith("system.") and not name.startswith("_"):
        return True
    return False


def main():
    print(f"MongoDB: {settings.mongo_uri}")
    print(f"数据库: {settings.mongo_db_url}")
    print(f"目标: source={SOURCE_SHOPIFY_URL} -> crawl_status={CRAWL_STATUS_CRAWLED}")
    print("=" * 50)

    db = MongoDBClient()
    total_updated = 0

    try:
        col_names = [
            name for name in db.db.list_collection_names()
            if _is_target_collection(name)
        ]
        print(f"扫描 {len(col_names)} 个目标集合...\n")

        for col_name in sorted(col_names):
            col = db.db[col_name]

            # 统计当前 source=shopify_url 且非 crawled 的文档数
            match_q = {
                "source": SOURCE_SHOPIFY_URL,
                "crawl_status": {"$ne": CRAWL_STATUS_CRAWLED},
            }
            matched = col.count_documents(match_q)
            if matched == 0:
                continue

            # 按当前状态分类统计，用于后续重建计数器参考
            stat_uncrawled = col.count_documents({
                "source": SOURCE_SHOPIFY_URL,
                "crawl_status": CRAWL_STATUS_UNCRAWLED,
            })
            stat_failed = col.count_documents({
                "source": SOURCE_SHOPIFY_URL,
                "crawl_status": CRAWL_STATUS_FAILED,
            })

            result = col.update_many(
                match_q,
                {"$set": {"crawl_status": CRAWL_STATUS_CRAWLED,
                          "updated_at": datetime.utcnow().isoformat()}},
            )
            modified = result.modified_count
            total_updated += modified
            print(f"  {col_name}: 更新 {modified} 条 "
                  f"(uncrawled={stat_uncrawled}, crawl_failed={stat_failed})")

        print(f"\n共更新 {total_updated} 条文档")

        if total_updated > 0:
            print("\n重建 _counters 集合以同步计数...")
            r = db.rebuild_counters()
            print(f"  重建: {r['rebuilt']} 个集合, 错误: {len(r['errors'])} 个")
            for err in r["errors"]:
                print(f"    - {err}")
    finally:
        db.close()

    print("\n完成。")


if __name__ == "__main__":
    main()
