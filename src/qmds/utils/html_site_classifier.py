"""基于HTML内容的Shopify网站分类器

从 shopify_home_html.py 提取核心算法，封装为可复用的类。
支持单个分类和批量Excel分类。
"""

import math
import re
import threading
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

from qmds.utils.logger import get_logger

log = get_logger("html_site_classifier")

# 禁用SSL警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── 常量定义 ──────────────────────────────────────────────

# 标题强信号词
TITLE_SIGNAL_KEYWORDS = {
    "Health & Beauty": [
        "beauty", "cosmetic", "cosmetics", "salon", "spa", "tanning", "skincare",
        "makeup", "barber", "hair", "nail", "nails", "manicure", "pedicure",
        "fragrance", "fragrances", "perfume", "perfumes", "lipstick", "lash", "lashes",
        "skin", "skincare", "derma", "dermatologist"
    ],
    "Sporting Goods": [
        "sport", "sports", "fitness", "gym", "exercise", "workout", "kayak",
        "canoe", "baseball", "basketball", "football", "soccer", "tennis",
        "yoga", "running", "bike", "bicycle", "cycling", "cyclist",
        "fishing", "tackle", "camping", "camp", "tent", "outdoor", "outdoors",
        "skateboard", "skateboarding", "longboard", "wakeboard",
        "surfboard", "surfing", "hiking", "trekking", "climbing",
        "golf", "hockey", "volleyball", "athletic", "athletics",
    ],
    "Animals & Pet Supplies": ["pet", "pets", "dog", "cat", "aquarium", "veterinary"],
    "Electronics": ["electronics", "gadget", "phone", "laptop", "computer", "tech"],
    "Arts & Entertainment": ["art", "music", "instrument", "craft", "hobby", "concert", "theater"],
    "Apparel & Accessories": ["clothing", "apparel", "fashion", "jewelry", "accessories"],
    "Home & Garden": ["home", "garden", "furniture", "decor", "kitchen"],
    "Toys & Games": ["toy", "toys", "game", "games", "puzzle", "hobby"],
    "Food, Beverages & Tobacco": ["food", "coffee", "tea", "wine", "beer", "snack"],
    "Furniture": ["furniture", "mattress"],
    "Baby & Toddler": ["baby", "toddler", "infant", "maternity"],
    "Cameras & Optics": ["camera", "photography", "lens", "drone"],
    "Luggage & Bags": ["luggage", "backpack", "handbag", "suitcase"],
    "Vehicles & Parts": ["car", "auto", "automotive", "motorcycle", "tire"],
    "Business & Industrial": ["industrial", "wholesale", "machinery", "manufacturing"],
    "Hardware": ["hardware", "tool", "tools"],
    "Media": ["book", "books", "magazine", "dvd", "vinyl"],
    "Office Supplies": ["office", "stationery"],
    "Religious & Ceremonial": ["religious", "bible", "wedding", "church"],
    "Software": ["software", "app", "saas", "digital"],
}

# 上下文互斥规则
CONTEXT_MUTEX_RULES = {
    ("beauty", "salon", "spa", "tanning", "cosmetic", "cosmetics", "barber", "nail", "hair", "skin", "skincare"): [
        ("Furniture", ["chair", "chairs", "bed", "beds", "table", "tables", "cabinet", "shelf", "stool", "seat"]),
        ("Home & Garden", ["lamp", "lamps", "light", "lighting", "mirror", "towel", "trolley", "cart"]),
        ("Apparel & Accessories", ["cap", "hat", "hats", "glove", "gloves", "apron", "jacket", "jackets", "boots"]),
    ],
    ("bike", "bicycle", "cycling", "cyclist", "fishing", "tackle", "camping", "tent",
     "skateboard", "soccer", "football", "baseball", "basketball", "kayak", "canoe",
     "surfboard", "wakeboard", "hockey", "ski", "climbing", "hiking", "outdoor", "sport", "sports", "fitness"): [
        ("Apparel & Accessories", ["shirt", "shirts", "hat", "hats", "jacket", "jackets",
         "bag", "bags", "shoe", "shoes", "hoodie", "sweater", "pants", "jeans", "belt",
         "scarf", "sunglasses", "glove", "gloves", "cap", "socks", "clothing", "apparel", "fashion"]),
        ("Vehicles & Parts", ["tire", "tires", "vehicle", "vehicles"]),
    ]
}

