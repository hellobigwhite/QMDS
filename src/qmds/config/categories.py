"""Google Product Taxonomy 一级分类定义

基于 Google Product Taxonomy Version: 2021-09-21
所有类目名称均使用简化英文格式（小写 + 下划线），用于 MongoDB 集合命名和 Web UI。
"""

import random
import re
from pathlib import Path

# 默认二级分类名（无二级分类时使用）
DEFAULT_SUBCATEGORY = "other"

# 集合名中一级分类与二级分类的分隔符（双下划线避免与类目名中的单下划线冲突）
CATEGORY_SEPARATOR = "__"


def normalize_subcategory(subcategory: str) -> str:
    """将二级分类名标准化为合法的 MongoDB 集合名片段

    规则:
    - 空字符串 -> "other"
    - 前后空格去除
    - 连续空格/制表符 -> 单个下划线
    - 移除 MongoDB 集合名非法字符及特殊符号（只保留字母、数字、下划线、Unicode字符如中文）
    - 英文转小写
    - 多个连续下划线合并为单个
    """
    if not subcategory or not subcategory.strip():
        return DEFAULT_SUBCATEGORY

    text = subcategory.strip()
    # 空格和制表符 -> 下划线
    text = re.sub(r"[\s]+", "_", text)
    # 移除所有非字母、非数字、非下划线、非 Unicode 字符（保留中文等）
    # 即移除 ASCII 标点符号和特殊字符
    text = re.sub(r"[^\w\u0080-\uffff]", "", text, flags=re.UNICODE)
    # 英文转小写（保留中文等非 ASCII 字符）
    text = text.lower()
    # 多个连续下划线合并
    text = re.sub(r"_+", "_", text)
    # 去除首尾下划线
    text = text.strip("_")
    # 处理后为空则返回默认值
    if not text:
        return DEFAULT_SUBCATEGORY
    return text


def make_collection_prefix(category: str, subcategory: str = "") -> str:
    """生成集合前缀: {category}__{normalized_subcategory}

    使用双下划线分隔一级和二级分类，避免与类目名中的单下划线冲突。

    示例:
        make_collection_prefix("electronics", "headphones") -> "electronics__headphones"
        make_collection_prefix("electronics", "") -> "electronics__other"
        make_collection_prefix("electronics") -> "electronics__other"
    """
    sub = normalize_subcategory(subcategory)
    return f"{category}{CATEGORY_SEPARATOR}{sub}"


def parse_collection_prefix(prefix: str) -> tuple:
    """从集合前缀解析出 (一级分类, 二级分类)

    示例:
        parse_collection_prefix("electronics__headphones") -> ("electronics", "headphones")
        parse_collection_prefix("electronics__other") -> ("electronics", "other")
        parse_collection_prefix("electronics") -> ("electronics", "other")  # 旧格式兼容
        parse_collection_prefix("animals_pet_supplies__pet_food") -> ("animals_pet_supplies", "pet_food")
    """
    if CATEGORY_SEPARATOR in prefix:
        parts = prefix.split(CATEGORY_SEPARATOR, 1)
        category = parts[0]
        subcategory = parts[1] if len(parts) > 1 and parts[1] else DEFAULT_SUBCATEGORY
        return category, subcategory
    # 旧格式（无分隔符），兼容已有数据
    return prefix, DEFAULT_SUBCATEGORY

# ── 21 个一级分类（简化名称） ────────────────────────────
SHOPIFY_CATEGORIES = [
    "animals_pet_supplies",
    "apparel_accessories",
    "arts_entertainment",
    "baby_toddler",
    "business_industrial",
    "cameras_optics",
    "electronics",
    "food_beverages_tobacco",
    "furniture",
    "hardware",
    "health_beauty",
    "home_garden",
    "luggage_bags",
    "mature",
    "media",
    "office_supplies",
    "religious_ceremonial",
    "software",
    "sporting_goods",
    "toys_games",
    "vehicles_parts",
]

