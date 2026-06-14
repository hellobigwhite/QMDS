import importlib.util
import io
import os
import re
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime

import pandas as pd
from pymongo import MongoClient
from pymongo import ReplaceOne

from product_processing_v2 import DESC_FIELD, CATEGORY_FIELD, SOURCE_DB, TITLE_FIELD, delete_ids_in_batches, invalidate_collection_cache, text_value


MONGO_URI = "mongodb://localhost:27017/"
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data", "错类扫描")
SOURCE_CATEGORY_FIELD = "source_category"
SOURCE_URL_FIELD = "source_url"
SKU_FIELD = "SKU"
PROGRESS_EVERY = 10000
CACHE_LIMIT = 50000
MOVE_BATCH_SIZE = 500
DESC_MATCH_LIMIT = 4
BACKUP_DB = "recycle"
BACKUP_COLLECTION = "products_mismatch_moved"
DELETE_BACKUP_COLLECTION = "products_mismatch_deleted"
ILLEGAL_EXCEL_CHAR_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")


class MismatchStopRequested(Exception):
    pass


def _raise_if_stop_requested(stop_callback=None):
    if stop_callback and stop_callback():
        raise MismatchStopRequested("Stop requested")

TARGET_COLLECTION_PREFERENCES = {
    "animals & pet supplies": ["pet"],
    "apparel & accessories": ["球衣"],
    "arts & entertainment": ["art_entertainment"],
    "baby & toddler": ["baby_toddler"],
    "beauty & personal care": ["Beauty"],
    "books": ["books"],
    "business & industrial": ["business_industrial"],
    "cameras & optics": ["camera"],
    "electronics": ["electronics"],
    "food, beverages & tobacco": ["liquor"],
    "furniture": ["furniture1"],
    "hardware": ["hardware"],
    "home & garden": ["家居与园艺"],
    "luggage & bags": ["bag"],
    "media": ["media"],
    "office supplies": ["Office Supplies"],
    "religious & ceremonial": ["religious_ceremonial"],
    "software": ["software"],
    "sporting goods": ["sports"],
    "toys & games": ["toy"],
    "vehicles & parts": ["auto"],
}

