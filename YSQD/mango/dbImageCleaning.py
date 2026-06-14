from pymongo import MongoClient
import re

# -------------------------------
# 1. MongoDB 配置
# -------------------------------
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "shopify_data_new"        # 你的数据库名

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# -------------------------------
# 2. 图片字段中需要删除的关键词
# -------------------------------
REMOVE_KEYWORDS = ["coming-soon", "noimage", "default", ".svg"]

# 生成正则列表
regex_list = [{"图片": {"$regex": kw, "$options": "i"}} for kw in REMOVE_KEYWORDS]


# -------------------------------
# 3. 删除规则封装为函数（处理单个集合）
# -------------------------------
def clean_collection(col_name):
    col = db[col_name]

    query = {
        "$or": regex_list + [
            {"价格": {"$lt": 3}},            # 价格 < 3
            {"价格": {"$gt": 6000}},         # 价格 > 6000
            {"描述": {"$exists": False}},    # 描述字段不存在
            {"描述": ""},                    # 描述字段为空字符串
            {"描述": {"$regex": r"^.{0,29}$"}},  # 描述长度小于 30 字符
            {"标题": {"$regex": r"^.{0,7}$"}},  # 标题长度小于 8 字符

            # ⭐ 标题为纯数字（如 "12345"）
            {"标题": {"$regex": r"^\s*\d+\s*$"}},

            # ⭐ 新增：图片字段为空的各种情况
            {"图片": {"$exists": False}},           # 图片字段不存在
            {"图片": None},                         # 图片字段为 null
            {"图片": ""},                           # 图片字段为空字符串
            {"图片": {"$size": 0}},                 # 图片字段是空数组 []
            {"图片": {"$eq": []}}                   # 同上，明确匹配空数组
        ]
    }

    result = col.delete_many(query)

    print(f"集合《{col_name}》 已删除 {result.deleted_count} 条数据")


# -------------------------------
# 4. 主程序：自动遍历当前数据库下所有集合
# -------------------------------
if __name__ == "__main__":
    collections = db.list_collection_names()

    print("\n=============== 开始清理所有集合 ===============")
    print(f"发现 {len(collections)} 个集合\n")

    for col_name in collections:
        clean_collection(col_name)

    print("\n=============== 所有集合清理完成 ===============")