# 品类关键词库
CATEGORY_KEYWORDS = {
    "Arts & Entertainment": [
        "music", "musical", "instrument", "instruments", "guitar", "piano", "drum",
        "trumpet", "saxophone", "violin", "flute", "orchestra", "band", "concert", "vinyl",
        "art", "craft", "hobby", "paint", "drawing", "sketch", "studio",
        "canvas", "brush", "easel", "watercolor", "acrylic", "theater", "dance",
        "sculpture", "pottery", "ceramic", "knitting", "sewing", "fabric", "yarn",
    ],
    "Sporting Goods": [
        "sport", "sports", "fitness", "athletic", "outdoor", "outdoors",
        "helmet", "ball", "dumbbell", "treadmill", "exercise", "workout", "training",
        "bike", "bicycle", "cycling", "cyclist", "ebike", "handlebar", "pedal",
        "camp", "camping", "tent", "tents", "shelter", "sleeping bag", "hammock",
        "fishing", "fish", "rod", "reel", "tackle", "bait", "lure", "hook",
        "skateboard", "skateboarding", "longboard", "wakeboard", "surfboard", "surfing",
        "kayak", "canoe", "paddle", "ski", "snowboard", "climbing", "hiking", "trekking",
        "baseball", "basketball", "football", "soccer", "tennis", "golf", "hockey",
        "running", "yoga",
    ],
    "Apparel & Accessories": [
        "clothing", "apparel", "fashion", "shirt", "dress", "shoe", "shoes",
        "jewelry", "bag", "bags", "hat", "hats", "jacket", "accessories",
        "necklace", "earring", "bracelet", "sunglasses", "scarf", "jeans", "pants",
        "hoodie", "sweater", "coat", "gloves", "belt", "sneaker", "boot", "boots",
        "t-shirt", "top", "blouse", "skirt", "shorts", "swimwear", "bikini",
        "underwear", "sock", "tie", "vest", "suit", "blazer", "legging",
        "watch", "watches", "ring", "wallet", "purse", "backpack", "tote",
    ],
    "Animals & Pet Supplies": [
        "pet", "pets", "dog", "dogs", "cat", "cats", "bird", "fish", "horse",
        "aquarium", "leash", "collar", "grooming", "treats", "toys",
        "rabbit", "hamster", "reptile", "snake", "lizard", "turtle",
        "pet food", "dog food", "cat food", "dog bed", "cat bed", "pet carrier",
        "litter box", "cat litter", "scratching post", "cat tree",
        "pet toy", "dog toy", "cat toy", "pet treat", "dog treat",
        "pet shampoo", "grooming kit", "dog harness", "veterinary",
    ],
    "Electronics": [
        "electronics", "phone", "laptop", "headphone", "charger", "tablet", "smartwatch",
        "speaker", "bluetooth", "gadget", "webcam", "computer", "tv", "monitor",
        "keyboard", "mouse", "printer", "cable", "adapter", "hub", "dock",
        "power bank", "battery", "usb", "hdmi", "ethernet", "router", "modem",
        "storage", "hard drive", "ssd", "flash drive", "memory card", "sd card",
        "processor", "cpu", "gpu", "graphics card", "motherboard",
        "phone case", "screen protector", "smart home", "security camera",
        "gaming", "gaming chair", "drone", "headset", "earbuds", "airpods",
    ],
    "Health & Beauty": [
        "beauty", "salon", "spa", "tanning", "cosmetic", "cosmetics", "skincare", "makeup",
        "barber", "hair", "shampoo", "conditioner", "styling", "gel", "spray",
        "nail", "polish", "manicure", "pedicure", "acrylic", "nail art",
        "salon chair", "beauty chair", "barber chair", "pedicure chair",
        "salon equipment", "beauty equipment", "salon furniture",
        "facial machine", "laser", "steamer", "beauty machine",
        "skincare", "cream", "moisturizer", "serum", "toner", "lotion",
        "foundation", "concealer", "powder", "blush", "eyeshadow", "mascara", "lipstick",
        "perfume", "fragrance", "cologne", "body lotion", "deodorant", "soap",
        "tanning", "tanning bed", "tanning lamp", "tanning lotion",
        "brush", "makeup brush", "sponge", "mirror", "makeup mirror",
        "hair dryer", "flat iron", "curling iron", "clipper", "trimmer", "razor",
    ],
    "Home & Garden": [
        "home", "decor", "decoration", "kitchen", "garden", "gardening", "lighting",
        "curtain", "pillow", "rug", "vase", "lantern", "furniture", "bedding",
        "towel", "sofa", "chair", "table", "cushion", "blanket", "throw",
        "bathroom", "shower", "kitchenware", "cookware", "utensil", "plate", "bowl", "cup", "mug",
        "storage", "basket", "shelf", "organizer", "plant", "pot", "planter", "flower",
        "outdoor", "patio", "garden tool", "hose", "sprinkler",
        "wall art", "frame", "mirror", "clock", "candle", "diffuser",
    ],
    "Toys & Games": [
        "toy", "toys", "game", "games", "puzzle", "lego", "doll", "plush",
        "board game", "action figure", "collectible", "card game", "pokemon",
        "rc car", "remote control", "drone", "model", "model kit",
        "building block", "stem toy", "educational toy", "play kitchen",
        "video game", "nintendo", "playstation", "xbox", "gaming", "controller",
        "bicycle", "scooter", "skateboard", "kite", "slime",
    ],
    "Food, Beverages & Tobacco": [
        "food", "coffee", "tea", "snack", "chocolate", "candy",
        "beverage", "protein", "organic", "vitamin", "supplement",
        "wine", "beer", "juice", "soda", "drink", "grocery",
        "baking", "spice", "herb", "seasoning", "sauce", "oil", "honey",
        "cookie", "cake", "bread", "cereal", "granola", "nut", "nuts",
        "tobacco", "cigarette", "cigar", "vape", "hookah",
    ],
    "Furniture": [
        "furniture", "chair", "table", "sofa", "couch", "bed", "desk",
        "cabinet", "shelf", "mattress", "dresser", "nightstand", "bench", "stool",
        "bookcase", "wardrobe", "drawer", "tv stand", "coffee table",
        "dining table", "dining chair", "bar stool", "recliner", "office chair",
        "outdoor furniture", "patio furniture", "hammock", "picnic table",
    ],
    "Baby & Toddler": [
        "baby", "toddler", "infant", "newborn", "diaper", "stroller", "crib",
        "pacifier", "onesie", "bibs", "baby food", "baby toy", "baby bottle",
        "baby carrier", "diaper bag", "baby monitor", "baby gate", "playpen",
        "high chair", "baby bath", "baby shampoo", "nursing", "breast pump",
        "teether", "rattle", "baby clothes", "maternity",
    ],
    "Cameras & Optics": [
        "camera", "lens", "photo", "photography", "tripod", "binocular", "drone",
        "dslr", "mirrorless", "gopro", "action camera", "polaroid", "film camera",
        "telescope", "microscope", "night vision", "webcam", "gimbal", "stabilizer",
        "camera bag", "memory card", "battery", "flash", "lightbox", "reflector",
    ],
    "Luggage & Bags": [
        "luggage", "backpack", "handbag", "suitcase", "tote", "duffel", "wallet",
        "briefcase", "travel bag", "carry-on", "garment bag", "weekender",
        "laptop bag", "messenger bag", "crossbody", "shoulder bag",
        "beach bag", "gym bag", "fanny pack", "belt bag", "sling bag",
        "packing cube", "toiletry bag", "luggage tag",
    ],
    "Vehicles & Parts": [
        "car", "auto", "automotive", "motorcycle", "tire", "engine", "headlight",
        "bumper", "scooter", "atv", "rv", "boat", "marine",
        "auto part", "car part", "rim", "wheel", "brake", "suspension",
        "exhaust", "muffler", "spark plug", "battery", "alternator",
        "radiator", "clutch", "transmission", "windshield", "wiper",
        "car cover", "floor mat", "seat cover", "steering wheel",
        "motorcycle helmet", "riding gear",
    ],
    "Business & Industrial": [
        "industrial", "wholesale", "machinery", "manufacturing", "factory",
        "warehouse", "logistics", "shipping", "freight", "packaging",
        "forklift", "conveyor", "pump", "valve", "compressor", "generator",
        "safety", "ppe", "hard hat", "safety glasses", "respirator",
        "janitorial", "cleaning", "chemical", "adhesive", "tape",
        "office equipment", "cash register", "barcode", "label maker",
    ],
    "Hardware": [
        "hardware", "tool", "tools", "power tool", "drill", "saw",
        "screw", "lock", "hammer", "wrench", "bolt", "toolbox",
        "screwdriver", "pliers", "tape measure", "level", "utility knife",
        "ladder", "workbench", "tool chest", "socket", "ratchet",
        "grinder", "sander", "router", "jigsaw", "circular saw",
        "air compressor", "nail gun", "glue gun", "welder", "flashlight",
        "painting", "paint brush", "sandpaper", "caulk", "duct tape",
    ],
    "Media": [
        "book", "books", "novel", "magazine", "dvd", "cd", "vinyl",
        "ebook", "comic", "movie", "film", "album", "audiobook",
        "paperback", "hardcover", "textbook", "cookbook",
        "record", "lp", "blu-ray", "streaming", "poster",
    ],
    "Office Supplies": [
        "office", "stationery", "pen", "paper", "printer", "notebook",
        "folder", "stapler", "envelope", "binder", "label", "sticky note",
        "highlighter", "marker", "whiteboard", "calculator", "desk lamp",
        "paper clip", "tape dispenser", "glue", "pencil", "ruler",
        "file cabinet", "desk organizer", "shredder", "laminator",
    ],
    "Religious & Ceremonial": [
        "religious", "bible", "prayer", "cross", "wedding", "church",
        "candle", "incense", "crucifix", "statue", "angel",
        "christian", "catholic", "jewish", "islamic", "buddhist",
        "wedding decoration", "bridal", "ceremony",
    ],
    "Software": [
        "software", "app", "apps", "game", "download", "digital",
        "license", "plugin", "template", "subscription", "saas", "cloud",
        "theme", "wordpress", "shopify", "extension", "addon",
        "antivirus", "vpn", "office suite", "design software",
        "development tool", "database", "hosting", "domain",
    ],
}