# ── 简化名称 → Google Taxonomy ID ───────────────────────
CATEGORY_ID_MAP = {
    "animals_pet_supplies": 1,
    "apparel_accessories": 166,
    "arts_entertainment": 8,
    "baby_toddler": 537,
    "business_industrial": 111,
    "cameras_optics": 141,
    "electronics": 222,
    "food_beverages_tobacco": 412,
    "furniture": 436,
    "hardware": 632,
    "health_beauty": 469,
    "home_garden": 536,
    "luggage_bags": 5181,
    "mature": 772,
    "media": 783,
    "office_supplies": 922,
    "religious_ceremonial": 5605,
    "software": 2092,
    "sporting_goods": 988,
    "toys_games": 1239,
    "vehicles_parts": 888,
}

# ── 简化名称 -> Google Taxonomy 一级分类名 ────────────────
# 用于从 cc_c.shopify_site_01 等源数据库中按 Google Taxonomy 名称查询
SHOPIFY_TO_GOOGLE_CATEGORY = {
    "animals_pet_supplies": "Animals & Pet Supplies",
    "apparel_accessories": "Apparel & Accessories",
    "arts_entertainment": "Arts & Entertainment",
    "baby_toddler": "Baby & Toddler",
    "business_industrial": "Business & Industrial",
    "cameras_optics": "Cameras & Optics",
    "electronics": "Electronics",
    "food_beverages_tobacco": "Food, Beverages & Tobacco",
    "furniture": "Furniture",
    "hardware": "Hardware",
    "health_beauty": "Health & Beauty",
    "home_garden": "Home & Garden",
    "luggage_bags": "Luggage & Bags",
    "mature": "Mature",
    "media": "Media",
    "office_supplies": "Office Supplies",
    "religious_ceremonial": "Religious & Ceremonial",
    "software": "Software",
    "sporting_goods": "Sporting Goods",
    "toys_games": "Toys & Games",
    "vehicles_parts": "Vehicles & Parts",
}


# ── 简化名称 -> 中文一级分类名（用于导出 BB 兼容的"自定义分类"列） ──
SHOPIFY_TO_CN_CATEGORY = {
    "animals_pet_supplies": "动物",
    "apparel_accessories": "服饰与配饰",
    "arts_entertainment": "艺术与娱乐",
    "baby_toddler": "婴幼儿用品",
    "business_industrial": "商业",
    "cameras_optics": "相机与光学器件",
    "electronics": "电子产品",
    "food_beverages_tobacco": "饮食",
    "furniture": "家具",
    "hardware": "五金",
    "health_beauty": "保健",
    "home_garden": "家居与园艺",
    "luggage_bags": "箱包",
    "mature": "成人",
    "media": "媒体",
    "office_supplies": "办公用品",
    "religious_ceremonial": "宗教",
    "software": "软件",
    "sporting_goods": "体育用品",
    "toys_games": "玩具",
    "vehicles_parts": "交通工具",
}


def get_cn_category_name(shopify_category: str) -> str:
    """将简化分类名转换为中文一级分类名，未知分类返回原值

    输入先规范化（去首尾空白、转小写、空白/连字符统一为下划线）再查表：
    历史数据的 source_category 存在空格形式（"animals pet supplies"，
    清洗流程曾把下划线替换为空格），与标准下划线形式
    （"animals_pet_supplies"）都能正确映射为中文。

    示例:
        get_cn_category_name("hardware") -> "五金"
        get_cn_category_name("electronics") -> "电子产品"
        get_cn_category_name("animals pet supplies") -> "动物"
        未知分类返回传入值原样
    """
    if not shopify_category or not shopify_category.strip():
        return shopify_category
    key = re.sub(r"[\s\-]+", "_", shopify_category.strip().lower())
    return SHOPIFY_TO_CN_CATEGORY.get(key, shopify_category)


def get_google_category_name(shopify_category: str) -> str:
    """将 Shopify 简化分类名转换为 Google Taxonomy 一级分类名

    示例:
        get_google_category_name("hardware") -> "Hardware"
        get_google_category_name("animals_pet_supplies") -> "Animals & Pet Supplies"
        未知分类则返回首字母大写形式
    """
    return SHOPIFY_TO_GOOGLE_CATEGORY.get(shopify_category, shopify_category.title())


