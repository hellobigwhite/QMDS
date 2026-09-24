"""重置被误判失败的站点 crawl_status（配合 2026-09-23 的误杀修复）

背景：旧版爬取逻辑把 "meta.json 请求失败（429/403/超时/代理不可用）" 也当成
"非 Shopify 站点" 并标记 crawl_failed，导致大量被 CF 限流的真店被永久跳过。
修复后的代码只在 meta.json 明确 404/410/禁用时才判非 Shopify。

本脚本把 crawl_failed 的站点重置回 uncrawled，让修复后的代码重新判定：
- 真非 Shopify 的站会在几秒内被再次标记（meta.json 404 → absent）
- 被 CF 拦截的站会走"拦截不标记 + 任务尾重试"的新逻辑

用法：
    python scripts/reset_crawl_failed.py                # dry-run，只统计
    python scripts/reset_crawl_failed.py --apply        # 全部重置
    python scripts/reset_crawl_failed.py --apply --since 2026-09-21
                                                        # 只重置该日期之后
                                                        # 标记失败的站点
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qmds.db.mongodb import CRAWL_STATUS_FAILED, CRAWL_STATUS_UNCRAWLED, MongoDBClient


def main():
    parser = argparse.ArgumentParser(description="重置 crawl_failed 站点为 uncrawled")
    parser.add_argument("--apply", action="store_true", help="实际写库（缺省只统计）")
    parser.add_argument("--since", default="", help="只处理 crawl_time >= 该日期（YYYY-MM-DD）")
    args = parser.parse_args()

    query = {"crawl_status": CRAWL_STATUS_FAILED}
    if args.since:
        query["crawl_time"] = {"$gte": args.since}

    db = MongoDBClient()
    mdb = None
    for attr in ("db", "_db", "db_name"):
        if hasattr(db, attr):
            mdb = getattr(db, attr)
            break
    if mdb is None or isinstance(mdb, str):
        # db_name 是字符串时取 client[db_name]
        mdb = db.client[getattr(db, "db_name", "qmds")]

    total = 0
    print(f"{'集合':48s} {'待重置':>8s}")
    for name in sorted(mdb.list_collection_names()):
        col = mdb[name]
        n = col.count_documents(query)
        if n == 0:
            continue
        if args.apply:
            result = col.update_many(query, {"$set": {"crawl_status": CRAWL_STATUS_UNCRAWLED}})
            n = result.modified_count
        print(f"{name:48s} {n:>8d}")
        total += n
    db.close()

    action = "已重置" if args.apply else "可重置(dry-run)"
    print(f"\n{action}站点总数: {total}")
    if not args.apply:
        print("加 --apply 实际写库")


if __name__ == "__main__":
    main()
