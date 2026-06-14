from pymongo import MongoClient
import sys

# ==================== 固定配置区 ====================
MONGO_URI = "mongodb://localhost:27017/"

SOURCE_DB_NAME = "shopify_data_backup"   # 固定
TARGET_DB_NAME = "shopify_data_new"      # 固定

BATCH_SIZE = 1000
DROP_TARGET_FIRST = True
# ==================================================


def choose_collection(db):
    collections = db.list_collection_names()

    if not collections:
        print("源数据库中没有任何集合")
        sys.exit(1)

    print("\n发现以下集合：")
    for i, name in enumerate(collections):
        print(f"[{i}] {name}")

    while True:
        try:
            idx = int(input("\n请选择要迁移的集合编号："))
            if 0 <= idx < len(collections):
                return collections[idx]
            print("编号超出范围")
        except ValueError:
            print("请输入数字")


def transfer_collection():
    client = MongoClient(MONGO_URI)

    # 🔒 数据库固定
    source_db = client[SOURCE_DB_NAME]
    target_db = client[TARGET_DB_NAME]

    # 🔍 只选择集合
    SOURCE_COLL_NAME = choose_collection(source_db)
    TARGET_COLL_NAME = SOURCE_COLL_NAME

    source_coll = source_db[SOURCE_COLL_NAME]
    target_coll = target_db[TARGET_COLL_NAME]

    total = source_coll.count_documents({})
    if total == 0:
        print("源集合无数据，退出")
        return

    print(f"\n开始迁移：")
    print(f"{SOURCE_DB_NAME}.{SOURCE_COLL_NAME}")
    print(f"→ {TARGET_DB_NAME}.{TARGET_COLL_NAME}")
    print(f"共 {total} 条数据")

    if DROP_TARGET_FIRST:
        deleted = target_coll.delete_many({}).deleted_count
        print(f"目标集合已清空（删除 {deleted} 条）")

    batch = []
    transferred = 0
    cursor = source_coll.find({}, no_cursor_timeout=True)

    try:
        for doc in cursor:
            doc.pop("_id", None)
            batch.append(doc)
            transferred += 1

            if len(batch) >= BATCH_SIZE:
                target_coll.insert_many(batch)
                print(f"已转移 {transferred}/{total}")
                batch.clear()

        if batch:
            target_coll.insert_many(batch)
            print(f"已转移 {transferred}/{total}")

    except Exception as e:
        print(f"发生错误：{e}")
        sys.exit(1)

    finally:
        cursor.close()

    print("\n✅ 迁移完成")
    print(f"目标集合文档数：{target_coll.count_documents({})}")


if __name__ == "__main__":
    transfer_collection()
