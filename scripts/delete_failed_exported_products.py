"""删除数据库中「清洗失败(failed)」和「已导出(exported)」的商品数据。

遍历 qmds_product_data 库所有产品集合，删除
    clean_status = failed  OR  export_status = exported
的文档，并同步重建 _counters 计数器。

用法:
    python scripts/delete_failed_exported_products.py
"""
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.db.product_db import (
    ProductDBClient,
    PRODUCT_DB_NAME,
    CLEAN_STATUS_FAILED,
    EXPORT_STATUS_EXPORTED,
)


def main():
    db = ProductDBClient()
    total_deleted = 0
    processed = 0
    errors = []
    start = time.time()

    try:
        print(f"数据库: {PRODUCT_DB_NAME}")
        items = db.list_categories_with_sub()
        print(f"产品集合数: {len(items)}")

        for item in items:
            prefix = item["prefix"]
            col = db.db[prefix]
            try:
                # 统计将删除的数量
                query = {"$or": [
                    {"clean_status": CLEAN_STATUS_FAILED},
                    {"export_status": EXPORT_STATUS_EXPORTED},
                ]}
                n_failed = col.count_documents({"clean_status": CLEAN_STATUS_FAILED})
                n_exported = col.count_documents({"export_status": EXPORT_STATUS_EXPORTED})
                n_to_delete = col.count_documents(query)
                if n_to_delete == 0:
                    print(f"[{prefix}] 无待删除数据")
                    continue

                result = col.delete_many(query)
                deleted = result.deleted_count
                total_deleted += deleted
                processed += 1
                print(f"[{prefix}] 删除 {deleted} 条 "
                      f"(failed={n_failed}, exported={n_exported})")
            except Exception as e:
                errors.append(f"{prefix}: {e}")
                print(f"[{prefix}] 错误: {e}", file=sys.stderr)

        print(f"\n删除完成: 共处理 {processed} 个集合, 删除 {total_deleted} 条, "
              f"耗时 {time.time() - start:.1f}s")
        if errors:
            print("错误列表:")
            for err in errors:
                print(f"  {err}")

        # 重建计数器，保证前端统计一致
        print("\n开始重建 _counters 计数器 ...")
        rebuild_start = time.time()
        result = db.rebuild_product_counters()
        print(f"计数器重建完成: rebuilt={result['rebuilt']}, "
              f"errors={len(result.get('errors', []))}, "
              f"耗时 {time.time() - rebuild_start:.1f}s")
        if result.get("errors"):
            for err in result["errors"]:
                print(f"  计数器错误: {err}", file=sys.stderr)
    finally:
        db.close()

    print("\n全部完成")


if __name__ == "__main__":
    main()
