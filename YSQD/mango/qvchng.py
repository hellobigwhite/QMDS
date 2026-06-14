#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专用去重脚本 - 根据中文字段 "标题" 去重
优化输出，百万级数据仅每 1000 条输出一次
"""

from pymongo import MongoClient
import argparse
import sys
from src.utils.logger import setup_logger
logger = setup_logger('deduplicate', 'logs/deduplicate.log')


MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "shopify_data_new"

parser = argparse.ArgumentParser()
parser.add_argument("--collection", type=str, required=True, help="要处理的集合名")
args = parser.parse_args()

DRY_RUN = False  # 永远直接去重

def connect_and_choose_collection():
    try:
        client = MongoClient(MONGO_URI)
        client.server_info()
        logger.info("✅ MongoDB 连接成功")

        db = client[DB_NAME]
        collections = [name for name in db.list_collection_names() if not name.startswith("system.")]

        if args.collection in collections:
            coll_name = args.collection
        else:
            logger.error(f"❌ 指定集合 {args.collection} 不存在")
            sys.exit(1)

        logger.info(f"✅ 已选择集合: {coll_name}")
        return db[coll_name]
    except Exception as e:
        logger.error(f"❌ 连接失败: {e}")
        sys.exit(1)

def deduplicate_by_chinese_title(coll):
    logger.info(f"正在处理 {DB_NAME}.{coll.name} （去重字段: 标题）")
    total = coll.count_documents({})
    logger.info(f"总文档数: {total}")

    if total == 0:
        logger.info("集合为空，无需处理")
        return

    title_to_ids = {}
    empty_title_ids = []

    cursor = coll.find({}, {"标题": 1}).sort("_id", 1)
    logger.info("正在扫描所有文档，建立标题映射...")

    for i, doc in enumerate(cursor, 1):
        if i % 1000 == 0 or i == total:  # 每 1000 条输出一次
            logger.info(f"进度: {i}/{total}")

        raw_title = doc.get("标题")
        if raw_title is None or str(raw_title).strip() == "":
            empty_title_ids.append(doc["_id"])
            continue

        title = str(raw_title).strip()
        title_to_ids.setdefault(title, []).append(doc["_id"])

    duplicates = {t: ids for t, ids in title_to_ids.items() if len(ids) > 1}
    to_delete = sum(len(ids) - 1 for ids in duplicates.values())

    logger.info("=== 去重分析结果 ===")
    logger.info(f"重复标题组: {len(duplicates)}，可删除: {to_delete} 条")
    logger.info(f"标题为空文档: {len(empty_title_ids)} 条（保留）")
    logger.info(f"去重后预计剩余: {total - to_delete} 条")

    if to_delete == 0:
        logger.info("数据已干净，无重复！")
        return

    logger.info("前 15 个重复标题示例：")
    for i, (title, ids) in enumerate(duplicates.items()):
        if i >= 15: break
        logger.info(f"  \"{title}\" → 重复 {len(ids)} 次")

    logger.info("🚀 开始实际删除重复文档...")
    deleted = 0
    batch_size = 10000

    for title, ids in duplicates.items():
        ids.sort()
        delete_ids = ids[1:]
        for start in range(0, len(delete_ids), batch_size):
            batch = delete_ids[start:start + batch_size]
            res = coll.delete_many({"_id": {"$in": batch}})
            deleted += res.deleted_count
        logger.info(f"已清理: \"{title}\" → 删除 {len(delete_ids)} 条，保留1条")

    logger.info(f"🎉 去重完成！共删除 {deleted} 条重复文档")
    logger.info(f"当前剩余文档数: {coll.count_documents({})} 条")

if __name__ == "__main__":
    coll = connect_and_choose_collection()
    deduplicate_by_chinese_title(coll)