COLLECTION_CATEGORY_GROUPS = {
    "animals & pet supplies": [
        "animal",
        "animals",
        "pet",
        "pets",
        "dog",
        "dogs",
        "puppy",
        "cat",
        "cats",
        "kitten",
        "bird",
        "fish",
        "pet food",
        "dog food",
        "cat food",
        "宠物",
        "宠物用品",
        "动物",
        "宠物与用品",
    ],
    "apparel & accessories": [
        "apparel",
        "clothing",
        "fashion",
        "jersey",
        "jerseys",
        "shirt",
        "t shirt",
        "t-shirt",
        "hoodie",
        "jacket",
        "dress",
        "pants",
        "shoes",
        "sneakers",
        "jewelry",
        "watch",
        "wallet",
        "球衣",
        "服装",
        "服饰",
        "鞋服",
        "配饰",
        "穿搭",
        "时尚",
        "箱包配饰",
    ],
    "arts & entertainment": [
        "art",
        "arts",
        "entertainment",
        "art entertainment",
        "arts entertainment",
        "music",
        "craft",
        "crafts",
        "painting",
        "canvas",
        "poster",
        "wall art",
        "instrument",
        "instruments",
        "guitar",
        "piano",
        "乐器",
        "音乐",
        "艺术",
        "艺术娱乐",
        "文娱",
    ],
    "baby & toddler": [
        "baby",
        "toddler",
        "newborn",
        "infant",
        "diaper",
        "stroller",
        "car seat",
        "crib",
        "pacifier",
        "baby bottle",
        "onesie",
        "婴儿",
        "婴童",
        "母婴",
        "宝宝",
    ],
    "beauty & personal care": [
        "beauty",
        "personal care",
        "beauty personal care",
        "makeup",
        "skincare",
        "cosmetics",
        "perfume",
        "shampoo",
        "conditioner",
        "lotion",
        "serum",
        "sunscreen",
        "nail",
        "美妆",
        "个护",
        "美容",
        "护理",
        "护肤",
    ],
    "books": [
        "book",
        "books",
        "novel",
        "textbook",
        "manga",
        "ebook",
        "audiobook",
        "cookbook",
        "bible",
        "图书",
        "书籍",
        "书",
    ],
    "business & industrial": [
        "business",
        "industrial",
        "industry",
        "commercial",
        "warehouse",
        "safety",
        "packaging",
        "barcode",
        "shelving",
        "pump",
        "forklift",
        "tool box",
        "工商业",
        "工业",
        "商业",
        "商用",
    ],
    "cameras & optics": [
        "camera",
        "cameras",
        "optics",
        "optic",
        "dslr",
        "mirrorless",
        "lens",
        "tripod",
        "binoculars",
        "telescope",
        "microscope",
        "drone",
        "相机",
        "摄影",
        "摄像",
        "光学",
        "镜头",
    ],
    "electronics": [
        "electronics",
        "electronic",
        "phone",
        "smartphone",
        "tablet",
        "laptop",
        "computer",
        "monitor",
        "keyboard",
        "mouse",
        "charger",
        "cable",
        "headphone",
        "headphones",
        "headset",
        "speaker",
        "audio",
        "电子",
        "数码",
        "电器",
    ],
    "food, beverages & tobacco": [
        "food",
        "foods",
        "beverage",
        "beverages",
        "tobacco",
        "liquor",
        "snack",
        "snacks",
        "candy",
        "coffee",
        "tea",
        "drink",
        "drinks",
        "wine",
        "beer",
        "cigar",
        "酒类",
        "酒水",
        "食品",
        "饮料",
        "烟酒",
        "食品饮料",
    ],
    "furniture": [
        "furniture",
        "furnishing",
        "chair",
        "table",
        "sofa",
        "couch",
        "desk",
        "cabinet",
        "shelf",
        "bed",
        "mattress",
        "家具",
        "家私",
    ],
    "hardware": [
        "hardware",
        "tool",
        "tools",
        "drill",
        "saw",
        "screw",
        "screws",
        "bolt",
        "bolts",
        "wrench",
        "pliers",
        "hammer",
        "nail",
        "nails",
        "pipe",
        "五金",
        "工具五金",
        "建材五金",
    ],
    "health & beauty": [
        "health",
        "wellness",
        "supplement",
        "supplements",
        "vitamin",
        "vitamins",
        "nutrition",
        "probiotic",
        "collagen",
        "保健",
        "健康",
        "健康美容",
    ],
    "home & garden": [
        "home",
        "garden",
        "home garden",
        "kitchen",
        "cookware",
        "bedding",
        "decor",
        "patio",
        "outdoor",
        "planter",
        "fertilizer",
        "lawn",
        "家居",
        "园艺",
        "家居与园艺",
        "家居园艺",
        "种子",
        "花园",
        "庭院",
    ],
    "luggage & bags": [
        "bag",
        "bags",
        "handbag",
        "tote",
        "backpack",
        "purse",
        "luggage",
        "suitcase",
        "duffel",
        "briefcase",
        "crossbody",
        "包",
        "箱包",
        "行李",
        "包袋",
        "背包",
    ],
    "media": [
        "media",
        "dvd",
        "cd",
        "vinyl",
        "album",
        "movie",
        "film",
        "blu ray",
        "game disc",
        "影音",
        "媒体",
    ],
    "office supplies": [
        "office",
        "office supplies",
        "stationery",
        "notebook",
        "folder",
        "binder",
        "printer paper",
        "pen",
        "marker",
        "stapler",
        "envelope",
        "办公",
        "办公用品",
        "文具",
    ],
    "religious & ceremonial": [
        "religious",
        "ceremonial",
        "bible",
        "prayer",
        "rosary",
        "cross",
        "church",
        "altar",
        "candle",
        "wedding",
        "baptism",
        "宗教",
        "礼仪",
        "宗教礼仪",
    ],
    "software": [
        "software",
        "app",
        "apps",
        "license",
        "subscription",
        "antivirus",
        "windows",
        "macos",
        "adobe",
        "office 365",
        "vpn",
        "pdf",
        "cad",
        "软件",
    ],
    "sporting goods": [
        "sport",
        "sports",
        "sporting",
        "fitness",
        "gym",
        "yoga",
        "basketball",
        "soccer",
        "football",
        "golf",
        "fishing",
        "camping",
        "hiking",
        "bike",
        "bicycle",
        "ski",
        "篮球",
        "钓鱼",
        "体育",
        "运动",
        "体育用品",
        "运动户外",
    ],
    "toys & games": [
        "toy",
        "toys",
        "game",
        "games",
        "lego",
        "doll",
        "puzzle",
        "board game",
        "rc car",
        "plush",
        "slime",
        "nerf",
        "model kit",
        "玩具",
        "游戏",
        "玩具游戏",
    ],
    "vehicles & parts": [
        "vehicle",
        "vehicles",
        "auto",
        "automotive",
        "car",
        "cars",
        "motorcycle",
        "scooter",
        "tire",
        "wheel",
        "brake",
        "battery",
        "headlight",
        "dash cam",
        "trailer",
        "truck",
        "摩托",
        "汽配",
        "汽车",
        "车辆",
        "交通工具",
        "车辆配件",
    ],
    "mature": [
        "adult",
        "mature",
        "sexy",
        "erotic",
        "lingerie",
        "vibrator",
        "dildo",
        "bdsm",
        "condom",
        "lube",
        "成人",
        "情趣",
        "性用品",
        "成人用品",
    ],
}

