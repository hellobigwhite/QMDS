"""MongoDB 集合命名迁移脚本（qmds_url_stores + qmds_product_data）

将碎片化的 {category}__{subcategory} 集合并入标准子分类集合：
- 多子分类拼接集合（source_subcategory 含逗号）-> {category}__other
- 单子分类集合：在标准列表中 -> 不变；否则按同义词映射或归入 other

用法:
    python scripts/migrate_collection_names.py              # dry-run 预览
    python scripts/migrate_collection_names.py --execute     # 实际执行

注意：执行前建议用 mongodump 备份数据库。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pymongo import MongoClient, ASCENDING
from qmds.config import settings
from qmds.config.categories import (
    CATEGORY_SEPARATOR,
    DEFAULT_SUBCATEGORY,
    STANDARD_SUBCATEGORIES,
    make_collection_prefix,
    normalize_subcategory,
    get_standard_subcategories,
)
from qmds.utils.logger import setup_logger, get_logger

log = get_logger("migrate_collections")


# NOTE: STANDARD_SUBCATEGORIES / get_standard_subcategories 已迁移至
#   qmds.config.categories，作为 ai_classifier.py prompt、shopify.py 运行时校验、
#   本迁移脚本三处共用的唯一真相源。


# ── 同义词映射：非标准子分类名 -> 标准子分类名 ──
# 未出现在此映射中的非标准子分类 -> other
SYNONYM_MAP: dict[str, dict[str, str]] = {
    "vehicles_parts": {
        "auto_parts": "car_parts",
        "electric_bicycles": "other",
        "electric_bikes": "other",
        "rc_parts": "other",
        "rc_parts_accessories": "other",
        "rc_vehicle_parts": "other",
        "rc_vehicles_parts": "other",
        "rv_parts_hardware": "other",
        "trailers": "other",
        "truck_parts": "other",
        "truck_parts_accessories": "other",
        "boat_parts": "other",
        "marine_parts_accessories": "other",
    },
    "animals_pet_supplies": {
        "dog_food": "pet_food",
        "horse_equipment": "other",
        "horse_care": "other",
        "horse_tack_equipment": "other",
        "livestock": "other",
        "aquarium_fish": "other",
        "pet_reptiles_exotics": "other",
        "reptile_supplies": "other",
        "pet_supplies": "pet_accessories",
    },
    "toys_games": {
        "plush_toys": "other",
        "collectibles": "other",
        "collectible_toys": "other",
        "model_kits": "other",
        "scale_model_kits": "other",
        "rc_vehicles": "other",
        "building_toys": "other",
        "model_trains": "other",
        "model_trains_accessories": "other",
        "model_trains_sets": "other",
        "arcade_cabinets_dollhouse_kits": "other",
        "inflatables_bouncers": "other",
        "kendama": "other",
        "kites": "other",
        "novelty_toys": "other",
        "pinball_machines_accessories": "other",
        "pinball_machines_arcade_games": "other",
        "playing_cards": "other",
        "radio_control": "other",
        "radio_control_rc_toys": "other",
        "skill_toys_juggling": "other",
        "slot_cars_racing": "other",
        "stuffed_toys_novelties": "other",
        "trading_cards": "other",
    },
    "health_beauty": {
        "nail_care": "personal_care",
        "nail_art": "personal_care",
        "dental": "other",
        "dental_care": "other",
        "dental_instruments": "other",
        "dental_supplies": "other",
        "medical_supplies": "other",
        "medical_equipment": "other",
        "medical_services": "other",
        "contact_lenses": "other",
        "eyewear": "other",
        "fitness": "other",
        "jewelry": "other",
        "lip_care": "personal_care",
        "mobility_aids": "other",
        "natural_beauty": "skincare",
        "wellness": "other",
        "yoga": "other",
    },
    "home_garden": {
        "cleaning_supplies": "other",
        "flooring": "other",
        "laundry": "other",
        "appliances": "other",
        "blogscontent": "other",
        "building_materials": "other",
        "fabrics": "other",
        "home_improvement": "other",
        "hvac_equipment": "other",
        "lighting": "other",
        "pest_control": "other",
        "plumbing": "other",
        "storage_organization": "other",
        "sustainable_living": "other",
    },
    "business_industrial": {
        "marketing_services": "other",
        "financial_services": "other",
        "business_services": "other",
        "insurance_services": "other",
        "advertising_solutions": "other",
        "business_software": "other",
        "commercial_real_estate_services": "other",
        "communications_platform_as_a_service_cpaas": "other",
        "conferences_events": "other",
        "consulting_services": "other",
        "digital_solutions": "other",
        "domain_services": "other",
        "legal_services": "other",
        "marketing_communications_services": "other",
        "professional_services": "other",
        "software_development_services": "other",
    },
    "electronics": {
        "appliances": "other",
        "semiconductors": "other",
        "smart_home": "other",
        "components": "other",
        "components_accessories": "other",
        "components_parts": "other",
        "components_supplies": "other",
        "fishing_electronics": "other",
        "gps_hardware_systems": "other",
        "measurement_testing_equipment": "other",
    },
    "sporting_goods": {
        "archery": "other",
        "fishing": "other",
        "golf": "other",
        "roller_skates": "other",
        "running": "other",
        "sailing": "other",
        "shooting": "other",
        "skateboarding": "other",
        "surfing": "other",
        "tents": "other",
        "water_sports": "other",
        "equestrian": "other",
        "camping_hiking": "camping",
    },
    "arts_entertainment": {
        "activities_experiences": "other",
        "art_supplies": "art",
        "dance_classes": "other",
        "entertainment": "other",
        "event_tickets_experiences": "other",
        "events_entertainment": "other",
        "music": "music_instruments",
    },
    "baby_toddler": {
        "bath_products": "other",
        "diaper_care": "other",
        "diapers": "other",
        "gifts": "other",
        "safety_health": "other",
        "shoes": "other",
        "skincare": "other",
    },
    "cameras_optics": {
        "telescopes": "other",
    },
    "luggage_bags": {
        "bags": "other",
        "briefcases": "other",
        "handbag_liners": "other",
        "motorcycle_luggage": "other",
    },
    "apparel_accessories": {
        "accessories": "other",
        "custom_apparel": "clothing",
        "hair_accessories": "other",
        "hats": "other",
    },
    "media": {
        # media 下非标准子分类较少，仅 movies_music_games_software_audio_books
        "movies_music_games_software_audio_books": "other",
    },
}


def _is_multi_subcategory_name(category: str, sub: str) -> bool:
    """判断集合名中的 sub 是否为多个标准子分类的拼接（MULTI）

    旧逻辑用 source_subcategory 字段含逗号判断，但 "Food, Beverages & Tobacco"
    一级分类名本身含逗号，导致该类目下所有单子分类集合被误判为 MULTI。

    新逻辑：用集合名本身判断。若 sub 本身是标准子分类则非 MULTI；
    否则检查 sub 是否由 2+ 个标准子分类名用 '_' 拼接而成。

    Args:
        category: 一级分类名
        sub: 集合名中的二级分类片段（标准化后）

    Returns:
        True 表示是 MULTI 拼接集合
    """
    if sub in get_standard_subcategories(category):
        return False
    std_subs = STANDARD_SUBCATEGORIES.get(category, [])
    # 检查 sub 是否包含 2+ 个标准子分类名作为 '_' 分隔的子串
    # 例如 "clothing_shoes_jewelry_handbags" 含 clothing/shoes/jewelry/handbags
    sub_tokens = sub.split("_")
    matched = 0
    for std in std_subs:
        std_tokens = std.split("_")
        # 检查 std_tokens 是否为 sub_tokens 的连续子序列
        n, m = len(sub_tokens), len(std_tokens)
        if m > n:
            continue
        for i in range(n - m + 1):
            if sub_tokens[i:i + m] == std_tokens:
                matched += 1
                break
    return matched >= 2


def resolve_target_subcategory(category: str, current_sub: str, is_multi: bool) -> str:
    """解析当前子分类应映射到的标准子分类名

    Args:
        category: 一级分类名
        current_sub: 当前子分类名（标准化后）
        is_multi: 是否为多子分类拼接集合

    Returns:
        目标标准子分类名
    """
    if is_multi:
        return "other"

    # 已在标准列表中，无需迁移
    if current_sub in get_standard_subcategories(category):
        return current_sub

    # 查同义词映射
    syn = SYNONYM_MAP.get(category, {}).get(current_sub)
    if syn:
        return syn

    # 兜底 -> other
    return "other"


def scan_database(db) -> list[dict]:
    """扫描数据库，生成迁移计划

    返回: [{"col_name", "category", "current_sub", "target_sub", "doc_count", "is_multi"}]
    """
    existing = set(db.list_collection_names())
    plans = []
    for name in sorted(existing):
        if name.startswith("system."):
            continue
        if CATEGORY_SEPARATOR not in name:
            continue
        # 排除旧后缀集合
        if name.endswith(("_filtered", "_crawled", "_unfiltered", "_raw", "_clean", "_export")):
            continue

        parts = name.split(CATEGORY_SEPARATOR, 1)
        category = parts[0]
        current_sub = parts[1] if len(parts) > 1 else "other"
        current_sub = current_sub or "other"

        doc_count = db[name].count_documents({})
        if doc_count == 0:
            continue

        # 判断是否多子分类拼接：用集合名本身判断，而非 source_subcategory 字段
        # （旧逻辑用 "," in source_subcategory 会误判 Food, Beverages & Tobacco 类目）
        is_multi = _is_multi_subcategory_name(category, current_sub)

        target_sub = resolve_target_subcategory(category, current_sub, is_multi)

        # 目标与当前相同则无需迁移
        if target_sub == current_sub and not is_multi:
            continue

        plans.append({
            "col_name": name,
            "category": category,
            "current_sub": current_sub,
            "target_sub": target_sub,
            "doc_count": doc_count,
            "is_multi": is_multi,
        })
    return plans


def ensure_indexes(db, col_name: str):
    """为目标集合创建索引（与 mongodb.py ensure_indexes 对齐）"""
    col = db[col_name]
    col.create_index(
        [("domain", ASCENDING), ("collection_handle", ASCENDING)],
        unique=True, name="idx_domain_collection",
    )
    col.create_index([("filter_status", ASCENDING)], name="idx_filter_status")
    col.create_index([("crawl_status", ASCENDING)], name="idx_crawl_status")
    col.create_index(
        [("category", ASCENDING), ("subcategory", ASCENDING)],
        name="idx_category_subcategory",
    )
    col.create_index([("created_at", ASCENDING)], name="idx_created_at")


def merge_collection(db, src_name: str, target_name: str, target_sub: str, dry_run: bool):
    """将 src 集合的文档合并到 target 集合，更新 subcategory 字段，然后删除 src"""
    src = db[src_name]
    target = db[target_name]

    if dry_run:
        return

    # 逐条迁移（upsert，按 domain+collection_handle 去重）
    migrated = 0
    for doc in src.find({}):
        doc.pop("_id", None)
        doc["subcategory"] = target_sub

        result = target.update_one(
            {"domain": doc.get("domain"), "collection_handle": doc.get("collection_handle", "")},
            {"$set": doc},
            upsert=True,
        )
        if result.upserted_id or result.modified_count > 0:
            migrated += 1

    # 删除源集合
    db.drop_collection(src_name)
    log.info(f"  合并 {src_name} -> {target_name}: {migrated} 条文档，源集合已删除")


def migrate_database(db, db_name: str, dry_run: bool):
    """迁移单个数据库的集合"""
    plans = scan_database(db)

    if not plans:
        log.info(f"[{db_name}] 没有需要迁移的集合")
        return

    # 按目标集合分组统计
    target_groups: dict[str, list[dict]] = {}
    for p in plans:
        target_name = make_collection_prefix(p["category"], p["target_sub"])
        target_groups.setdefault(target_name, []).append(p)

    log.info(f"[{db_name}] {'[DRY-RUN] ' if dry_run else ''}共 {len(plans)} 个集合需要迁移，"
             f"涉及 {len(target_groups)} 个目标集合:")

    for target_name, group in sorted(target_groups.items()):
        total_docs = sum(g["doc_count"] for g in group)
        log.info(f"  -> {target_name} (接收 {total_docs} 条文档，来自 {len(group)} 个源集合)")
        for g in group:
            tag = "MULTI" if g["is_multi"] else "SYNONYM"
            log.info(f"      [{tag}] {g['col_name']} ({g['doc_count']} docs)")

    if dry_run:
        log.info(f"[{db_name}] dry-run 模式，未做任何修改。加 --execute 参数执行实际迁移。")
        return

    # 实际执行
    for target_name, group in sorted(target_groups.items()):
        # 确保目标集合索引存在
        ensure_indexes(db, target_name)

        for g in group:
            src_name = g["col_name"]
            if src_name == target_name:
                # 源与目标同名（理论上不应发生，因为 scan 时已排除）
                # 但 is_multi 的源集合名可能恰好等于 other 目标名
                # 此时需要先更新文档 subcategory 字段，不删除集合
                db[src_name].update_many({}, {"$set": {"subcategory": g["target_sub"]}})
                log.info(f"  原地更新 {src_name}: subcategory -> {g['target_sub']}")
                continue
            merge_collection(db, src_name, target_name, g["target_sub"], dry_run=False)

    log.info(f"[{db_name}] 迁移完成")


def check_alignment(url_db, product_db):
    """检查两个库的集合名对齐情况"""
    url_cols = {n for n in url_db.list_collection_names() if CATEGORY_SEPARATOR in n}
    product_cols = {n for n in product_db.list_collection_names() if CATEGORY_SEPARATOR in n}

    only_in_product = product_cols - url_cols
    only_in_url = url_cols - product_cols

    log.info(f"[对齐校验] qmds_url_stores: {len(url_cols)} 个 __ 集合, "
             f"qmds_product_data: {len(product_cols)} 个 __ 集合")

    if only_in_product:
        log.warning(f"  qmds_product_data 中有 {len(only_in_product)} 个孤立集合（url_stores 中无对应）:")
        for n in sorted(only_in_product):
            log.warning(f"    {n}")
    if only_in_url:
        log.info(f"  qmds_url_stores 中有 {len(only_in_url)} 个集合暂无 product_data 对应（正常，未爬取）")

    if not only_in_product:
        log.info("  product_data 集合与 url_stores 完全对齐")


def main():
    parser = argparse.ArgumentParser(description="MongoDB 集合命名迁移脚本")
    parser.add_argument("--execute", action="store_true", help="实际执行迁移（默认 dry-run）")
    parser.add_argument("--skip-product-data", action="store_true", help="跳过 qmds_product_data 迁移")
    args = parser.parse_args()

    setup_logger()
    dry_run = not args.execute

    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")

    url_db = client[settings.mongo_db_url]

    if dry_run:
        log.info("=" * 60)
        log.info("DRY-RUN 模式：仅预览迁移计划，不修改任何数据")
        log.info("=" * 60)
    else:
        log.info("=" * 60)
        log.info("EXECUTE 模式：将实际迁移数据（建议已备份）")
        log.info("=" * 60)

    # 1. 迁移 qmds_url_stores
    log.info("")
    log.info(">>> 扫描 qmds_url_stores ...")
    migrate_database(url_db, settings.mongo_db_url, dry_run)

    # 2. 迁移 qmds_product_data（如不跳过）
    if not args.skip_product_data:
        product_db_name = "qmds_product_data"
        product_db = client[product_db_name]
        log.info("")
        log.info(f">>> 扫描 {product_db_name} ...")
        migrate_database(product_db, product_db_name, dry_run)

    # 3. 两库对齐校验
    log.info("")
    log.info(">>> 集合对齐校验 ...")
    check_alignment(url_db, client["qmds_product_data"])

    log.info("")
    log.info("全部完成")


if __name__ == "__main__":
    main()
