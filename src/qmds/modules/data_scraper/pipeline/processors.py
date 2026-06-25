"""商品数据处理器"""

import re
from pathlib import Path
from typing import Any
from functools import lru_cache

from qmds.modules.data_scraper.models.schemas import Product

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

CATEGORY_TXT_DIR = Path(__file__).resolve().parents[2] / "data" / "categories"
CATEGORY_FILES = [
    "Animals & Pet Supplies.txt", "Apparel & Accessories.txt", "Arts & Entertainment.txt",
    "Baby & Toddler.txt", "Business & Industrial.txt", "Cameras & Optics.txt",
    "Electronics.txt", "Food, Beverages & Tobacco.txt", "Furniture.txt",
    "Hardware.txt", "Health & Beauty.txt", "Home & Garden.txt",
    "Luggage & Bags.txt", "Mature.txt", "Media.txt",
    "Office Supplies.txt", "Religious & Ceremonial.txt", "Software.txt",
    "Sporting Goods.txt", "Toys & Games.txt", "Vehicles & Parts.txt",
]

IRREGULAR_NOUNS = {
    "women": "woman", "men": "man", "children": "child",
    "feet": "foot", "teeth": "tooth", "mice": "mouse",
    "geese": "goose", "oxen": "ox", "knives": "knife",
    "lives": "life", "wives": "wife", "leaves": "leaf",
    "shelves": "shelf", "halves": "half", "calves": "calf",
    "scarves": "scarf", "dwarves": "dwarf", "hooves": "hoof",
    "elves": "elf", "berries": "berry", "cherries": "cherry",
    "strawberries": "strawberry", "babies": "baby",
    "countries": "country", "cities": "city", "families": "family",
    "parties": "party", "stories": "story", "factories": "factory",
    "companies": "company", "batteries": "battery", "activities": "activity",
    "qualities": "quality", "quantities": "quantity", "utilities": "utility",
    "accessories": "accessory", "categories": "category",
    "galleries": "gallery", "libraries": "library", "machinery": "machinery",
    "jewelry": "jewelry", "footwear": "footwear", "outerwear": "outerwear",
    "underwear": "underwear", "sleepwear": "sleepwear", "activewear": "activewear",
    "swimwear": "swimwear", "sportswear": "sportswear", "workwear": "workwear",
}


@lru_cache(maxsize=1)
def _load_all_taxonomy_names() -> set[str]:
    """加载所有 Google Product Taxonomy 分类名称（小写）"""
    names: set[str] = set()
    for filename in CATEGORY_FILES:
        txt_path = CATEGORY_TXT_DIR / filename
        if not txt_path.exists():
            continue
        with open(txt_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(">")]
                for part in parts:
                    if part:
                        names.add(part.lower())
    return names


def _to_singular(word: str) -> str:
    """将单词转换为单数形式"""
    lower = word.lower()
    if lower in IRREGULAR_NOUNS:
        return IRREGULAR_NOUNS[lower]
    if len(lower) <= 2:
        return lower
    if lower.endswith("ies") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith("ves") and len(lower) > 4:
        return lower[:-3] + "f"
    if lower.endswith("ses") or lower.endswith("xes") or lower.endswith("zes") or \
       lower.endswith("ches") or lower.endswith("shes"):
        return lower[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and not lower.endswith("us") and \
       not lower.endswith("is") and len(lower) > 3:
        return lower[:-1]
    if lower.endswith("ing") and len(lower) > 5:
        base = lower[:-3]
        if len(base) >= 2 and base[-1] == base[-2]:
            return base[:-1]
        return base
    if lower.endswith("ied") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith("ed") and len(lower) > 4:
        return lower[:-2]
    if lower.endswith("es") and len(lower) > 3:
        return lower[:-1]
    return lower


def normalize_category(product_type: str) -> str:
    """标准化分类名称：清洗、单复数统一并尝试匹配 Google Product Taxonomy"""
    if not product_type:
        return ""
    cleaned = product_type.strip()
    cleaned = WHITESPACE_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", "")
    if not cleaned:
        return ""
    taxonomy_names = _load_all_taxonomy_names()
    lower_cleaned = cleaned.lower()
    if lower_cleaned in taxonomy_names:
        return cleaned.title()
    words = cleaned.split()
    singular_words = [_to_singular(w) for w in words]
    singular_form = " ".join(singular_words)
    if singular_form.lower() in taxonomy_names:
        return singular_form.title()
    return cleaned


class ProductProcessor:
    """商品数据清洗和标准化"""

    @staticmethod
    def clean_html(text: str) -> str:
        text = HTML_TAG_RE.sub(" ", text)
        text = WHITESPACE_RE.sub(" ", text)
        return text.strip()

    @staticmethod
    def clean_title(title: str) -> str:
        return title.strip().replace("\n", " ")

    @staticmethod
    def clean_description(desc: str) -> str:
        cleaned = ProductProcessor.clean_html(desc)
        return cleaned[:5000] if len(cleaned) > 5000 else cleaned

    @staticmethod
    def clean_category(product: Product) -> None:
        """标准化商品分类字段"""
        product.product_type = normalize_category(product.product_type)
        product.category = normalize_category(product.category)

    @staticmethod
    def deduplicate_by_title(products: list[Product]) -> list[Product]:
        """基于标题去重，保留第一个出现的商品"""
        seen_titles = set()
        unique_products = []
        for p in products:
            normalized_title = p.title.strip().lower()
            if normalized_title and normalized_title not in seen_titles:
                seen_titles.add(normalized_title)
                unique_products.append(p)
        return unique_products

    @staticmethod
    def remove_bulk_image_products(products: list[Product], threshold: int = 20) -> list[Product]:
        """删除使用相同图片链接且商品数超过阈值的商品"""
        from collections import Counter
        image_counter: Counter[str] = Counter()
        for p in products:
            for img in p.images:
                if img:
                    image_counter[img] += 1
        bulk_images = {img for img, count in image_counter.items() if count > threshold}
        if not bulk_images:
            return products
        filtered = [p for p in products if not any(img in bulk_images for img in p.images)]
        return filtered

    @staticmethod
    def process_all(products: list[Product]) -> list[Product]:
        processed = []
        for p in products:
            p.title = ProductProcessor.clean_title(p.title)
            p.body_html = ProductProcessor.clean_description(p.body_html)
            p.tags = [t.strip() for t in p.tags if t.strip()]
            ProductProcessor.clean_category(p)
            processed.append(p)
        processed = ProductProcessor.remove_bulk_image_products(processed)
        return processed
