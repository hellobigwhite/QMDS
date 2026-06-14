import re
import time
from datetime import datetime

from pymongo import MongoClient


MONGO_URI = "mongodb://localhost:27017/"
SOURCE_DB = "shopify_data_new"
RECYCLE_DB = "recycle"
RECYCLE_COLLECTION = "products_deleted"
COLLECTION_CACHE_TTL = 30

IMAGE_REMOVE_KEYWORDS = ["coming-soon", "noimage", "default", ".svg"]
MIN_PRICE = 3
MAX_PRICE = 6000
FORBIDDEN_PROGRESS_EVERY = 50000
FORBIDDEN_BATCH_SIZE = 2000
DELETE_BATCH_SIZE = 2000

FORBIDDEN_KEYWORDS = [
    # Weapons / firearms / ammunition
    "gun", "guns", "handgun", "handguns", "pistol", "pistols", "rifle", "rifles",
    "shotgun", "shotguns", "firearm", "firearms", "ammo", "ammunition", "bullet", "bullets",
    "cartridge", "cartridges", "magazine", "magazines", "drum mag", "suppressor", "silencer",
    "trigger kit", "gun barrel", "upper receiver", "lower receiver", "ar-15", "ak-47",
    "switchblade", "switchblade knives", "dagger", "daggers", "knife", "knives", "combat knife",
    "machete", "tomahawk", "crossbow", "illegal crossbows", "stun gun", "taser",
    "pepper spray", "brass knuckles", "weapon", "weapons", "unlicensed firearms",

    # Drugs / cannabis / controlled substances
    "drug", "drugs", "narcotic", "narcotics", "controlled substance", "controlled substances",
    "heroin", "methamphetamine", "meth", "cocaine", "crack cocaine", "opium",
    "marijuana", "cannabis", "weed", "hash", "hashish", "thc", "cbd flower",
    "psychedelic", "lsd", "mdma", "ecstasy", "ketamine", "fentanyl",
    "drug paraphernalia", "bong", "water pipe", "rolling paper", "grinder",

    # Tobacco / vape
    "tobacco", "cigarette", "cigarettes", "cigar", "cigars", "nicotine",
    "vape", "vapes", "vape pen", "e-cigarette", "e cigarette", "hookah",

    # Adult / sexual
    "porn", "pornographic", "pornographic magazines", "adult video discs",
    "obscene dvds", "erotic", "erotic photos", "sex toy", "sex toys",
    "dildo", "vibrator", "masturbator", "fleshlight", "bdsm", "fetish",
    "lingerie", "bras", "hosiery", "garter belts", "garters", "jock straps",
    "underwear", "underwear slips", "petticoats", "pettipants", "shapewear",

    # Counterfeit / illegal documents / piracy
    "counterfeit", "counterfeit banknotes", "fake securities", "forged passport",
    "fake id", "forged id", "pirated dvds", "pirated software", "counterfeit luxury goods",

    # Wildlife / endangered species
    "ivory", "ivory products", "rhinoceros horn", "tiger bones", "pangolin scales",
    "endangered animal specimens", "bear bile", "shark fin", "sea turtle shell",

    # Existing apparel / policy-sensitive list
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
    "T-Shirt", "Triblend Shirt", "Sweatshirt", "Tee", "Triangular scrapers",
    "Controlled blades", "Pornographic USB drives",

    # Chinese aliases
    "毒品", "大麻", "枪", "枪支", "手枪", "步枪", "弹药", "子弹", "刀具",
    "匕首", "甩刀", "弩", "电击枪", "催泪喷雾", "色情", "成人用品", "假证",
    "假币", "象牙", "犀牛角", "虎骨", "穿山甲", "电子烟", "烟草",
]

FORBIDDEN_REGEX = re.compile("|".join(re.escape(item) for item in FORBIDDEN_KEYWORDS), re.IGNORECASE)
_COLLECTION_CACHE = {"time": 0.0, "items": []}


