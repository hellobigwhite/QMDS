import os
import re
import json
import random
from pymongo import MongoClient

# -------------------------------
# 1. MongoDB 配置
# -------------------------------
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "shopify_data_new"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# -------------------------------
# 2. 自动选择类目
# -------------------------------
collections = db.list_collection_names()
if not collections:
    print("❌ 数据库中没有任何集合")
    exit(1)

print("可处理的类目列表：")
for i, col_name in enumerate(collections):
    print(f"{i + 1}. {col_name}")

# 用户选择
selected_index = input(f"请选择要处理的类目（输入序号 1-{len(collections)}，默认 1）: ").strip()
if selected_index.isdigit() and 1 <= int(selected_index) <= len(collections):
    CATEGORY = collections[int(selected_index) - 1]
else:
    CATEGORY = collections[0]

print(f"✅ 已选择处理类目: {CATEGORY}")

COLLECTION_NAME = CATEGORY
col = db[COLLECTION_NAME]

# -------------------------------
# 3. 加载关键词词库
# -------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORDS_FILE = os.path.join(BASE_DIR, "data", "keywords", f"{CATEGORY}.json")

def load_keywords(filepath):
    if not os.path.exists(filepath):
        print(f"❌ 关键词配置文件不存在: {filepath}")
        exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
        print(f"✅ 已加载关键词词库 [{CATEGORY}]，共 {len(data)} 个一级分类")
        return data

KEYWORDS = load_keywords(KEYWORDS_FILE)

# -------------------------------
# 4. 构建关键词匹配正则列表
# -------------------------------
keyword_patterns = []
for level1, level2_dict in KEYWORDS.items():
    for level2, words in level2_dict.items():
        if isinstance(words, list):
            for word in words:
                keyword_patterns.append({
                    "level1": level1,
                    "level2": level2,
                    "keyword": word,
                    "pattern": re.compile(re.escape(word), re.IGNORECASE)
                })
        else:
            for level3, subwords in words.items():
                for word in subwords:
                    keyword_patterns.append({
                        "level1": level1,
                        "level2": level2,
                        "level3": level3,
                        "keyword": word,
                        "pattern": re.compile(re.escape(word), re.IGNORECASE)
                    })

# -------------------------------
# 5. 分类逻辑
# -------------------------------
def classify_products():
    query = {"$or": [{"分类": {"$exists": False}}, {"分类": None}, {"分类": ""}, {"分类": []}]}
    cursor = col.find(query)
    updated = 0

    for item in cursor:
        title = item.get("标题", "")
        desc = item.get("描述", "")
        text = f"{title} {desc}".lower()

        matched = False
        selected_path = None
        selected_keyword = None

        for rule in keyword_patterns:
            if rule["pattern"].search(text):
                if "level3" in rule:
                    selected_path = [rule["level1"], rule["level2"], rule["level3"], rule["keyword"]]
                else:
                    selected_path = [rule["level1"], rule["level2"], rule["keyword"]]
                selected_keyword = rule["keyword"]
                matched = True
                break

        if not matched:
            # 随机分配
            level1 = random.choice(list(KEYWORDS.keys()))
            level2_candidates = KEYWORDS[level1]
            if isinstance(next(iter(level2_candidates.values())), list):
                level2 = random.choice(list(level2_candidates.keys()))
                keyword = random.choice(level2_candidates[level2])
                selected_path = [level1, level2, keyword]
            else:
                level2 = random.choice(list(level2_candidates.keys()))
                level3 = random.choice(list(level2_candidates[level2].keys()))
                keyword = random.choice(level2_candidates[level2][level3])
                selected_path = [level1, level2, level3, keyword]
            selected_keyword = keyword
            print(f"[RANDOM] {item['_id']} → {selected_path}")
        else:
            print(f"[MATCHED] {item['_id']} → {selected_path}")

        final_category = "|||".join(selected_path)

        col.update_one(
            {"_id": item["_id"]},
            {"$set": {"分类": final_category, "分类层级": selected_path, "命中关键词": selected_keyword}}
        )
        updated += 1

    print(f"\n>>> [{CATEGORY.upper()}] 共更新分类 {updated} 条商品")


if __name__ == "__main__":
    if len(KEYWORDS) == 0:
        print("❌ 无法运行：未加载到任何关键词")
    else:
        classify_products()
