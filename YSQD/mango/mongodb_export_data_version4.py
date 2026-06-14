#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MongoDB 数据导出 + 备份工具（支持 MongoDB 8.x）
功能：
1️⃣ 从 shopify_data 数据库导出集合为 Excel
2️⃣ 每个集合导出为 {集合名}.xlsx
3️⃣ 文件存储路径：C:/Users/Administrator/Desktop/shopify导出数据文件
4️⃣ 导出后仅备份“导出的商品”到 shopify_data_backup 数据库
5️⃣ 从原数据库中删除已导出的商品（未导出的商品继续保留）
6️⃣ 支持选择性导出 & 限制导出商品数量
7️⃣ 导出的表头必须包含以下字段：
   ['SKU', '标题', '描述', '子描述', '图片', '原价', '折扣价', '变体名', '变体值', '分类']
"""

import os
import sys
import pandas as pd
from pymongo import MongoClient

# ---------------------- 基础配置 ----------------------
MONGO_URI = "mongodb://localhost:27017/"
EXPORT_DIR = r"C:\Users\admin\Desktop\shopify导出数据文件"
SOURCE_DB_NAME = "shopify_data_new"
BACKUP_DB_NAME = "shopify_data_backup"

# 要求的字段
REQUIRED_COLUMNS = ['SKU', '标题', '描述', '子描述', '图片', '原价', '折扣价', '变体名', '变体值', '分类']

# ---------------------- 连接 MongoDB ----------------------
def connect_mongo(uri=MONGO_URI):
    """连接到 MongoDB"""
    try:
        client = MongoClient(uri)
        print(f"✅ 成功连接到 MongoDB: {uri}")
        return client
    except Exception as e:
        print(f"❌ 连接 MongoDB 失败: {e}")
        sys.exit(1)

# ---------------------- 导出集合为 Excel（支持分批导出） ----------------------
def export_collection_to_excel(db, coll_name, export_dir, limit=None, batch_size=1000):
    """导出集合为 Excel 文件（分批导出避免爆内存，兼容 pandas 新版本）"""
    try:
        coll = db[coll_name]

        # 计算总量
        total_count = coll.count_documents({}) if limit is None else min(coll.count_documents({}), limit)
        if total_count == 0:
            print(f"⚠️ 集合 {coll_name} 无数据，跳过导出。")
            return False, []

        exported_ids = []  # 记录导出的 ID
        os.makedirs(export_dir, exist_ok=True)
        file_path = os.path.join(export_dir, f"{coll_name}.xlsx")

        # 如果文件已存在，先删除
        if os.path.exists(file_path):
            os.remove(file_path)

        start = 0
        with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
            while start < total_count:
                batch_limit = min(batch_size, total_count - start)
                batch_data = list(coll.find({}).skip(start).limit(batch_limit))
                if not batch_data:
                    break

                df = pd.DataFrame(batch_data)
                batch_ids = [doc["_id"] for doc in batch_data]
                exported_ids.extend(batch_ids)

                # 清理 _id
                if "_id" in df.columns:
                    df.drop(columns=["_id"], inplace=True)

                # 确保包含所需字段
                for col in REQUIRED_COLUMNS:
                    if col not in df.columns:
                        df[col] = ""

                # 调整列顺序
                ordered_cols = REQUIRED_COLUMNS + [c for c in df.columns if c not in REQUIRED_COLUMNS]
                df = df[ordered_cols]

                # 处理非法字符
                df.replace({r'[\x00-\x1F\x7F]+': ''}, regex=True, inplace=True)

                # 截断超出 Excel 单元格限制的内容（上限 32767 字符）
                MAX_CELL_LEN = 32760
                for col_name in df.columns:
                    df[col_name] = df[col_name].apply(
                        lambda v: (v[:MAX_CELL_LEN] + "...(truncated)") if isinstance(v, str) and len(v) > MAX_CELL_LEN else v
                    )

                # 写入 Excel（追加模式）
                if start == 0:
                    df.to_excel(writer, index=False, sheet_name='Sheet1')
                else:
                    df.to_excel(writer, index=False, header=False, startrow=start, sheet_name='Sheet1')

                start += batch_limit
                print(f"➡️ 已导出 {start}/{total_count} 行...")

        print(f"✅ 导出完成: {file_path} ({len(exported_ids)} 行)")

        return True, exported_ids

    except Exception as e:
        print(f"❌ 导出集合 {coll_name} 出错: {e}")
        return False, []

# ---------------------- 备份并删除“已导出”的商品 ----------------------
def backup_exported_docs(source_db, backup_db, coll_name, exported_ids):
    """仅备份导出的商品，并从源集合删除对应文档"""
    if not exported_ids:
        print(f"⚠️ 集合 {coll_name} 没有可备份的文档，跳过。")
        return

    try:
        source_coll = source_db[coll_name]
        backup_coll = backup_db[coll_name]

        docs = list(source_coll.find({"_id": {"$in": exported_ids}}))
        if docs:
            backup_coll.insert_many(docs)
            print(f"✅ 已备份 {len(docs)} 条商品到 {BACKUP_DB_NAME}.{coll_name}")

        # 删除已备份的数据
        source_coll.delete_many({"_id": {"$in": exported_ids}})
        print(f"🗑 已从源集合删除 {len(exported_ids)} 条商品")

    except Exception as e:
        print(f"❌ 备份/删除商品时出错: {e}")

# ---------------------- 主函数 ----------------------
def main():
    client = connect_mongo()
    source_db = client[SOURCE_DB_NAME]
    backup_db = client[BACKUP_DB_NAME]

    # 获取集合列表
    collections = source_db.list_collection_names()
    if not collections:
        print(f"⚠️ 数据库 {SOURCE_DB_NAME} 中没有集合。")
        sys.exit(0)

    print(f"\n📦 检测到 {len(collections)} 个集合：")
    for i, name in enumerate(collections, 1):
        print(f"  {i}. {name}")

    # ---------------------- 选择集合 ----------------------
    choice = input("\n是否导出所有集合？(y=是 / n=选择部分): ").strip().lower()
    if choice == "y":
        selected_collections = collections
    else:
        selected_input = input("请输入要导出的集合编号（如 1,3,5）: ").strip()
        try:
            selected_collections = [
                collections[int(i) - 1]
                for i in selected_input.split(",")
                if i.strip().isdigit() and 1 <= int(i) <= len(collections)
            ]
        except Exception:
            print("❌ 输入格式错误。")
            sys.exit(1)

    if not selected_collections:
        print("❌ 未选择任何集合，程序结束。")
        sys.exit(0)

    # ---------------------- 选择导出的商品数量 ----------------------
    limit_input = input("\n请输入要导出的商品数量（0=导出全部）: ").strip()
    limit = int(limit_input) if limit_input.isdigit() else 0
    if limit <= 0:
        limit = None  # 导出全部

    print("\n🚀 开始导出、备份和删除选中的商品...\n")

    # ---------------------- 循环处理集合 ----------------------
    for coll_name in selected_collections:
        success, exported_ids = export_collection_to_excel(source_db, coll_name, EXPORT_DIR, limit)

        if success:
            backup_exported_docs(source_db, backup_db, coll_name, exported_ids)

    print("\n🎉 所有任务完成！文件已保存至：", os.path.abspath(EXPORT_DIR))
    print(f"📦 已备份至数据库：{BACKUP_DB_NAME}")

# ---------------------- 入口 ----------------------
if __name__ == "__main__":
    main()