def get_client():
    return MongoClient(MONGO_URI)


def get_source_db(client):
    return client[SOURCE_DB]


def invalidate_collection_cache():
    _COLLECTION_CACHE["time"] = 0.0
    _COLLECTION_CACHE["items"] = []


def list_product_collections():
    now = time.time()
    if _COLLECTION_CACHE["items"] and now - _COLLECTION_CACHE["time"] < COLLECTION_CACHE_TTL:
        return list(_COLLECTION_CACHE["items"])

    client = get_client()
    try:
        db = get_source_db(client)
        items = []
        for name in sorted(db.list_collection_names()):
            try:
                count = db[name].estimated_document_count()
            except Exception:
                count = db[name].count_documents({})
            items.append({"name": name, "count": count})
        _COLLECTION_CACHE["time"] = now
        _COLLECTION_CACHE["items"] = list(items)
        return items
    finally:
        client.close()


def resolve_collections(db, selected_collection):
    selected = (selected_collection or "").strip()
    if not selected or selected == "__all__":
        return sorted(db.list_collection_names())
    return [selected]


def delete_ids_in_batches(collection, ids, batch_size=DELETE_BATCH_SIZE):
    deleted = 0
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        if not chunk:
            continue
        deleted += collection.delete_many({"_id": {"$in": chunk}}).deleted_count
    return deleted


def parse_price(doc):
    for field in ("折扣价", "原价", "价格"):
        value = doc.get(field)
        if value in ("", None):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def should_delete_basic(doc):
    title = str(doc.get("标题") or "").strip()
    desc = str(doc.get("描述") or "").strip()
    if not title or not desc:
        return True
    if len(desc) < 30:
        return True
    if len(title) <= 7:
        return True
    if re.fullmatch(r"\s*\d+\s*", title):
        return True
    price = parse_price(doc)
    if price is not None and (price < MIN_PRICE or price > MAX_PRICE):
        return True
    return False


def has_bad_image(doc):
    image = doc.get("图片")
    if image in (None, "", []):
        return True
    text = str(image).strip().lower()
    if not text:
        return True
    return any(keyword in text for keyword in IMAGE_REMOVE_KEYWORDS)


def should_recycle_forbidden(doc):
    category = str(doc.get("分类") or "").strip()
    title = str(doc.get("标题") or "").strip()
    desc = str(doc.get("描述") or "").strip()
    if category and FORBIDDEN_REGEX.search(category):
        return True
    if FORBIDDEN_REGEX.search(f"{title} {desc}"):
        return True
    return False


def run_basic_cleanup(selected_collection=None, progress_callback=None):
    client = get_client()
    try:
        db = get_source_db(client)
        collections = resolve_collections(db, selected_collection)
        total_deleted = 0
        for name in collections:
            collection = db[name]
            deleted_ids = []
            for doc in collection.find({}, {"标题": 1, "描述": 1, "折扣价": 1, "原价": 1, "价格": 1}):
                if should_delete_basic(doc):
                    deleted_ids.append(doc["_id"])
            deleted = 0
            if deleted_ids:
                deleted = delete_ids_in_batches(collection, deleted_ids)
            total_deleted += deleted
            if progress_callback:
                progress_callback(f"[{name}] 基础数据清洗完成，删除 {deleted} 条")
        invalidate_collection_cache()
        return {"collections": len(collections), "deleted": total_deleted}
    finally:
        client.close()


def run_image_cleanup(selected_collection=None, progress_callback=None):
    client = get_client()
    try:
        db = get_source_db(client)
        collections = resolve_collections(db, selected_collection)
        total_deleted = 0
        for name in collections:
            collection = db[name]
            deleted_ids = []
            for doc in collection.find({}, {"图片": 1}):
                if has_bad_image(doc):
                    deleted_ids.append(doc["_id"])
            deleted = 0
            if deleted_ids:
                deleted = delete_ids_in_batches(collection, deleted_ids)
            total_deleted += deleted
            if progress_callback:
                progress_callback(f"[{name}] 异常图片清洗完成，删除 {deleted} 条")
        invalidate_collection_cache()
        return {"collections": len(collections), "deleted": total_deleted}
    finally:
        client.close()


