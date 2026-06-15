"""Google Product Taxonomy 一级分类定义

基于 Google Product Taxonomy Version: 2021-09-21
所有类目名称均使用简化英文格式（小写 + 下划线），用于 MongoDB 集合命名和 Web UI。
"""

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

# ── 旧类别名 → 新类别名（用于 MongoDB 集合迁移） ──────────
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