# 核心词配置（权重更高）
CORE_KEYWORDS = {
    "Arts & Entertainment": ["music", "instrument", "art", "craft", "studio", "gallery", "concert", "theater"],
    "Sporting Goods": ["sport", "bike", "bicycle", "fishing", "camping", "skateboard", "kayak", "fitness", "hiking"],
    "Apparel & Accessories": ["clothing", "apparel", "fashion", "jewelry", "shoe", "bag", "hat"],
    "Animals & Pet Supplies": ["pet", "dog", "cat", "aquarium", "veterinary", "grooming"],
    "Electronics": ["electronics", "phone", "laptop", "computer", "headphone", "gadget", "charger"],
    "Health & Beauty": ["beauty", "salon", "spa", "makeup", "skincare", "cosmetics", "hair", "nail", "tanning"],
    "Home & Garden": ["home", "garden", "decor", "kitchen", "furniture", "lighting"],
    "Toys & Games": ["toy", "game", "puzzle", "lego", "doll", "board game"],
    "Food, Beverages & Tobacco": ["food", "coffee", "tea", "beverage", "snack", "wine", "beer"],
    "Furniture": ["furniture", "chair", "sofa", "table", "bed", "desk", "mattress"],
    "Baby & Toddler": ["baby", "toddler", "stroller", "crib", "diaper", "maternity"],
    "Cameras & Optics": ["camera", "lens", "photography", "drone", "tripod"],
    "Luggage & Bags": ["luggage", "backpack", "handbag", "suitcase", "tote", "travel bag"],
    "Vehicles & Parts": ["car", "auto", "motorcycle", "tire", "engine", "vehicle"],
    "Business & Industrial": ["industrial", "wholesale", "machinery", "manufacturing", "warehouse"],
    "Hardware": ["hardware", "tool", "drill", "saw", "hammer", "wrench"],
    "Media": ["book", "magazine", "vinyl", "dvd", "comic", "ebook"],
    "Office Supplies": ["office", "stationery", "pen", "paper", "printer", "notebook"],
    "Religious & Ceremonial": ["religious", "bible", "wedding", "church", "prayer"],
    "Software": ["software", "app", "saas", "digital", "plugin", "license"],
}


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class HTMLClassificationResult:
    """HTML网站分类结果"""
    url: str
    primary_category: str  # 主营类目
    confidence: float  # 置信度 (0-100)
    vertical_type: str  # 垂直类型
    is_shopify: bool  # 是否Shopify
    primary_score: float  # 分类得分
    title: str = ""  # 网站标题
    error: Optional[str] = None  # 错误信息
    all_categories: list = field(default_factory=list)  # 所有匹配的分类