# ── 类目 .txt 文件目录 ───────────────────────────────────
CATEGORY_TXT_DIR = Path(__file__).resolve().parent.parent / "data" / "categories"


# ── 简化名称 -> .txt 文件名 ───────────────────────────────
CATEGORY_TO_TXT: dict[str, str] = {
    "animals_pet_supplies": "Animals & Pet Supplies.txt",
    "apparel_accessories": "Apparel & Accessories.txt",
    "arts_entertainment": "Arts & Entertainment.txt",
    "baby_toddler": "Baby & Toddler.txt",
    "business_industrial": "Business & Industrial.txt",
    "cameras_optics": "Cameras & Optics.txt",
    "electronics": "Electronics.txt",
    "food_beverages_tobacco": "Food, Beverages & Tobacco.txt",
    "furniture": "Furniture.txt",
    "hardware": "Hardware.txt",
    "health_beauty": "Health & Beauty.txt",
    "home_garden": "Home & Garden.txt",
    "luggage_bags": "Luggage & Bags.txt",
    "mature": "Mature.txt",
    "media": "Media.txt",
    "office_supplies": "Office Supplies.txt",
    "religious_ceremonial": "Religious & Ceremonial.txt",
    "software": "Software.txt",
    "sporting_goods": "Sporting Goods.txt",
    "toys_games": "Toys & Games.txt",
    "vehicles_parts": "Vehicles & Parts.txt",
}


