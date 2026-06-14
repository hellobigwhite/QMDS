from pymongo import MongoClient
import os

# ---------------------- MongoDB 配置
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"

# ===================== 导出到桌面 tasks.txt =====================
DESKTOP = os.path.expanduser("~/Desktop")
OUTPUT_FILE = os.path.join(DESKTOP, "tasks.txt")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ========== 1. 获取所有可用的分类 ==========
all_collections = db.list_collection_names()
category_list = []

for coll_name in all_collections:
    if coll_name.endswith("_Unfiltered_URLs"):
        category = coll_name.replace("_Unfiltered_URLs", "")
        category_list.append(category)

if not category_list:
    print("❌ 数据库中没有找到任何店铺数据！")
    input("按回车退出")
    exit()

# ========== 2. 显示菜单供选择 ==========
print("=" * 50)
print("📋 可用的分类列表（请输入数字选择）：")
print("=" * 50)

for i, cat in enumerate(category_list, 1):
    print(f"  {i}. {cat}")

print(f"  0. 导出所有分类")
print("=" * 50)

# ========== 3. 用户选择 ==========
while True:
    try:
        choice = int(input("请输入数字：").strip())
        if 0 <= choice <= len(category_list):
            break
        else:
            print(f"❌ 请输入 0 ~ {len(category_list)} 之间的数字")
    except ValueError:
        print("❌ 请输入有效数字！")

# ========== 4. 提取 URL ==========
urls = set()

if choice == 0:
    # 导出全部
    for cat in category_list:
        coll = db[f"{cat}_Unfiltered_URLs"]
        for doc in coll.find({}, {"URL": 1}):
            if doc.get("URL"):
                urls.add(doc["URL"])
else:
    # 导出选中的单个分类
    selected_cat = category_list[choice - 1]
    coll = db[f"{selected_cat}_Unfiltered_URLs"]
    for doc in coll.find({}, {"URL": 1}):
        if doc.get("URL"):
            urls.add(doc["URL"])

# ========== 5. 写入桌面 tasks.txt ==========
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    for url in sorted(urls):
        f.write(url + "\n")

# ========== 完成 ==========
print("\n✅ 导出成功！")
print(f"📄 保存位置：桌面 → tasks.txt")
print(f"🔢 总 URL 数量：{len(urls)} 条")
input("\n按回车退出")