"""[已废弃] 将旧的一级分类集合拆分到二级分类集合

此脚本用于旧版本（三后缀集合模式 _raw/_clean/_export 和 _filtered/_crawled/_unfiltered）的迁移。
当前版本已采用单一集合模式，此脚本保留仅供历史参考，不应再执行。

迁移逻辑（旧）:
  1. qmds_product_data:
     - 遍历所有 {cat}_raw / {cat}_clean / {cat}_export 集合（旧格式，无 __ 分隔符）
     - 按文档的 source_subcategory 字段分组（空值归入 "other"）
     - 写入 {cat}__{sub}_raw / {cat}__{sub}_clean / {cat}__{sub}_export
  2. qmds_url_stores:
     - 遍历所有 {cat}_filtered / {cat}_crawled 集合（旧格式，无 __ 分隔符）
     - 按文档的 subcategory 字段分组（空值归入 "other"）
     - 写入 {cat}__{sub}_filtered / {cat}__{sub}_crawled
     - {cat}_unfiltered 不动（始终按一级分类）
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pymongo import MongoClient
from qmds.config import settings
from qmds.config.categories import (
    SHOPIFY_CATEGORIES,
    CATEGORY_SEPARATOR,
    normalize_subcategory,
    make_collection_prefix,
)
from qmds.db.product_db import PRODUCT_DB_NAME, RAW_SUFFIX, CLEAN_SUFFIX, EXPORT_SUFFIX
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("migrate_subcategory")

URL_DB_NAME = settings.mongo_db_url  # qmds_url_stores


def is_old_format_collection(name: str, suffix: str) -> bool:
    """判断集合名是否为旧格式（不含 __ 分隔符）"""
    if not name.endswith(suffix):
        return False
    prefix = name[: -len(suffix)]
    if not prefix:
        return False
    # 旧格式: 不含 __ 分隔符，且 prefix 是已知的一级分类
    if CATEGORY_SEPARATOR in prefix:
        return False  # 新格式
    return prefix in SHOPIFY_CATEGORIES


def split_collection(db, old_name: str, suffix: str, subcategory_field: str, dry_run: bool) -> dict:
    """将旧集合按 subcategory 字段拆分到新集合

    Args:
        db: 数据库对象
        old_name: 旧集合名（如 electronics_raw）
        suffix: 集合后缀（如 _raw）
        subcategory_field: 文档中二级分类字段名（如 source_subcategory 或 subcategory）
        dry_run: 是否只预览

    Returns:
        {"category": str, "groups": {sub: count}, "total": int}
    """
    old_col = db[old_name]
    category = old_name[: -len(suffix)]

    # 按 subcategory 分组
    groups = defaultdict(list)
    for doc in old_col.find({}, {"_id": 1, subcategory_field: 1}):
        sub = doc.get(subcategory_field, "")
        sub_norm = normalize_subcategory(sub)
        groups[sub_norm].append(doc["_id"])

    total = sum(len(ids) for ids in groups.values())
    result = {"category": category, "groups": {sub: len(ids) for sub, ids in groups.items()}, "total": total}

    if dry_run:
        return result

    # 实际迁移：分批读取并写入新集合
    for sub, id_list in groups.items():
        new_prefix = make_collection_prefix(category, sub)
        new_name = f"{new_prefix}{suffix}"
        new_col = db[new_name]

        # 分批处理
        batch_size = 1000
        for i in range(0, len(id_list), batch_size):
            batch_ids = id_list[i : i + batch_size]
            docs = list(old_col.find({"_id": {"$in": batch_ids}}))
            if docs:
                # 移除 _id 以避免冲突
                for doc in docs:
                    doc.pop("_id", None)
                new_col.insert_many(docs, ordered=False)
                old_col.delete_many({"_id": {"$in": batch_ids}})

        log.info(f"  {old_name} -> {new_name}: {len(id_list)} 条")

    return result


def migrate(dry_run: bool, drop_old: bool):
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")

    # ── 1. 迁移 qmds_product_data ──
    log.info(f"{'[DRY-RUN] ' if dry_run else ''}开始迁移 {PRODUCT_DB_NAME}")
    product_db = client[PRODUCT_DB_NAME]
    product_tasks = []

    for suffix, sub_field in [(RAW_SUFFIX, "source_subcategory"), (CLEAN_SUFFIX, "source_subcategory"), (EXPORT_SUFFIX, "source_subcategory")]:
        for name in sorted(product_db.list_collection_names()):
            if is_old_format_collection(name, suffix):
                product_tasks.append((name, suffix, sub_field))

    if product_tasks:
        log.info(f"{'[DRY-RUN] ' if dry_run else ''}发现 {len(product_tasks)} 个旧格式产品数据集合需要迁移:")
        for name, suffix, _ in product_tasks:
            result = split_collection(product_db, name, suffix, _, dry_run)
            log.info(f"  {name}: {result['total']} 条 -> {len(result['groups'])} 个子集合")
            for sub, count in sorted(result["groups"].items()):
                new_name = f"{make_collection_prefix(result['category'], sub)}{suffix}"
                log.info(f"    ├─ {new_name}: {count} 条")
    else:
        log.info("没有旧格式产品数据集合需要迁移")

    # ── 2. 迁移 qmds_url_stores ──
    log.info(f"{'[DRY-RUN] ' if dry_run else ''}开始迁移 {URL_DB_NAME}")
    url_db = client[URL_DB_NAME]
    url_tasks = []

    for suffix, sub_field in [("_filtered", "subcategory"), ("_crawled", "subcategory")]:
        for name in sorted(url_db.list_collection_names()):
            if is_old_format_collection(name, suffix):
                url_tasks.append((name, suffix, sub_field))

    if url_tasks:
        log.info(f"{'[DRY-RUN] ' if dry_run else ''}发现 {len(url_tasks)} 个旧格式 URL 存储集合需要迁移:")
        for name, suffix, sub_field in url_tasks:
            result = split_collection(url_db, name, suffix, sub_field, dry_run)
            log.info(f"  {name}: {result['total']} 条 -> {len(result['groups'])} 个子集合")
            for sub, count in sorted(result["groups"].items()):
                new_name = f"{make_collection_prefix(result['category'], sub)}{suffix}"
                log.info(f"    ├─ {new_name}: {count} 条")
    else:
        log.info("没有旧格式 URL 存储集合需要迁移")

    # ── 3. 删除旧集合 ──
    if not dry_run and drop_old:
        log.info("开始删除旧集合...")
        for name, suffix, _ in product_tasks:
            if product_db[name].count_documents({}) == 0:
                product_db.drop_collection(name)
                log.info(f"  已删除: {PRODUCT_DB_NAME}.{name}")
            else:
                log.warning(f"  跳过（非空）: {PRODUCT_DB_NAME}.{name}")
        for name, suffix, _ in url_tasks:
            if url_db[name].count_documents({}) == 0:
                url_db.drop_collection(name)
                log.info(f"  已删除: {URL_DB_NAME}.{name}")
            else:
                log.warning(f"  跳过（非空）: {URL_DB_NAME}.{name}")

    if dry_run:
        log.info("这是 dry-run 模式，未做任何修改。加 --execute 参数执行实际迁移。")
    else:
        log.info("迁移完成")

    client.close()


def main():
    parser = argparse.ArgumentParser(description="将旧的一级分类集合拆分到二级分类集合")
    parser.add_argument("--execute", action="store_true", help="实际执行迁移（默认 dry-run）")
    parser.add_argument("--drop-old", action="store_true", help="迁移后删除旧集合（仅在 --execute 时生效）")
    args = parser.parse_args()

    setup_logger()
    migrate(dry_run=not args.execute, drop_old=args.drop_old)


if __name__ == "__main__":
    main()