def _load_category_lines(category: str) -> list[str]:
    """读取一级类目对应的 .txt 文件，返回非空行列表

    每行格式如 "Electronics > Audio > Audio Components"，
    用 " > " 分隔层级。
    """
    txt_filename = CATEGORY_TO_TXT.get(category)
    if not txt_filename:
        return []
    txt_path = CATEGORY_TXT_DIR / txt_filename
    if not txt_path.exists():
        return []
    with open(txt_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def get_level2_categories(category: str) -> list[str]:
    """获取一级类目下的所有二级分类名称

    读取 .txt 文件，提取含恰好1个 ">" 的行（即二级分类路径），
    返回二级分类名称列表（不含一级前缀）。

    示例:
        get_level2_categories("electronics")
        -> ["Arcade Equipment", "Audio", "Computers", "Networking", ...]
    """
    lines = _load_category_lines(category)
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        parts = [p.strip() for p in line.split(">")]
        if len(parts) == 2:
            level2_name = parts[1]
            if level2_name and level2_name not in seen:
                seen.add(level2_name)
                result.append(level2_name)
    return result


def generate_keywords_from_subcategory(
    category: str,
    subcategory: str = "",
    count: int = 10,
) -> dict:
    """从指定一级类目的二级分类下随机生成搜索关键词

    读取 .txt 文件，找到二级分类（subcategory 为空则随机选一个），
    提取该二级分类下所有三级和四级分类的末级名称作为候选关键词，
    从中随机挑选 count 个返回。

    随机选择时会跳过没有三级/四级子分类的二级分类。
    若用户指定的 subcategory 无子分类，则返回空关键词列表。

    Args:
        category: 一级类目简化名（如 "electronics"）
        subcategory: 二级分类名（如 "Audio"），为空则随机选择
        count: 需要生成的关键词数量

    Returns:
        {"level2": "Audio", "keywords": ["Headphones", "Speakers", ...]}
    """
    lines = _load_category_lines(category)
    if not lines:
        return {"level2": "", "keywords": []}

    google_name = SHOPIFY_TO_GOOGLE_CATEGORY.get(category, category.title())

    def _collect_candidates(level2_name: str) -> list[str]:
        """收集指定二级分类下的三/四级分类末级名称"""
        prefix = f"{google_name} > {level2_name}"
        result: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if not line.startswith(prefix + " >"):
                continue
            parts = [p.strip() for p in line.split(">")]
            if len(parts) in (3, 4):
                leaf_name = parts[-1]
                if leaf_name and leaf_name not in seen:
                    seen.add(leaf_name)
                    result.append(leaf_name)
        return result

    # 获取所有二级分类
    all_level2 = get_level2_categories(category)
    if not all_level2:
        return {"level2": "", "keywords": []}

    # 确定使用的二级分类
    if subcategory and subcategory in all_level2:
        selected_level2 = subcategory
        candidates = _collect_candidates(selected_level2)
    else:
        # 随机选择：优先选有三级/四级子分类的二级分类
        shuffled = all_level2[:]
        random.shuffle(shuffled)
        selected_level2 = ""
        candidates = []
        for level2 in shuffled:
            cands = _collect_candidates(level2)
            if cands:
                selected_level2 = level2
                candidates = cands
                break
        if not selected_level2:
            # 所有二级分类都无子分类，随机返回一个
            selected_level2 = random.choice(all_level2)
            candidates = []

    if not candidates:
        return {"level2": selected_level2, "keywords": []}

    # 随机挑选 count 个关键词（不足则全部返回）
    actual_count = min(count, len(candidates))
    selected = random.sample(candidates, actual_count)

    return {"level2": selected_level2, "keywords": selected}


# ── 旧类别名 -> 新类别名（用于 MongoDB 集合迁移） ──────────
OLD_TO_NEW_CATEGORY = {
    "hardware": "hardware",
    "vehicles": "vehicles_parts",
    "sports": "sporting_goods",
    "health": "health_beauty",
    "office": "office_supplies",
    "pets": "animals_pet_supplies",
    "business": "business_industrial",
    "baby": "baby_toddler",
    "media": "media",
    "religion": "religious_ceremonial",
    "furniture": "furniture",
    "home-garden": "home_garden",
    "adult": "mature",
    "fashion": "apparel_accessories",
    "toys": "toys_games",
    "electronics": "electronics",
    "cameras": "cameras_optics",
    "bags": "luggage_bags",
    "arts-entertainment": "arts_entertainment",
    "software": "software",
    "food-beverage": "food_beverages_tobacco",
}


# ── 成人类目（Mature）判定 ──────────────────────────────
# 用途：商品清洗的违禁词过滤对成人类目跳过"成人关键词"——成人用品的标题/描述
# 天然包含 lingerie / vibrator / adult toy 等词，这些词只在非成人类目下才判违禁。
ADULT_CATEGORY_ALIASES = {
    "mature",  # 标准简化名（SHOPIFY_CATEGORIES）
    "adult",   # OLD_TO_NEW_CATEGORY 中的旧别名
    "成人",     # 中文一级分类名（SHOPIFY_TO_CN_CATEGORY）
    "772",     # CATEGORY_ID_MAP 中 Mature 的 Google Taxonomy ID
    "13",      # 旧订单口径的成人类目编号（对应 order_db 的 "13": "成人"）
}


def is_adult_category(category) -> bool:
    """判断一级分类是否属于成人（Mature）类目

    输入先规范化（去首尾空白、转小写、空白/连字符统一为下划线）再判定，
    因此 "Mature"、"adult"、"成人"、"mature " 以及历史别名都能识别。

    示例:
        is_adult_category("mature") -> True
        is_adult_category("成人") -> True
        is_adult_category("adult") -> True
        is_adult_category("health_beauty") -> False
        is_adult_category(None) -> False
    """
    if category is None:
        return False
    key = re.sub(r"[\s\-]+", "_", str(category).strip().lower())
    if not key:
        return False
    if key in ADULT_CATEGORY_ALIASES:
        return True
    return OLD_TO_NEW_CATEGORY.get(key) == "mature"


# ── 标准二级分类清单（与 ai_classifier.py build_prompt 一致）──────────
# 每个一级分类的合法二级分类名（标准化后的小写+下划线形式）。
# 三处共用唯一真相源：
#   1. ai_classifier.py 的 prompt（人类可读形式，如 "Computers, Phones, Audio"）
#   2. shopify.py 写入时的运行时校验（调用 get_standard_subcategories）
#   3. migrate_collection_names.py 迁移时的标准判断
# 注意：清单需与 ai_classifier.py:397-417 的 prompt 保持同步。
STANDARD_SUBCATEGORIES: dict[str, list[str]] = {
    "apparel_accessories": ["clothing", "shoes", "jewelry", "handbags", "watches", "sunglasses"],
    "electronics": ["computers", "phones", "audio", "cameras", "tvs", "gaming", "accessories"],
    "home_garden": ["furniture", "decor", "kitchen", "bedding", "garden", "tools"],
    "health_beauty": ["skincare", "makeup", "hair", "supplements", "personal_care", "fragrance"],
    "food_beverages_tobacco": ["coffee", "tea", "snacks", "wine", "beer", "grocery", "gourmet"],
    "sporting_goods": ["fitness", "outdoor", "cycling", "yoga", "camping", "hiking", "sports"],
    "baby_toddler": ["clothing", "toys", "nursery", "strollers", "feeding", "maternity"],
    "animals_pet_supplies": ["dog", "cat", "pet_food", "pet_accessories", "pet_care"],
    "toys_games": ["board_games", "action_figures", "puzzles", "educational_toys"],
    "business_industrial": ["office_supplies", "printing", "industrial_equipment"],
    "media": ["books", "movies", "music", "magazines", "digital_content"],
    "arts_entertainment": ["art", "crafts", "party_supplies", "collectibles", "music_instruments"],
    "cameras_optics": ["cameras", "lenses", "binoculars", "photography_accessories"],
    "furniture": ["home_furniture", "office_furniture", "mattresses", "outdoor_furniture"],
    "hardware": ["tools", "hardware", "building_materials", "plumbing", "electrical"],
    "luggage_bags": ["suitcases", "backpacks", "travel_bags", "wallets"],
    "office_supplies": ["paper", "pens", "office_equipment", "stationery"],
    "software": ["business_software", "education_software", "entertainment_software"],
    "vehicles_parts": ["car_parts", "motorcycle_parts", "auto_accessories", "tires"],
    "mature": ["adult_toys", "lingerie", "adult_content", "adult_novelties", "adult_gifts"],
    "religious_ceremonial": ["incense_candles", "ritual_supplies", "worship_items", "ceremonial_objects", "religious_texts"],
}


# ── 类目补充关键词（taxonomy 之外、业内明确属于该类目的商品）──────
#
# 背景：Google Product Taxonomy 的宗教类目只收录到 Prayer Beads / Prayer Cards /
# Religious Altars / Religious Veils / Tarot Cards 等少量条目，像十字架项链、
# 念珠手串、圣牌这类「宗教 + 珠宝」的常见商品完全没有收录，导致精准类目筛选
# 时这些 collection 匹配不到宗教类目，被漏掉或归到珠宝类目。
#
# 为什么不能直接把这些词写进 data/categories/*.txt：
#   taxonomy 的匹配是「单词交集」——写入 "Cross Necklace" 会让通用词 necklace
#   变成宗教类目的关键词，于是所有项链 collection 都会被误判成宗教类目。
# 所以这里用「短语级」匹配：短语里的每个词都要在标题中整词出现（词序无关），
# "cross necklace" 只命中同时含 cross 和 necklace 的标题，单独 "Gold Necklaces"
# 或 "Cross Training Shoes" 都不会命中。
#
# 两个消费方共用本表：
#   1. data_scraper/category_matcher.match_title（精准类目筛选）
#   2. utils/site_classifier（站点主营类目判断）
# 短语分词：非字母数字一律视为分隔符（"Cross & Rosary Necklaces" -> cross/rosary/necklaces）
_EXTRA_PHRASE_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _singularize_word(word: str) -> str:
    """轻量去复数（与 utils.site_classifier.normalize_text 同规则）

    让词表只需写一种形式即可命中单复数标题（"Kippahs"/"Kippah"、
    "Rosaries"/"Rosary"、"Crosses"/"Cross"）。以 ss 结尾的词（cross/glass）
    不去 s。
    """
    if len(word) > 4:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith(("ses", "xes", "zes", "ches", "shes")):
            return word[:-2]
        if word.endswith("es"):
            return word[:-1]
        if word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
    return word

CATEGORY_EXTRA_PHRASES: dict[str, tuple[str, ...]] = {
    "religious_ceremonial": (
        # ── 宗教专有词：单独出现即可判定（非宗教语境几乎不会使用）──
        "rosary", "rosaries",
        "crucifix", "crucifixes",
        "mezuzah", "mezuza", "menorah", "menorahs",
        "kippah", "kippot", "yarmulke", "yarmulkes", "tallit", "tallis",
        "chalice", "chalices", "hymnal", "hymnals", "siddur",
        "quran", "koran", "talmud", "torah", "sutra", "mala beads",
        "communion", "communion cups", "communion wafers",
        "prayer beads", "prayer cards", "prayer shawl", "prayer shawls",
        "prayer mat", "prayer mats", "prayer rug", "prayer rugs",
        "incense burner", "incense burners", "incense holder", "incense holders",
        "incense sticks", "incense cones",
        "smudge stick", "smudge sticks",
        "bible", "bibles", "bible cover", "bible covers", "bible case",
        "bible study", "bible journal", "scripture", "scriptures",
        "devotional", "devotionals",
        "christian", "catholic", "jewish", "judaica", "muslim", "islamic",
        "buddhist", "hindu", "sikh",
        "christening", "baptism", "first communion", "confirmation gift",
        "confirmation gifts",
        # ── 宗教珠宝/饰品：与首饰词共现才判定，避免 "cross training" 之类误判 ──
        "cross necklace", "cross necklaces", "cross pendant", "cross pendants",
        "cross bracelet", "cross bracelets", "cross earring", "cross earrings",
        "cross ring", "cross rings", "cross charm", "cross charms",
        "cross jewelry", "cross jewellery",
        "crucifix necklace", "crucifix pendant", "crucifix bracelet",
        "crucifix earrings", "crucifix ring",
        "rosary necklace", "rosary bracelet", "rosary bracelets",
        "rosary ring", "rosary rings", "rosary pouch", "rosary case",
        "religious necklace", "religious necklaces", "religious pendant",
        "religious pendants", "religious bracelet", "religious bracelets",
        "religious jewelry", "religious jewellery", "religious charm",
        "religious charms", "religious ring", "religious rings",
        "religious earrings", "religious gift", "religious gifts",
        "saint pendant", "saint pendants", "saint medal", "saint medals",
        "saint bracelet", "saint necklace", "patron saint",
        "bible verse necklace", "bible verse bracelet", "bible verse ring",
        "bible verse jewelry", "faith necklace", "faith bracelet", "faith jewelry",
    ),
}


def match_category_extra_phrases(category: str, text: str) -> bool:
    """类目补充关键词短语匹配（见 CATEGORY_EXTRA_PHRASES）

    匹配规则：短语里的每个词都要在 text 中整词出现（词序无关，忽略大小写
    与标点）。整词比较保证 "cross" 不会命中 "crossbody"，短语共现保证
    "necklace" 单独出现不会命中宗教类目。

    Args:
        category: 一级分类简化名（如 "religious_ceremonial"）
        text: 待匹配文本（collection 标题 / 站点描述等）

    Returns:
        是否命中该类目的任一补充短语
    """
    phrases = CATEGORY_EXTRA_PHRASES.get(category)
    if not phrases or not text:
        return False
    # 标题与词表都做同样的去复数，词表只需写一种形式
    words = {_singularize_word(w)
             for w in _EXTRA_PHRASE_SPLIT_RE.split(str(text).lower()) if w}
    if not words:
        return False
    for phrase in phrases:
        parts = [_singularize_word(p) for p in phrase.split()]
        if parts and all(p in words for p in parts):
            return True
    return False


def get_standard_subcategories(category: str) -> set[str]:
    """获取一级分类的标准子分类集合（含 DEFAULT_SUBCATEGORY='other'）

    用于写入时校验：非标准 subcategory 一律归入 other，防止碎片集合。

    Args:
        category: 一级分类简化名（如 "electronics"）

    Returns:
        标准子分类名集合（小写+下划线形式），始终包含 "other"
    """
    return set(STANDARD_SUBCATEGORIES.get(category, [])) | {DEFAULT_SUBCATEGORY}
