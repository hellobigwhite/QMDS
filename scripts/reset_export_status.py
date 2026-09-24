"""把指定集合的导出状态重置为未导出

将集合中 export_status=exported 的文档改为 unexported（并清空导出时间），
随后重建 _counters 计数器，保证页面统计一致。

用法:
    python scripts/reset_export_status.py --collection arts_entertainment__art
    python scripts/reset_export_status.py --collection arts_entertainment__art arts_entertainment__collectibles arts_entertainment__crafts
    （不传 --collection 时默认处理 art/collectibles/crafts 三个集合）
"""
import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.db.product_db import EXPORT_STATUS_EXPORTED, EXPORT_STATUS_UNEXPORTED, ProductDBClient

DEFAULT_COLLECTIONS = [
    "arts_entertainment__art",
    "arts_entertainment__collectibles",
    "arts_entertainment__crafts",
]


def main():
    parser = argparse.ArgumentParser(description="重置导出状态为未导出")
    parser.add_argument("--collection", nargs="+", default=DEFAULT_COLLECTIONS,
                        help="集合名列表（category__subcategory 格式），默认 art/collectibles/crafts")
    parser.add_argument("--rebuild", action="store_true", default=True,
                        help="重置后重建计数器（默认开启）")
    args = parser.parse_args()

    db = ProductDBClient()
    total_modified = 0
    try:
        for prefix in args.collection:
            if "__" not in prefix:
                print(f"[跳过] {prefix}: 集合名格式应为 category__subcategory")
                continue
            category, subcategory = prefix.split("__", 1)
            col = db.collection(category, subcategory)
            exported = col.count_documents({"export_status": EXPORT_STATUS_EXPORTED})
            if exported == 0:
                print(f"[{prefix}] 没有已导出的数据，无需修改")
                continue
            result = col.update_many(
                {"export_status": EXPORT_STATUS_EXPORTED},
                {"$set": {
                    "export_status": EXPORT_STATUS_UNEXPORTED,
                    "export_time": None,
                    "last_export_time": None,
                }},
            )
            total_modified += result.modified_count
            print(f"[{prefix}] 已导出 {exported} 条 -> 已重置 {result.modified_count} 条为未导出")
            print(f"          重置后该集合已导出数: {col.count_documents({'export_status': EXPORT_STATUS_EXPORTED})} 条")

        if args.rebuild:
            print("\n重建 _counters 计数器...")
            result = db.rebuild_product_counters()
            print(f"重建完成: {result['rebuilt']} 个集合, 错误 {len(result.get('errors', []))} 个")
            for err in result.get("errors", []):
                print(f"  - {err}")

        print(f"\n完成: 共重置 {total_modified} 条")
    finally:
        db.close()


if __name__ == "__main__":
    main()
