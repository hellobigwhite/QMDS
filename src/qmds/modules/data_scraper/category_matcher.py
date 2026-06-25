"""类目匹配器：加载 Google Product Taxonomy 分类文件，匹配 collections.json 中的 title"""

from pathlib import Path
from typing import Optional
from functools import lru_cache

from qmds.utils.logger import get_logger

log = get_logger("category_matcher")

CATEGORY_TXT_DIR = Path(__file__).resolve().parents[2] / "data" / "categories"

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


@lru_cache(maxsize=32)
def load_category_names(category: str) -> set[str]:
    """加载类目 .txt 文件，返回所有分类名称集合（小写）

    提取 .txt 文件中每一行的叶子节点名称和完整路径的最后一段。
    例如 "Electronics > Audio > Speakers" 提取出 {"electronics", "audio", "speakers"}
    """
    txt_filename = CATEGORY_TO_TXT.get(category)
    if not txt_filename:
        log.warning(f"未找到类目 {category} 对应的 .txt 文件映射")
        return set()

    txt_path = CATEGORY_TXT_DIR / txt_filename
    if not txt_path.exists():
        log.warning(f"类目文件不存在: {txt_path}")
        return set()

    names: set[str] = set()
    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(">")]
            for part in parts:
                if part:
                    names.add(part.lower())
    log.info(f"类目 {category}: 从 {txt_filename} 加载了 {len(names)} 个分类名称")
    return names


def match_title(category: str, title: str) -> bool:
    """判断 collection title 是否与类目分类匹配（忽略大小写）

    匹配规则：
    1. 完全匹配：title 在分类名称集合中
    2. 单词匹配：title 和分类名称有相同的单词（长度>=3，排除通用词）
    """
    if not title:
        return False
    title_lower = title.strip().lower()
    names = load_category_names(category)
    
    # 排除的通用词
    excluded_words = {"all", "the", "and", "for", "with", "new", "sale", "shop", "best", "top"}
    
    # 完全匹配
    if title_lower in names:
        return True
    
    # 单词匹配：将 title 拆分为单词集合
    title_words = set(title_lower.split())
    # 过滤掉过短和通用词
    title_words = {w for w in title_words if len(w) >= 3 and w not in excluded_words}
    
    if not title_words:
        return False
    
    for name in names:
        # 分类名也要拆分为单词
        name_words = set(name.split())
        name_words = {w for w in name_words if len(w) >= 3 and w not in excluded_words}
        
        if not name_words:
            continue
        
        # 检查是否有交集（单词级别匹配）
        if title_words & name_words:
            return True
    
    return False
