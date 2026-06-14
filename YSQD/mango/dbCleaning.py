from pymongo import MongoClient
import re
from src.utils.logger import setup_logger
logger = setup_logger('db_cleaning', 'logs/db_cleaning.log')


# --------------------------------------
# 1. MongoDB 连接配置
# --------------------------------------
MONGO_URI = "mongodb://localhost:27017/"
client = MongoClient(MONGO_URI)

logger.info("===== Connected to MongoDB Standalone 8.2.1 Enterprise =====")
logger.info("Host: localhost:27017")
logger.info("Cluster: Standalone")
logger.info("Edition: MongoDB 8.2.1 Enterprise")

# 打印数据库与集合
logger.info("现有数据库与集合：")
for db_name in client.list_database_names():
    db_tmp = client[db_name]
    logger.info(f"- DB: {db_name}")
    try:
        for col_name in db_tmp.list_collection_names():
            logger.info(f"    - Collection: {col_name}")
    except:
        pass
logger.info("============================================================")

# --------------------------------------
# 2. 设置源数据表与回收站表（部分固定）
# --------------------------------------
SOURCE_DB = "shopify_data_new"
RECYCLE_DB = "recycle"
RECYCLE_COL = "products_deleted"

# 交互式选择源集合
db = client[SOURCE_DB]
collections = db.list_collection_names()
logger.info(f"源数据库 `{SOURCE_DB}` 下的集合：")
for i, name in enumerate(collections):
    logger.info(f"{i}. {name}")

col_index = int(input("请选择源集合编号: "))
SOURCE_COL = collections[col_index]
col = db[SOURCE_COL]

recycle_db = client[RECYCLE_DB]
recycle_col = recycle_db[RECYCLE_COL]

logger.info(f"已设置：\n源集合: {SOURCE_DB}.{SOURCE_COL}\n回收站集合: {RECYCLE_DB}.{RECYCLE_COL}")

# --------------------------------------
# 3. 关键词词库
# --------------------------------------
KEYWORDS = [
    "bicycle bibs", "bicycle jerseys", "bicycle shorts", "bicycle briefs",
    "bicycle skinsuits", "bicycle tights", "boxing shorts", "ghillie suits",
    "hunting vests", "fishing vests", "hunting tactical pants",
    "martial arts shorts", "motorcycle jackets", "motorcycle pants",
    "motorcycle suits", "paintball clothing",
    "baby toddler bottoms", "baby toddler diaper covers", "baby toddler dresses",
    "baby toddler outerwear", "baby toddler outfits", "baby toddler sleepwear",
    "baby toddler socks tights", "baby toddler swimwear", "baby toddler tops",
    "baby one pieces", "toddler underwear", "dresses", "jumpsuits", "rompers",
    "jumpsuits rompers", "leotards", "unitards", "overalls", "chaps",
    "coats jackets", "rain pants", "rain suits", "snow pants", "snow suits",
    "vests", "outfit sets", "pants", "shirts", "tops", "shirts tops", "shorts",
    "skirts", "skorts", "loungewear", "nightgowns", "pajamas", "robes",
    "pant suits", "skirt suits", "tuxedos", "swimwear", "dirndls",
    "hakama trousers", "japanese black formal wear", "kimono outerwear",
    "kimonos", "baptism dresses", "communion dresses", "saris", "lehengas",
    "traditional leather pants", "yukata", "bra strap pads",
    "breast enhancing inserts", "bras", "hosiery", "jock straps", "lingerie",
    "garter belts", "garters", "long johns", "petticoats", "pettipants",
    "shapewear", "socks", "undershirts", "underwear", "underwear slips",
    "contractor pants", "contractor coveralls", "flight suits", "chef hats",
    "chef jackets", "chef pants", "military uniforms", "school uniforms",
    "security uniforms", "baseball uniforms", "basketball uniforms",
    "cheerleading uniforms", "bridal party dresses", "wedding dresses",
    "T-Shirt", "Triblend Shirt", "Sweatshirt","Tee","Heroin", "Methamphetamine", "Cocaine", "Opium", "Marijuana",
    "Switchblade knives", "Daggers", "Triangular scrapers", "Illegal crossbows", "Controlled blades",
    "Counterfeit banknotes", "Fake securities", "Pirated DVDs", "Counterfeit luxury goods", "Unlicensed firearms",
    "Obscene DVDs", "Pornographic magazines", "Adult video discs", "Erotic photos", "Pornographic USB drives",
    "Ivory products", "Rhinoceros horn", "Tiger bones", "Pangolin scales", "Endangered animal specimens"
]

# 转为 OR 正则模式（大小写不敏感）
keywords_regex = re.compile("|".join([re.escape(k) for k in KEYWORDS]), re.IGNORECASE)

# --------------------------------------
# 4. 判断逻辑
# --------------------------------------
def need_recycle(item):
    """判断商品是否需要移到 recycle"""
    category = item.get("分类", "")
    title = item.get("标题", "")
    desc = item.get("描述", "")

    # 分类中匹配关键词
    if category and keywords_regex.search(str(category)):
        return True

    # 分类为空时，标题或描述匹配关键词
    if not category or str(category).strip() == "":
        text = f"{title} {desc}"
        if keywords_regex.search(text):
            return True

    return False

# --------------------------------------
# 5. 迁移流程
# --------------------------------------
def move_to_recycle():
    logger.info(f"开始从数据库 `{SOURCE_DB}.{SOURCE_COL}` 检查数据...")

    count = 0
    cursor = col.find()
    for item in cursor:
        if need_recycle(item):
            recycle_col.insert_one(item)
            col.delete_one({"_id": item["_id"]})
            count += 1

    logger.info(f">>> 已成功移动 {count} 条数据到 `{RECYCLE_DB}.{RECYCLE_COL}`")

# --------------------------------------
# 6. 运行脚本
# --------------------------------------
if __name__ == "__main__":
    move_to_recycle()
