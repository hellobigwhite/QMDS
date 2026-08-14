"""全量重建 _counters 集合，修复 $inc 漂移。

用法:
    python scripts/rebuild_counters.py
"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.config import settings
from qmds.db.mongodb import MongoDBClient
from qmds.db.product_db import ProductDBClient


def main():
    print(f"开始重建 _counters 集合...")
    print(f"  MongoDB: {settings.mongo_uri}")

    # 1. qmds_url_stores 计数器
    print(f"\n{'='*50}")
    print(f"[qmds_url_stores] 数据库: {settings.mongo_db_url}")
    print(f"{'='*50}")
    db = MongoDBClient()
    try:
        result = db.rebuild_counters()
        print(f"  重建: {result['rebuilt']} 个集合, 错误: {len(result['errors'])} 个")
        for err in result["errors"]:
            print(f"    - {err}")

        all_counts = db.get_all_collection_counts()
        for ctype, items in all_counts.items():
            print(f"\n  [{ctype}] ({len(items)} 个集合)")
            for item in items[:5]:
                cid = item.get("_id", "")
                total = item.get("total", 0)
                counts = item.get("counts", {})
                parts = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
                print(f"    {cid}: total={total} | {parts}")
            if len(items) > 5:
                print(f"    ... 还有 {len(items) - 5} 个集合")
    finally:
        db.close()

    # 2. qmds_product_data 计数器
    print(f"\n{'='*50}")
    print(f"[qmds_product_data] 数据库")
    print(f"{'='*50}")
    pdb = ProductDBClient()
    try:
        result = pdb.rebuild_product_counters()
        print(f"  重建: {result['rebuilt']} 个集合, 错误: {len(result['errors'])} 个")
        for err in result["errors"]:
            print(f"    - {err}")

        all_counts = pdb.get_all_collection_counts()
        items = all_counts.get("product", [])
        print(f"\n  [product] ({len(items)} 个集合)")
        for item in items[:5]:
            cid = item.get("_id", "")
            total = item.get("total", 0)
            counts = item.get("counts", {})
            parts = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
            print(f"    {cid}: total={total} | {parts}")
        if len(items) > 5:
            print(f"    ... 还有 {len(items) - 5} 个集合")
    finally:
        pdb.close()

    print(f"\n全部重建完成。")


if __name__ == "__main__":
    main()
