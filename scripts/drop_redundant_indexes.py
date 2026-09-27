"""删除商品集合中审计确认无查询使用的冗余索引

索引审计结论（见 product_db._REDUNDANT_PRODUCT_INDEXES）：
source_url / crawl_time / 分类 / clean_status / clean_time / export_time /
export_count / last_export_time / 标题 —— 全库无查询使用（单字段 clean_status
已被 idx_clean_export_status 复合前缀覆盖），删除后每次插入维护的索引从 15 个
降到 6 个，显著降低写放大与索引体积。

用法:
    python scripts/drop_redundant_indexes.py                      # dry-run 只列出
    python scripts/drop_redundant_indexes.py --apply              # 实际删除（全部集合）
    python scripts/drop_redundant_indexes.py --apply --collection vehicles_parts__auto_accessories media__books
"""
import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.db.product_db import ProductDBClient


def iter_collections(db: ProductDBClient):
    """qmds_product_data 下的商品集合（{category}__{subcategory}）"""
    for name in db.db.list_collection_names():
        if name.startswith("_") or "__" not in name:
            continue
        category, subcategory = name.split("__", 1)
        yield name, category, subcategory


def main():
    parser = argparse.ArgumentParser(description="删除冗余索引（降低 insert 写放大）")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run）")
    parser.add_argument("--collection", nargs="*", default=None,
                        help="限定集合名（category__subcategory），默认全部商品集合")
    args = parser.parse_args()

    db = ProductDBClient()
    try:
        if args.collection:
            pairs = []
            for name in args.collection:
                if "__" not in name:
                    print(f"[跳过] {name}: 集合名格式应为 category__subcategory")
                    continue
                category, subcategory = name.split("__", 1)
                pairs.append((name, category, subcategory))
        else:
            pairs = list(iter_collections(db))

        touched = 0
        total = 0
        for name, category, subcategory in pairs:
            dropped = db.drop_redundant_indexes(category, subcategory, dry_run=not args.apply)
            if dropped:
                touched += 1
                total += len(dropped)
                action = "已删除" if args.apply else "待删除"
                print(f"[{name}] {action}: {', '.join(dropped)}")

        mode = "已删除" if args.apply else "待删除（dry-run）"
        print(f"\n共 {touched} 个集合，{mode} {total} 个冗余索引")
        if not args.apply and total:
            print("确认无误后加 --apply 实际执行")
    finally:
        db.close()


if __name__ == "__main__":
    main()