# ── 辅助函数 ──────────────────────────────────────────────

def is_english_text(text: str, threshold: float = 0.8) -> bool:
    """检测文本是否为英文"""
    if not text or len(text.strip()) < 20:
        return True
    title_match = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.DOTALL)
    if title_match:
        title_text = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
        if len(title_text) >= 5:
            ascii_count = sum(1 for c in title_text if 32 <= ord(c) <= 126)
            if ascii_count / len(title_text) >= threshold:
                return True
    text_clean = re.sub(r'<[^>]+>', ' ', text)[:5000]
    text_clean = re.sub(r'\s+', ' ', text_clean).strip()
    if len(text_clean) < 20:
        return True
    ascii_count = sum(1 for c in text_clean if 32 <= ord(c) <= 126)
    return (ascii_count / len(text_clean)) >= threshold


def calculate_position_weight(position: int, total_length: int) -> float:
    """计算位置权重（越早出现权重越高）"""
    if total_length == 0:
        return 0.5
    normalized_pos = position / total_length
    weight = math.exp(-normalized_pos * 2.5)
    return max(0.3, min(1.0, weight))


def calculate_category_score(text: str, category_keywords: list, core_keywords_set: set, source_type: str = "title") -> tuple:
    """计算某个分类在文本中的得分"""
    if not text:
        return 0, []

    text_lower = text.lower()
    total_length = len(text_lower)
    total_score = 0
    matched_keywords = []
    matched_positions = set()

    for keyword in category_keywords:
        kw_lower = keyword.lower()
        if kw_lower.endswith('s'):
            patterns = [r'\b' + re.escape(kw_lower) + r'\b']
        else:
            patterns = [r'\b' + re.escape(kw_lower) + r'\b']
            if len(kw_lower) >= 3:
                patterns.append(r'\b' + re.escape(kw_lower) + r's\b')

        keyword_matched = False
        for pattern in patterns:
            for match in re.finditer(pattern, text_lower):
                pos = match.start()
                if pos in matched_positions:
                    continue
                matched_positions.add(pos)
                pos_weight = calculate_position_weight(pos, total_length)

                base_score = 1.0
                if keyword in core_keywords_set:
                    base_score += 1.0

                if source_type == "title":
                    source_weight = 1.5
                elif source_type == "collections":
                    source_weight = 0.9
                else:
                    source_weight = 1.0

                score = base_score * pos_weight * source_weight
                total_score += score
                if not keyword_matched:
                    matched_keywords.append(keyword)
                    keyword_matched = True

    return total_score, list(set(matched_keywords))