COLLECTION_CATEGORY_ALIASES = {}
for _target, _aliases in COLLECTION_CATEGORY_GROUPS.items():
    for _alias in _aliases:
        _clean_alias = text_value(_alias).lower().strip()
        if _clean_alias:
            COLLECTION_CATEGORY_ALIASES[_clean_alias] = _target

BROAD_ALIAS_ITEMS = [
    (alias, target, bool(re.search(r"[a-z]", alias)))
    for alias, target in sorted(COLLECTION_CATEGORY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
]


def ensure_export_dir():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    return EXPORT_DIR


def list_mismatch_reports(limit=20):
    ensure_export_dir()
    items = []
    for name in os.listdir(EXPORT_DIR):
        if not name.lower().endswith(".xlsx"):
            continue
        full_path = os.path.join(EXPORT_DIR, name)
        if not os.path.isfile(full_path):
            continue
        items.append(
            {
                "name": name,
                "path": full_path,
                "mtime": datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items[:limit]


def clean_name(value):
    text = str(value or "").strip()
    for bad in '\\/:*?"<>|':
        text = text.replace(bad, "_")
    return text or "mismatch"


def _load_mainfenle_module():
    module_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mango", "mainfenle.py")
    spec = importlib.util.spec_from_file_location("ysqd_mainfenle_rules", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载大类规则文件 mango/mainfenle.py")
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


_MAINFENLE_MODULE = None


def get_mainfenle_module():
    global _MAINFENLE_MODULE
    if _MAINFENLE_MODULE is None:
        _MAINFENLE_MODULE = _load_mainfenle_module()
    return _MAINFENLE_MODULE


def normalize_collection_name(name):
    text = text_value(name).lower().strip()
    for char in "_-&/":
        text = text.replace(char, " ")
    text = " ".join(text.split())
    text = text.rstrip("0123456789").strip()
    return text


def resolve_expected_category(collection_name):
    normalized = normalize_collection_name(collection_name)
    if normalized in COLLECTION_CATEGORY_ALIASES:
        return COLLECTION_CATEGORY_ALIASES[normalized]

    for key, target in sorted(COLLECTION_CATEGORY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if not key:
            continue
        if normalized == key:
            return target

        if re.search(r"[a-z]", key):
            if normalized.startswith(f"{key} ") or normalized.endswith(f" {key}") or f" {key} " in f" {normalized} ":
                return target
        else:
            if key in normalized or normalized in key:
                return target
    return ""


def get_field_matches(module, text):
    normalized = module.normalize(text_value(text))
    if not normalized:
        return []
    return module.get_all_matched_categories(normalized)


def get_cached_matches(module, text, cache=None):
    normalized = module.normalize(text_value(text))
    if not normalized:
        return []
    if cache is not None and normalized in cache:
        return list(cache[normalized])
    matches = module.get_all_matched_categories(normalized)
    if cache is not None and len(cache) < CACHE_LIMIT:
        cache[normalized] = tuple(matches)
    return matches


def prune_desc_matches(matches):
    deduped = []
    seen = set()
    for item in matches:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    if len(deduped) > DESC_MATCH_LIMIT:
        return []
    return deduped


def normalize_broad_text(text):
    value = text_value(text).lower().strip()
    if not value:
        return ""
    value = re.sub(r"[_/&|>\-:;,\(\)\[\]\.]+", " ", value)
    value = " ".join(value.split())
    return value


def get_broad_matches(text, cache=None):
    normalized = normalize_broad_text(text)
    if not normalized:
        return []
    if cache is not None and normalized in cache:
        return list(cache[normalized])

    hits = []
    wrapped = f" {normalized} "
    for alias, target, is_english in BROAD_ALIAS_ITEMS:
        if is_english:
            if f" {alias} " in wrapped:
                hits.append(target)
        else:
            if alias in normalized:
                hits.append(target)

    deduped = []
    seen = set()
    for item in hits:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    if cache is not None and len(cache) < CACHE_LIMIT:
        cache[normalized] = tuple(deduped)
    return deduped


def score_broad_categories(doc, cache=None):
    title_matches = get_broad_matches(doc.get(TITLE_FIELD), cache=cache)
    category_matches = get_broad_matches(doc.get(CATEGORY_FIELD), cache=cache)
    scores = Counter()
    for item in title_matches:
        scores[item] += 2
    for item in category_matches:
        scores[item] += 3
    return {
        "scores": scores,
        "ranked": build_ranked_scores(scores),
        "title_matches": title_matches,
        "category_matches": category_matches,
        "source_category_matches": [],
        "desc_matches": [],
    }


def should_heavy_refine(expected_category, broad_info):
    ranked = broad_info["ranked"]
    if not ranked:
        return False

    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    expected_score = broad_info["scores"].get(expected_category, 0)

    if top_category == expected_category:
        return False
    if expected_category in broad_info["category_matches"]:
        return False
    if top_score < 3:
        return False
    if top_score - second_score < 1:
        return False
    if expected_score >= top_score:
        return False
    return True


def build_ranked_scores(scores):
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def score_primary_categories(module, doc, title_cache=None, structured_cache=None):
    title_matches = get_cached_matches(module, doc.get(TITLE_FIELD), cache=title_cache)
    category_matches = get_cached_matches(module, doc.get(CATEGORY_FIELD), cache=structured_cache)
    scores = Counter()
    for item in title_matches:
        scores[item] += 2
    for item in category_matches:
        scores[item] += 3

    return {
        "scores": scores,
        "ranked": build_ranked_scores(scores),
        "title_matches": title_matches,
        "category_matches": category_matches,
        "source_category_matches": [],
        "desc_matches": [],
    }


def refine_with_desc(module, doc, primary_info, desc_cache=None):
    desc_matches = prune_desc_matches(get_cached_matches(module, doc.get(DESC_FIELD), cache=desc_cache))
    scores = Counter(primary_info["scores"])
    for item in desc_matches:
        scores[item] += 1
    return {
        "scores": scores,
        "ranked": build_ranked_scores(scores),
        "title_matches": list(primary_info["title_matches"]),
        "category_matches": list(primary_info["category_matches"]),
        "source_category_matches": list(primary_info["source_category_matches"]),
        "desc_matches": desc_matches,
    }


def should_fast_pass(expected_category, score_info):
    scores = score_info["scores"]
    ranked = score_info["ranked"]
    if not ranked:
        return True

    top_category, top_score = ranked[0]
    expected_score = scores.get(expected_category, 0)

    if top_category == expected_category:
        return True
    if expected_category in score_info["category_matches"] and expected_score >= 3:
        return True
    if expected_score >= 4:
        return True
    if expected_category in score_info["title_matches"] and expected_score >= max(top_score - 1, 3):
        return True
    if expected_category in score_info["category_matches"] and expected_score >= top_score - 1:
        return True
    return False


def should_refine_candidate(expected_category, score_info):
    ranked = score_info["ranked"]
    if not ranked:
        return False

    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    expected_score = score_info["scores"].get(expected_category, 0)

    if top_category == expected_category:
        return False
    if top_score < 3:
        return False
    if top_score - second_score < 1:
        return False
    if expected_score >= top_score:
        return False
    if expected_category in score_info["category_matches"] and expected_score >= 3:
        return False
    return True


def is_high_confidence_mismatch(expected_category, score_info):
    ranked = score_info["ranked"]
    if not ranked:
        return False, "", 0, 0
    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    expected_score = score_info["scores"].get(expected_category, 0)
    expected_in_primary = (
        expected_category in score_info["title_matches"]
        or expected_category in score_info["category_matches"]
    )

    if top_category == expected_category:
        return False, top_category, top_score, second_score
    if expected_in_primary:
        return False, top_category, top_score, second_score
    if top_score < 3:
        return False, top_category, top_score, second_score
    if top_score - second_score < 1:
        return False, top_category, top_score, second_score
    if expected_score >= top_score - 1:
        return False, top_category, top_score, second_score
    return True, top_category, top_score, second_score


def is_quick_mismatch(expected_category, score_info):
    ranked = score_info["ranked"]
    if not ranked:
        return False, "", 0, 0

    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    expected_score = score_info["scores"].get(expected_category, 0)

    if should_fast_pass(expected_category, score_info):
        return False, top_category, top_score, second_score
    if top_score < 3:
        return False, top_category, top_score, second_score
    if top_score - second_score < 1:
        return False, top_category, top_score, second_score
    if expected_score >= 3:
        return False, top_category, top_score, second_score
    return True, top_category, top_score, second_score


def export_mismatch_rows(rows, collection_name):
    ensure_export_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{clean_name(collection_name)}_错类扫描_{timestamp}_{len(rows)}条.xlsx"
    file_path = os.path.join(EXPORT_DIR, file_name)
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("没有疑似错类数据可导出")
    for column in df.columns:
        df[column] = df[column].map(sanitize_excel_value)
    df.to_excel(file_path, index=False)
    return file_path


def sanitize_excel_value(value):
    if not isinstance(value, str):
        return value
    return ILLEGAL_EXCEL_CHAR_RE.sub("", value)


def build_mismatch_row(collection_name, expected_category, predicted_category, top_score, second_score, doc, score_info):
    return {
        "集合": collection_name,
        "预期大类": expected_category,
        "预测大类": predicted_category,
        "预测分数": top_score,
        "第二分数": second_score,
        "SKU": text_value(doc.get(SKU_FIELD)),
        "标题": text_value(doc.get(TITLE_FIELD)),
        "分类": text_value(doc.get(CATEGORY_FIELD)),
        "来源分类": text_value(doc.get(SOURCE_CATEGORY_FIELD)),
        "标题命中大类": " | ".join(score_info["title_matches"]),
        "分类命中大类": " | ".join(score_info["category_matches"]),
        "来源分类命中大类": " | ".join(score_info["source_category_matches"]),
        "描述命中大类": " | ".join(score_info["desc_matches"]),
        "来源链接": text_value(doc.get(SOURCE_URL_FIELD)),
    }


def analyze_collection_mismatches(collection, collection_name, expected_category, module=None, logger=None, collect_docs=False, quick_only=False, stop_callback=None):
    rows = []
    scanned = 0
    fast_passed = 0
    refined = 0
    heavy_candidates = 0
    broad_cache = {}
    title_cache = {}
    structured_cache = {}
    desc_cache = {}
    projection = {
        TITLE_FIELD: 1,
        CATEGORY_FIELD: 1,
        SOURCE_CATEGORY_FIELD: 1,
        DESC_FIELD: 1,
        SOURCE_URL_FIELD: 1,
        SKU_FIELD: 1,
    }

    for doc in collection.find({}, projection).batch_size(1000):
        _raise_if_stop_requested(stop_callback)
        scanned += 1
        broad_info = score_broad_categories(doc, cache=broad_cache)
        if should_fast_pass(expected_category, broad_info):
            fast_passed += 1
            if logger and scanned % PROGRESS_EVERY == 0:
                logger(
                    f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                    f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
                )
            continue

        if quick_only:
            mismatch, predicted_category, top_score, second_score = is_quick_mismatch(expected_category, broad_info)
            if mismatch:
                item = build_mismatch_row(
                    collection_name=collection_name,
                    expected_category=expected_category,
                    predicted_category=predicted_category,
                    top_score=top_score,
                    second_score=second_score,
                    doc=doc,
                    score_info=broad_info,
                )
                if collect_docs:
                    item["_doc"] = dict(doc)
                rows.append(item)
            elif module:
                desc_info = refine_with_desc(module, doc, broad_info, desc_cache=desc_cache)
                mismatch, predicted_category, top_score, second_score = is_quick_mismatch(expected_category, desc_info)
                if mismatch:
                    item = build_mismatch_row(
                        collection_name=collection_name,
                        expected_category=expected_category,
                        predicted_category=predicted_category,
                        top_score=top_score,
                        second_score=second_score,
                        doc=doc,
                        score_info=desc_info,
                    )
                    if collect_docs:
                        item["_doc"] = dict(doc)
                    rows.append(item)

            if logger and scanned % PROGRESS_EVERY == 0:
                logger(
                    f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                    f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
                )
            continue

        if not should_heavy_refine(expected_category, broad_info):
            if logger and scanned % PROGRESS_EVERY == 0:
                logger(
                    f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                    f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
                )
            continue

        heavy_candidates += 1
        primary_info = score_primary_categories(
            module,
            doc,
            title_cache=title_cache,
            structured_cache=structured_cache,
        )
        if should_fast_pass(expected_category, primary_info):
            fast_passed += 1
            if logger and scanned % PROGRESS_EVERY == 0:
                logger(
                    f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                    f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
                )
            continue

        if not should_refine_candidate(expected_category, primary_info):
            if logger and scanned % PROGRESS_EVERY == 0:
                logger(
                    f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                    f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
                )
            continue

        refined += 1
        score_info = refine_with_desc(module, doc, primary_info, desc_cache=desc_cache)
        mismatch, predicted_category, top_score, second_score = is_high_confidence_mismatch(expected_category, score_info)
        if mismatch:
            item = build_mismatch_row(
                collection_name=collection_name,
                expected_category=expected_category,
                predicted_category=predicted_category,
                top_score=top_score,
                second_score=second_score,
                doc=doc,
                score_info=score_info,
            )
            if collect_docs:
                item["_doc"] = dict(doc)
            rows.append(item)

        if logger and scanned % PROGRESS_EVERY == 0:
            logger(
                f"已扫描 {scanned} 条，快速放行 {fast_passed} 条，"
                f"进入重判 {heavy_candidates} 条，进入精判 {refined} 条，高置信疑似错类 {len(rows)} 条"
            )

    return {
        "rows": rows,
        "scanned": scanned,
        "fast_passed": fast_passed,
        "refined": refined,
        "heavy_candidates": heavy_candidates,
    }


def slugify_category_name(value):
    text = text_value(value).lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "mismatch_target"


def resolve_target_collection_name(db, predicted_category, source_collection):
    existing_names = list(db.list_collection_names())
    preferred_names = TARGET_COLLECTION_PREFERENCES.get(predicted_category, [])

    for preferred in preferred_names:
        for name in existing_names:
            if name == source_collection:
                continue
            if name.lower() == preferred.lower():
                return name

    predicted_normalized = normalize_collection_name(predicted_category)
    candidates = []
    for name in existing_names:
        if name == source_collection:
            continue
        if resolve_expected_category(name) != predicted_category:
            continue
        score = 0
        normalized = normalize_collection_name(name)
        if normalized == predicted_normalized:
            score += 100
        if re.search(r"[A-Za-z]", name):
            score += 10
        if " " in name or "_" in name:
            score += 5
        score += min(len(name), 30)
        candidates.append((score, name))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1].lower()))
        return candidates[0][1]

    if preferred_names:
        return preferred_names[0]
    return slugify_category_name(predicted_category)


def build_backup_doc(doc, collection_name, target_collection, item):
    backup_doc = dict(doc)
    original_id = backup_doc.pop("_id", None)
    backup_doc["original_id"] = str(original_id) if original_id is not None else ""
    backup_doc["mismatch_backup_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_doc["mismatch_source_collection"] = collection_name
    backup_doc["mismatch_target_collection"] = target_collection
    backup_doc["mismatch_expected_category"] = item["预期大类"]
    backup_doc["mismatch_predicted_category"] = item["预测大类"]
    backup_doc["mismatch_score"] = item["预测分数"]
    backup_doc["mismatch_second_score"] = item["第二分数"]
    return backup_doc


def build_target_doc(doc, collection_name, target_collection, item):
    moved_doc = dict(doc)
    moved_doc["mismatch_source_collection"] = collection_name
    moved_doc["mismatch_target_collection"] = target_collection
    moved_doc["mismatch_expected_category"] = item["预期大类"]
    moved_doc["mismatch_predicted_category"] = item["预测大类"]
    moved_doc["mismatch_score"] = item["预测分数"]
    moved_doc["mismatch_second_score"] = item["第二分数"]
    moved_doc["mismatch_moved_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return moved_doc


def count_existing_ids(collection, ids, batch_size=MOVE_BATCH_SIZE):
    remaining = 0
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        if not chunk:
            continue
        remaining += collection.count_documents({"_id": {"$in": chunk}})
    return remaining


def flush_move_batches(db, source_collection, backup_collection, pending_moves):
    moved = 0
    for target_name, entries in list(pending_moves.items()):
        if not entries:
            continue

        target_collection = db[target_name]
        target_ops = []
        backup_docs = []
        delete_ids = []
        for item, doc in entries:
            target_ops.append(ReplaceOne({"_id": doc["_id"]}, build_target_doc(doc, source_collection.name, target_name, item), upsert=True))
            backup_docs.append(build_backup_doc(doc, source_collection.name, target_name, item))
            delete_ids.append(doc["_id"])

        if target_ops:
            target_collection.bulk_write(target_ops, ordered=False)
        if backup_docs:
            backup_collection.insert_many(backup_docs, ordered=False)
        if delete_ids:
            deleted = delete_ids_in_batches(source_collection, delete_ids)
            if deleted == len(delete_ids):
                moved += deleted
            else:
                remaining = count_existing_ids(source_collection, delete_ids)
                moved += max(len(delete_ids) - remaining, 0)
        pending_moves[target_name] = []
    return moved


def build_delete_backup_doc(doc, collection_name, item):
    backup_doc = dict(doc)
    original_id = backup_doc.pop("_id", None)
    backup_doc["original_id"] = str(original_id) if original_id is not None else ""
    backup_doc["recycle_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_doc["recycle_reason"] = "quick_category_mismatch"
    backup_doc["recycle_source_collection"] = collection_name
    backup_doc["mismatch_expected_category"] = item["预期大类"]
    backup_doc["mismatch_predicted_category"] = item["预测大类"]
    backup_doc["mismatch_score"] = item["预测分数"]
    backup_doc["mismatch_second_score"] = item["第二分数"]
    return backup_doc


def flush_delete_batches(source_collection, backup_collection, pending_docs, pending_ids):
    if not pending_ids:
        return 0

    if pending_docs:
        backup_collection.insert_many(pending_docs, ordered=False)

    deleted = delete_ids_in_batches(source_collection, pending_ids)
    if deleted == len(pending_ids):
        return deleted

    remaining = count_existing_ids(source_collection, pending_ids)
    return max(len(pending_ids) - remaining, 0)


def scan_collection_mismatches(collection_name, logger=None, stop_callback=None):
    if not collection_name or collection_name == "__all__":
        raise ValueError("错类扫描只支持单个集合，请先选择一个集合")

    expected_category = resolve_expected_category(collection_name)
    if not expected_category:
        raise ValueError(f"暂时无法识别集合 {collection_name} 对应的大类，请先补充映射规则")

    client = MongoClient(MONGO_URI)
    analysis = {"scanned": 0}
    try:
        collection = client[SOURCE_DB][collection_name]
        total = collection.estimated_document_count()
        if logger:
            logger(f"开始按快速分类扫描集合 {collection_name}，预期大类：{expected_category}，预计商品数 {total}")

        analysis = analyze_collection_mismatches(
            collection=collection,
            collection_name=collection_name,
            expected_category=expected_category,
            module=None,
            logger=logger,
            collect_docs=False,
            quick_only=True,
            stop_callback=stop_callback,
        )
        rows = analysis["rows"]

        if not rows:
            if logger:
                logger(
                    f"扫描完成，共扫描 {analysis['scanned']} 条，快速放行 {analysis['fast_passed']} 条，"
                    f"快速判定不匹配 0 条"
                )
            return {
                "collection_name": collection_name,
                "expected_category": expected_category,
                "scanned_count": analysis["scanned"],
                "mismatch_count": 0,
                "file_path": "",
            }

        file_path = export_mismatch_rows(rows, collection_name)
        if logger:
            logger(
                f"扫描完成，共扫描 {analysis['scanned']} 条，快速放行 {analysis['fast_passed']} 条，"
                f"快速判定不匹配 {len(rows)} 条"
            )
            logger(f"结果已导出：{file_path}")

        return {
            "collection_name": collection_name,
            "expected_category": expected_category,
            "scanned_count": analysis["scanned"],
            "mismatch_count": len(rows),
            "file_path": file_path,
            "stopped": False,
        }
    except MismatchStopRequested:
        if logger:
            logger("收到停止请求，快速分类扫描已提前结束")
        return {
            "collection_name": collection_name,
            "expected_category": expected_category,
            "scanned_count": analysis.get("scanned", 0),
            "mismatch_count": 0,
            "file_path": "",
            "stopped": True,
        }
    finally:
        client.close()


def delete_collection_mismatches(collection_name, logger=None, stop_callback=None):
    if not collection_name or collection_name == "__all__":
        raise ValueError("快速分类直删只支持单个集合，请先选择一个集合")

    expected_category = resolve_expected_category(collection_name)
    if not expected_category:
        raise ValueError(f"暂时无法识别集合 {collection_name} 对应的大类，请先补充映射规则")

    client = MongoClient(MONGO_URI)
    analysis = {"scanned": 0}
    deleted = 0
    file_path = ""
    try:
        db = client[SOURCE_DB]
        source_collection = db[collection_name]
        backup_collection = client[BACKUP_DB][DELETE_BACKUP_COLLECTION]
        total = source_collection.estimated_document_count()
        if logger:
            logger(f"开始按快速分类直删集合 {collection_name}，预期大类：{expected_category}，预计商品数 {total}")

        analysis = analyze_collection_mismatches(
            collection=source_collection,
            collection_name=collection_name,
            expected_category=expected_category,
            module=None,
            logger=logger,
            collect_docs=True,
            quick_only=True,
            stop_callback=stop_callback,
        )
        rows = analysis["rows"]
        if not rows:
            if logger:
                logger(
                    f"删除完成，共扫描 {analysis['scanned']} 条，快速放行 {analysis['fast_passed']} 条，"
                    f"快速判定不匹配 0 条，实际删除 0 条"
                )
            return {
                "collection_name": collection_name,
                "expected_category": expected_category,
                "scanned_count": analysis["scanned"],
                "mismatch_count": 0,
                "deleted_count": 0,
                "file_path": "",
            }

        file_path = export_mismatch_rows([{k: v for k, v in row.items() if not k.startswith("_")} for row in rows], collection_name)
        pending_docs = []
        pending_ids = []

        for index, item in enumerate(rows, start=1):
            _raise_if_stop_requested(stop_callback)
            doc = item.pop("_doc")
            pending_docs.append(build_delete_backup_doc(doc, collection_name, item))
            pending_ids.append(doc["_id"])
            if len(pending_ids) >= MOVE_BATCH_SIZE:
                deleted += flush_delete_batches(source_collection, backup_collection, pending_docs, pending_ids)
                pending_docs = []
                pending_ids = []
            if logger and index % PROGRESS_EVERY == 0:
                logger(f"快速不匹配处理中，已准备 {index} 条，已删除 {deleted} 条")

        deleted += flush_delete_batches(source_collection, backup_collection, pending_docs, pending_ids)
        invalidate_collection_cache()

        if logger:
            logger(
                f"删除完成，共扫描 {analysis['scanned']} 条，快速放行 {analysis['fast_passed']} 条，"
                f"快速判定不匹配 {len(rows)} 条，实际删除 {deleted} 条"
            )
            logger(f"删除前快照已导出：{file_path}")

        return {
            "collection_name": collection_name,
            "expected_category": expected_category,
            "scanned_count": analysis["scanned"],
            "mismatch_count": len(rows),
            "deleted_count": deleted,
            "file_path": file_path,
            "stopped": False,
        }
    except MismatchStopRequested:
        if logger:
            logger("收到停止请求，快速分类直删已提前结束")
        return {
            "collection_name": collection_name,
            "expected_category": expected_category,
            "scanned_count": analysis.get("scanned", 0),
            "mismatch_count": 0,
            "deleted_count": deleted,
            "file_path": file_path,
            "stopped": True,
        }
    finally:
        client.close()


def relocate_collection_mismatches(collection_name, logger=None):
    if not collection_name or collection_name == "__all__":
        raise ValueError("错类处理只支持单个集合，请先选择一个集合")

    expected_category = resolve_expected_category(collection_name)
    if not expected_category:
        raise ValueError(f"暂时无法识别集合 {collection_name} 对应的大类，请先补充映射规则")

    module = get_mainfenle_module()
    client = MongoClient(MONGO_URI)
    try:
        db = client[SOURCE_DB]
        source_collection = db[collection_name]
        backup_collection = client[BACKUP_DB][BACKUP_COLLECTION]
        total = source_collection.estimated_document_count()
        if logger:
            logger(f"开始扫描并转移集合 {collection_name}，预期大类：{expected_category}，预计商品数 {total}")

        analysis = analyze_collection_mismatches(
            collection=source_collection,
            collection_name=collection_name,
            expected_category=expected_category,
            module=module,
            logger=logger,
            collect_docs=True,
        )
        rows = analysis["rows"]
        if not rows:
            if logger:
                logger("没有发现可转移的高置信疑似错类")
            return {
                "collection_name": collection_name,
                "expected_category": expected_category,
                "scanned_count": analysis["scanned"],
                "mismatch_count": 0,
                "moved_count": 0,
                "file_path": "",
                "created_targets": [],
            }

        file_path = export_mismatch_rows([{k: v for k, v in row.items() if not k.startswith("_")} for row in rows], collection_name)
        pending_moves = {}
        moved = 0
        created_targets = set()
        existing_names = set(db.list_collection_names())

        for index, item in enumerate(rows, start=1):
            doc = item.pop("_doc")
            target_name = resolve_target_collection_name(db, item["预测大类"], collection_name)
            if target_name == collection_name:
                continue
            if target_name not in existing_names:
                created_targets.add(target_name)
                existing_names.add(target_name)
            pending_moves.setdefault(target_name, []).append((item, doc))
            if sum(len(values) for values in pending_moves.values()) >= MOVE_BATCH_SIZE:
                moved += flush_move_batches(db, source_collection, backup_collection, pending_moves)
            if logger and index % PROGRESS_EVERY == 0:
                logger(f"高置信疑似错类处理中，已准备 {index} 条，已转移 {moved} 条")

        moved += flush_move_batches(db, source_collection, backup_collection, pending_moves)
        invalidate_collection_cache()

        if logger:
            logger(
                f"转移完成，共扫描 {analysis['scanned']} 条，发现高置信疑似错类 {len(rows)} 条，"
                f"实际转移 {moved} 条"
            )
            if created_targets:
                logger(f"本次新建目标集合：{', '.join(sorted(created_targets))}")
            logger(f"转移前快照已导出：{file_path}")

        return {
            "collection_name": collection_name,
            "expected_category": expected_category,
            "scanned_count": analysis["scanned"],
            "mismatch_count": len(rows),
            "moved_count": moved,
            "file_path": file_path,
            "created_targets": sorted(created_targets),
        }
    finally:
        client.close()
