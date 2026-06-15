"""MongoDB 类目迁移脚本

将旧类目集合重命名为 Google Product Taxonomy 一级分类名称。
用法:
    python scripts/migrate_categories.py              # dry-run 预览
    python scripts/migrate_categories.py --execute     # 实际执行
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pymongo import MongoClient
from qmds.config import settings
from qmds.config.categories import OLD_TO_NEW_CATEGORY
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("migrate")


def migrate(dry_run: bool = True):
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[settings.mongo_db_url]
    existing = set(db.list_collection_names())

    actions = []  # (old_name, new_name, new_cat)

    for old_cat, new_cat in OLD_TO_NEW_CATEGORY.items():
        if old_cat == new_cat:
            continue
        for suffix in ("_unfiltered", "_filtered"):
            old_name = f"{old_cat}{suffix}"
            new_name = f"{new_cat}{suffix}"
            if old_name in existing:
                actions.append((old_name, new_name, new_cat))

    if not actions:
        log.info("没有需要迁移的集合")
        return

    log.info(f"{'[DRY-RUN] ' if dry_run else ''}共 {len(actions)} 个集合需要迁移:")
    for old_name, new_name, _ in actions:
        log.info(f"  {old_name} -> {new_name}")

    if dry_run:
        log.info("这是 dry-run 模式，未做任何修改。加 --execute 参数执行实际迁移。")
        return

    for old_name, new_name, new_cat in actions:
        # 检查目标集合是否已存在
        if new_name in existing:
            log.warning(f"目标集合 {new_name} 已存在，跳过重命名 {old_name}")
            continue

        # 重命名集合
        db[old_name].rename(new_name)
        log.info(f"已重命名: {old_name} -> {new_name}")

        # 更新文档中的 category 字段
        col = db[new_name]
        result = col.update_many(
            {"category": {"$ne": new_cat}},
            [{"$set": {"category": new_cat}}],
        )
        if result.modified_count:
            log.info(f"  更新 category 字段: {result.modified_count} 条文档")

        # 更新 filtered 集合中的 classified_from 字段
        if new_name.endswith("_filtered"):
            old_classified_from = old_name.replace("_filtered", "_unfiltered")
            new_classified_from = f"{new_cat}_unfiltered"
            result2 = col.update_many(
                {"classified_from": old_classified_from},
                {"$set": {"classified_from": new_classified_from}},
            )
            if result2.modified_count:
                log.info(f"  更新 classified_from 字段: {result2.modified_count} 条文档")

        existing.add(new_name)

    log.info("迁移完成")


def main():
    parser = argparse.ArgumentParser(description="MongoDB 类目迁移脚本")
    parser.add_argument("--execute", action="store_true", help="实际执行迁移（默认 dry-run）")
    args = parser.parse_args()

    setup_logger()
    dry_run = not args.execute
    migrate(dry_run=dry_run)


if __name__ == "__main__":
    main()