def extract_collection_links(soup) -> list:
    """提取collection链接文本"""
    collection_texts = []
    for li in soup.find_all('li'):
        for a in li.find_all('a', href=True):
            href = a['href'].lower()
            if '/collections/' in href or '/collection/' in href:
                text = a.get_text(strip=True)
                if text and len(text) > 3:
                    collection_texts.append(text)
    return collection_texts


def detect_context_signals(text_sources: list) -> set:
    """检测上下文信号词"""
    combined_text = ' '.join(text for _, text in text_sources).lower()
    signals = set()
    for signal_group in CONTEXT_MUTEX_RULES:
        for signal_word in signal_group:
            if re.search(r'\b' + re.escape(signal_word) + r's?\b', combined_text):
                signals.add(signal_word)
    return signals


def apply_title_signal_boost(title_text: str, category_stats: dict) -> dict:
    """标题信号提升"""
    if not title_text:
        return category_stats

    title_lower = title_text.lower()
    signal_hits = {}
    for category, signals in TITLE_SIGNAL_KEYWORDS.items():
        hit_count = 0
        for signal in signals:
            if re.search(r'\b' + re.escape(signal) + r'\b', title_lower):
                hit_count += 1
        if hit_count > 0:
            signal_hits[category] = hit_count

    if not signal_hits:
        return category_stats

    max_hits = max(signal_hits.values())
    top_signal_cats = [cat for cat, hits in signal_hits.items() if hits == max_hits]

    boost_amount = 10.0
    for cat in top_signal_cats:
        if cat in category_stats:
            category_stats[cat]["total_score"] += boost_amount
            category_stats[cat]["title_boost"] = True
        else:
            category_stats[cat] = {
                "total_score": boost_amount,
                "matched_keywords": set(),
                "unique_count": 0,
                "source_breakdown": {},
                "title_boost": True
            }

    boosted = set(top_signal_cats)
    for category in list(category_stats.keys()):
        if category in boosted:
            continue
        if category_stats[category].get("title_boost"):
            continue
        breakdown = category_stats[category].get("source_breakdown", {})
        if "title" in breakdown:
            title_score = breakdown["title"].get("score", 0)
            if title_score > 0:
                category_stats[category]["total_score"] -= title_score * 0.7

    return category_stats


def apply_context_mutex(category_stats: dict, context_signals: set) -> dict:
    """上下文互斥削减"""
    if not context_signals:
        return category_stats

    for signal_group, affected_rules in CONTEXT_MUTEX_RULES.items():
        if not any(sig in context_signals for sig in signal_group):
            continue
        for affected_cat, affected_keywords in affected_rules:
            if affected_cat not in category_stats:
                continue
            matched = category_stats[affected_cat].get("matched_keywords", set())
            penalty_score = 0
            for kw in matched:
                if kw in affected_keywords:
                    penalty_score += 5.0
            if penalty_score > 0:
                category_stats[affected_cat]["total_score"] = max(0, category_stats[affected_cat]["total_score"] - penalty_score)

    return category_stats