def run_forbidden_cleanup(selected_collection=None, progress_callback=None):
    client = get_client()
    try:
        db = get_source_db(client)
        recycle_collection = client[RECYCLE_DB][RECYCLE_COLLECTION]
        collections = resolve_collections(db, selected_collection)
        total_moved = 0

        for name in collections:
            source_collection = db[name]
            estimated = source_collection.estimated_document_count()
            if progress_callback:
                progress_callback(f"[{name}] 开始违禁词过滤，预计扫描 {estimated} 条")

            scanned = 0
            moved = 0
            matched_docs = []
            matched_ids = []

            for doc in source_collection.find({}, {"分类": 1, "标题": 1, "描述": 1, "图片": 1, "source_url": 1, "source_category": 1}):
                scanned += 1
                if should_recycle_forbidden(doc):
                    recycle_doc = dict(doc)
                    original_id = recycle_doc.pop("_id", None)
                    recycle_doc["original_id"] = str(original_id) if original_id is not None else ""
                    recycle_doc["recycle_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    recycle_doc["recycle_reason"] = "forbidden_keyword"
                    recycle_doc["recycle_source_collection"] = name
                    matched_docs.append(recycle_doc)
                    matched_ids.append(doc["_id"])

                if len(matched_docs) >= FORBIDDEN_BATCH_SIZE:
                    recycle_collection.insert_many(matched_docs, ordered=False)
                    moved += source_collection.delete_many({"_id": {"$in": matched_ids}}).deleted_count
                    matched_docs = []
                    matched_ids = []
                    if progress_callback:
                        progress_callback(f"[{name}] 违禁词过滤处理中，已扫描 {scanned} 条，已移入回收站 {moved} 条")
                elif progress_callback and scanned % FORBIDDEN_PROGRESS_EVERY == 0:
                    progress_callback(f"[{name}] 违禁词过滤处理中，已扫描 {scanned} 条，当前已命中 {moved + len(matched_ids)} 条")

            if matched_docs:
                recycle_collection.insert_many(matched_docs, ordered=False)
                moved += delete_ids_in_batches(source_collection, matched_ids)

            total_moved += moved
            if progress_callback:
                progress_callback(f"[{name}] 违禁词过滤完成，移入回收站 {moved} 条")

        invalidate_collection_cache()
        return {"collections": len(collections), "moved": total_moved}
    finally:
        client.close()


def run_full_cleanup(selected_collection=None, progress_callback=None):
    if progress_callback:
        progress_callback("开始阶段 1/3：基础数据清洗")
    basic = run_basic_cleanup(selected_collection=selected_collection, progress_callback=progress_callback)
    if progress_callback:
        progress_callback("开始阶段 2/3：异常图片处理")
    image = run_image_cleanup(selected_collection=selected_collection, progress_callback=progress_callback)
    if progress_callback:
        progress_callback("开始阶段 3/3：违禁词过滤")
    forbidden = run_forbidden_cleanup(selected_collection=selected_collection, progress_callback=progress_callback)

    client = get_client()
    try:
        db = get_source_db(client)
        for name in resolve_collections(db, selected_collection):
            db[name].update_many({}, {"$set": {"子描述": ""}})
    finally:
        client.close()
    invalidate_collection_cache()

    return {
        "collections": max(basic.get("collections", 0), image.get("collections", 0), forbidden.get("collections", 0)),
        "basic_deleted": basic.get("deleted", 0),
        "image_deleted": image.get("deleted", 0),
        "forbidden_moved": forbidden.get("moved", 0),
    }