def analyze_category_keywords_presence(text_sources: list, title_text: str = "") -> dict:
    """全站关键词分析"""
    category_stats = {}

    for source_type, text in text_sources:
        for category, keywords in CATEGORY_KEYWORDS.items():
            core_set = set(CORE_KEYWORDS.get(category, []))
            score, matched = calculate_category_score(text, keywords, core_set, source_type=source_type)

            if score > 0:
                if category not in category_stats:
                    category_stats[category] = {
                        "total_score": 0,
                        "matched_keywords": set(),
                        "source_breakdown": {}
                    }
                category_stats[category]["total_score"] += score
                category_stats[category]["matched_keywords"].update(matched)
                category_stats[category]["source_breakdown"][source_type] = {
                    "score": round(score, 2),
                    "keywords": matched
                }

    for cat in category_stats:
        category_stats[cat]["unique_count"] = len(category_stats[cat]["matched_keywords"])

    category_stats = apply_title_signal_boost(title_text, category_stats)

    for cat in category_stats:
        sig_hits = category_stats[cat].get("title_signal_hits", 0)
        if sig_hits > 0:
            category_stats[cat]["unique_count"] = max(
                category_stats[cat].get("unique_count", 0),
                sig_hits
            )

    context_signals = detect_context_signals(text_sources)
    if context_signals:
        category_stats = apply_context_mutex(category_stats, context_signals)

    return category_stats


def determine_vertical_type(primary_category: str, primary_score: float, all_categories: list) -> dict:
    """判断网站垂直类型"""
    if not all_categories:
        return {"vertical_type": "unknown", "description": "无法判定", "primary_ratio": 0, "score_gap": None}

    total_score = sum(cat['score'] for cat in all_categories)
    primary_ratio = (primary_score / total_score * 100) if total_score > 0 else 0

    second_score = all_categories[1]['score'] if len(all_categories) > 1 else 0
    score_gap = primary_score / second_score if second_score > 0 else float('inf')

    if score_gap >= 1.5 and primary_score >= 3 and primary_ratio >= 30:
        return {"vertical_type": "vertical", "description": f"垂直站，主营{primary_category}", "primary_ratio": round(primary_ratio, 1), "score_gap": round(score_gap, 1) if score_gap != float('inf') else None}
    elif primary_ratio == 100:
        return {"vertical_type": "strong_vertical", "description": f"强垂直站，高度专注于{primary_category}", "primary_ratio": 100.0, "score_gap": None}
    elif primary_ratio >= 60 and primary_score >= 0.4:
        return {"vertical_type": "vertical", "description": f"垂直站，主营{primary_category}", "primary_ratio": round(primary_ratio, 1), "score_gap": round(score_gap, 1) if score_gap != float('inf') else None}
    elif primary_ratio >= 40 and primary_score >= 0.3:
        return {"vertical_type": "semi_vertical", "description": f"偏垂直站，主要经营{primary_category}", "primary_ratio": round(primary_ratio, 1), "score_gap": round(score_gap, 1) if score_gap != float('inf') else None}
    elif primary_ratio >= 20:
        return {"vertical_type": "general", "description": "综合站，经营多个品类", "primary_ratio": round(primary_ratio, 1), "score_gap": None}
    elif primary_score > 0:
        return {"vertical_type": "unclear", "description": "信息不足，无法明确判定", "primary_ratio": round(primary_ratio, 1), "score_gap": None}
    else:
        return {"vertical_type": "unknown", "description": "未检测到品类信息", "primary_ratio": 0, "score_gap": None}


# ── 分类器类 ──────────────────────────────────────────────

class HTMLSiteClassifier:
    """基于HTML内容的Shopify网站分类器"""

    def __init__(self, use_proxy: bool = True):
        self.use_proxy = use_proxy
        self._proxy_manager = None
        self._headers_list = [
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36'},
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0'},
            {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'}
        ]
        if use_proxy:
            try:
                from qmds.utils.proxy_manager import ProxyManager
                self._proxy_manager = ProxyManager.from_settings()
                log.info(f"已加载 {self._proxy_manager.total_count} 个代理，可用: {self._proxy_manager.available_count}")
            except Exception as e:
                log.warning(f"加载代理失败: {e}")

    def _get_random_proxy(self) -> Optional[dict]:
        """获取随机代理，返回 proxy_dict 格式 {"http": ..., "https": ...}"""
        if self._proxy_manager:
            return self._proxy_manager.get_proxy()
        return None

    def _mark_proxy_dead(self, proxy_dict: Optional[dict]):
        """标记代理为不可用"""
        if proxy_dict and self._proxy_manager:
            self._proxy_manager.mark_bad(proxy_dict)

    def _fetch_html(self, url: str) -> Optional[str]:
        """获取网页HTML"""
        def _try_once(use_proxy: bool):
            headers = random.choice(self._headers_list)
            proxies = None
            proxy_dict = None
            if use_proxy and self._proxy_manager:
                proxy_dict = self._get_random_proxy()
                if proxy_dict:
                    proxies = proxy_dict
            try:
                resp = requests.get(url, headers=headers, proxies=proxies, timeout=(3, 6), verify=False)
                resp.raise_for_status()
                html = resp.text
                if not html or len(html) < 500:
                    return None
                if not is_english_text(html[:2000]):
                    return "__NON_ENGLISH__"
                return html
            except Exception:
                if proxy_dict:
                    self._mark_proxy_dead(proxy_dict)
                return None

        if self.use_proxy and self._proxy_manager:
            with ThreadPoolExecutor(max_workers=2) as ex:
                dir_fut = ex.submit(_try_once, False)
                prox_fut = ex.submit(_try_once, True)
                for f in as_completed([dir_fut, prox_fut]):
                    res = f.result()
                    if res is not None:
                        return res
                return None

        return _try_once(False)

    def _is_cf_page(self, html: str) -> bool:
        """检测是否为Cloudflare验证页面"""
        cf_patterns = ["__cf_chl_f_tm", "__cf_chl_opt", "cf_challenge", "cf-browser-verification"]
        for pattern in cf_patterns:
            if pattern.lower() in html.lower():
                return True
        return False

    def classify(self, url: str) -> HTMLClassificationResult:
        """分类单个网站"""
        # 标准化URL
        if not url.startswith('http'):
            url = 'https://' + url

        try:
            html = self._fetch_html(url)
            if html == "__NON_ENGLISH__":
                return HTMLClassificationResult(url=url, primary_category="Non-English", confidence=0, vertical_type="unknown", is_shopify=False, primary_score=0, error="非英文站点，已过滤")
            if not html:
                return HTMLClassificationResult(url=url, primary_category="Error", confidence=0, vertical_type="error", is_shopify=False, primary_score=0, error="无法获取页面")
            if self._is_cf_page(html):
                return HTMLClassificationResult(url=url, primary_category="Error", confidence=0, vertical_type="error", is_shopify=False, primary_score=0, error="页面被Cloudflare拦截")

            soup = BeautifulSoup(html, 'html.parser')
            title_text = soup.title.get_text(strip=True) if soup.title else ""
            meta_desc = soup.find('meta', attrs={'name': re.compile('description', re.I)})
            desc_text = meta_desc.get('content', '') if meta_desc else ""
            collection_texts = extract_collection_links(soup)
            collection_combined = ' '.join(collection_texts)

            text_sources = []
            if title_text:
                text_sources.append(("title", title_text))
            if desc_text:
                text_sources.append(("description", desc_text))
            if collection_combined:
                text_sources.append(("collections", collection_combined))

            if text_sources:
                stats = analyze_category_keywords_presence(text_sources, title_text)
                if stats:
                    sorted_cats = sorted(stats.items(), key=lambda x: x[1]["total_score"], reverse=True)
                    primary_category = sorted_cats[0][0]
                    top_score = sorted_cats[0][1]["total_score"]
                    top_unique = sorted_cats[0][1]["unique_count"]
                    second_score = sorted_cats[1][1]["total_score"] if len(sorted_cats) > 1 else 0
                    second_unique = sorted_cats[1][1]["unique_count"] if len(sorted_cats) > 1 else 0

                    score_ratio = top_score / (top_score + second_score + 0.1) if top_score > 0 else 0
                    unique_ratio = top_unique / (top_unique + second_unique + 0.1) if top_unique > 0 else 0
                    confidence = min(98, 40 + score_ratio * 35 + unique_ratio * 25)
                    if top_score < 1.0:
                        confidence *= max(0.3, top_score)
                    elif top_score < 2.0:
                        confidence *= 0.85

                    all_categories = []
                    for cat, cat_stats in sorted_cats[:5]:
                        all_categories.append({
                            'category': cat,
                            'score': round(cat_stats['total_score'], 2),
                            'unique_keywords': cat_stats['unique_count'],
                            'matched_keywords': list(cat_stats['matched_keywords'])[:8]
                        })

                    vertical_result = determine_vertical_type(primary_category, top_score, all_categories)
                    is_shopify = any(ind in html.lower() for ind in ["cdn.shopify", "shopify", "myshopify.com"])

                    return HTMLClassificationResult(
                        url=url,
                        primary_category=vertical_result.get("final_category", primary_category),
                        confidence=round(confidence, 1),
                        vertical_type=vertical_result["vertical_type"],
                        is_shopify=is_shopify,
                        primary_score=round(top_score, 2),
                        title=title_text[:100],
                        all_categories=all_categories
                    )

            # 域名子串匹配（兜底）
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ''
            domain = domain.replace('www.', '')
            dot_pos = domain.rfind('.')
            if dot_pos > 0:
                domain_base = domain[:dot_pos]
            else:
                domain_base = domain.replace('.', '')
            if domain_base:
                domain_lower = domain_base.lower()
                for category, keywords in CATEGORY_KEYWORDS.items():
                    for kw in keywords:
                        if kw.lower() in domain_lower:
                            is_shopify = any(ind in html.lower() for ind in ["cdn.shopify", "shopify", "myshopify.com"])
                            return HTMLClassificationResult(
                                url=url,
                                primary_category=category,
                                confidence=50.0,
                                vertical_type="unclear",
                                is_shopify=is_shopify,
                                primary_score=1.0,
                                title=title_text[:100]
                            )

            is_shopify = any(ind in html.lower() for ind in ["cdn.shopify", "shopify", "myshopify.com"])
            return HTMLClassificationResult(
                url=url,
                primary_category="Other",
                confidence=0,
                vertical_type="unknown",
                is_shopify=is_shopify,
                primary_score=0,
                title=title_text[:100]
            )

        except Exception as e:
            log.error(f"分类失败 {url}: {e}")
            return HTMLClassificationResult(url=url, primary_category="Error", confidence=0, vertical_type="error", is_shopify=False, primary_score=0, error=str(e))

    def classify_from_excel(self, file_path: str, url_column: str = "domain", max_workers: int = 10) -> dict:
        """批量分类Excel中的网站，在原表格后面添加字段"""
        log.info(f"开始批量分类: {file_path}, 线程数: {max_workers}")

        df = pd.read_excel(file_path, engine="openpyxl")

        if url_column not in df.columns:
            raise ValueError(f"列 '{url_column}' 不存在，可用列: {list(df.columns)}")

        total = len(df)
        log.info(f"共 {total} 条记录待处理")

        # 准备任务列表
        tasks = []
        for idx, row in df.iterrows():
            raw_url = str(row[url_column]).strip()
            if not raw_url or raw_url == "nan":
                tasks.append((idx, None))
            else:
                tasks.append((idx, raw_url))

        # 多线程处理
        results: list[HTMLClassificationResult] = [None] * total
        completed_count = 0
        success_count = 0
        error_count = 0

        def process_one(idx: int, url: str) -> tuple:
            if url is None:
                return idx, HTMLClassificationResult(url="", primary_category="", confidence=0, vertical_type="unknown", is_shopify=False, primary_score=0)
            result = self.classify(url)
            return idx, result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(process_one, idx, url): idx for idx, url in tasks}

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    _, result = future.result()
                    results[idx] = result
                    if result and not result.error:
                        success_count += 1
                    else:
                        error_count += 1
                except Exception as e:
                    log.error(f"处理失败: {e}")
                    results[idx] = HTMLClassificationResult(url="", primary_category="Error", confidence=0, vertical_type="error", is_shopify=False, primary_score=0, error=str(e))
                    error_count += 1

                completed_count += 1
                if completed_count % 10 == 0 or completed_count == total:
                    log.info(f"进度: {completed_count}/{total} | 成功: {success_count} | 失败: {error_count}")
                    # 显示最近成功的详情
                    recent_success = [r for r in results[max(0, idx-9):idx+1] if r and not r.error]
                    if recent_success:
                        for r in recent_success[-3:]:  # 显示最近3条成功记录
                            log.info(f"  ✓ {r.url} → {r.primary_category} (置信度: {r.confidence}%, 类型: {r.vertical_type})")

        # 添加字段到DataFrame
        df['primary_category'] = [r.primary_category if r else "" for r in results]
        df['confidence'] = [r.confidence if r else 0 for r in results]
        df['vertical_type'] = [r.vertical_type if r else "" for r in results]
        df['is_shopify'] = [r.is_shopify if r else False for r in results]
        df['primary_score'] = [r.primary_score if r else 0 for r in results]

        # 保存到原文件
        df.to_excel(file_path, index=False, engine="openpyxl")

        # 统计
        stats = {
            "total": total,
            "success": sum(1 for r in results if r and not r.error),
            "error": sum(1 for r in results if r and r.error),
            "shopify": sum(1 for r in results if r and r.is_shopify),
            "file_path": file_path
        }

        # 统计各类型数量
        type_counts = {}
        for r in results:
            if r:
                vtype = r.vertical_type
                type_counts[vtype] = type_counts.get(vtype, 0) + 1
        stats["type_counts"] = type_counts

        log.info(f"批量分类完成: {stats}")
        return stats
