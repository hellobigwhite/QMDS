import os
import re
from datetime import datetime

import pandas as pd
from pymongo import MongoClient

from product_processing_v2 import (
    get_basic_delete_reason,
    get_non_english_reason,
    has_bad_image,
    parse_price,
    strip_html,
    MIN_PRICE,
    MAX_PRICE,
)
from product_processing_v2 import IMAGE_FIELD


MONGO_URI = "mongodb://localhost:27017/"
SOURCE_DB_NAME = "shopify_data_new"
BACKUP_DB_NAME = "shopify_data_backup"
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data", "商品导出")
TITLE_FIELD = "标题"
DESC_FIELD = "描述"
CATEGORY_FIELD = "分类"

REQUIRED_COLUMNS = ["SKU", "标题", "描述", "子描述", "图片", "原价", "折扣价", "变体名", "变体值", "分类"]
BACKUP_BATCH_SIZE = 1000
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]+")


class ExportStopRequested(Exception):
    pass


def _raise_if_stop_requested(stop_callback=None):
    if stop_callback and stop_callback():
        raise ExportStopRequested("Stop requested")


def ensure_export_dir(export_dir=EXPORT_DIR):
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


def list_export_files(limit=20, export_dir=EXPORT_DIR):
    ensure_export_dir(export_dir)
    items = []
    for name in os.listdir(export_dir):
        if not name.lower().endswith(".xlsx"):
            continue
        full_path = os.path.join(export_dir, name)
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
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    return text or "export"


def sanitize_excel_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = CONTROL_CHAR_RE.sub("", text)
    if len(text) > 32760:
        text = text[:32760] + "...(truncated)"
    return text


def category_match(category_name):
    return {
        "$expr": {
            "$eq": [
                {"$trim": {"input": {"$ifNull": [f"${CATEGORY_FIELD}", ""]}}},
                category_name,
            ]
        }
    }


def list_collection_categories(collection):
    pipeline = [
        {
            "$project": {
                "category": {
                    "$trim": {
                        "input": {"$ifNull": [f"${CATEGORY_FIELD}", ""]},
                    }
                }
            }
        },
        {"$match": {"category": {"$ne": ""}}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]
    return [{"name": item["_id"], "count": int(item["count"])} for item in collection.aggregate(pipeline, allowDiskUse=True)]


def title_quality_sort_key(doc):
    title = strip_html(doc.get(TITLE_FIELD))
    desc = strip_html(doc.get(DESC_FIELD))
    category = doc.get(CATEGORY_FIELD, "")
    image = doc.get(IMAGE_FIELD, "")
    variant_name = doc.get("变体名", "")
    variant_value = doc.get("变体值", "")
    score = 0
    title_len = len(title)
    desc_len = len(desc)

    title_words = re.findall(r"[A-Za-z]{2,}", title)
    unique_words = set(w.lower() for w in title_words)
    word_count = len(title_words)
    unique_count = len(unique_words)

    if not title:
        return (-9999, 0, desc_len, str(doc.get("_id", "")))

    if word_count < 3:
        score -= 500
    elif word_count >= 5:
        score += 120
    elif word_count >= 3:
        score += 40

    if unique_count >= 4:
        score += 100
    elif unique_count >= 2:
        score += 40

    if 30 <= title_len <= 80:
        score += 180
    elif 20 <= title_len < 30:
        score += 60
    elif 80 < title_len <= 120:
        score += 20
    elif title_len < 20:
        score -= 300

    if not desc:
        score -= 600
    elif desc_len < 30:
        score -= 250
    elif desc_len < 60:
        score += 30
    elif desc_len < 150:
        score += 150
    else:
        score += 300

    basic_reason = get_basic_delete_reason(doc)
    if basic_reason == "":
        score += 80
    elif basic_reason in ("empty", "numeric_title"):
        score -= 400
    elif basic_reason == "short_title":
        score -= 300
    elif basic_reason == "bad_price":
        score -= 150

    non_english_reason = get_non_english_reason(doc)
    if non_english_reason == "":
        score += 150
    else:
        score -= 400

    if not has_bad_image(doc):
        if isinstance(image, list) and len(image) > 1:
            score += 150
        else:
            score += 60
    else:
        score -= 250

    price = parse_price(doc)
    if price is not None and MIN_PRICE <= price <= MAX_PRICE:
        score += 100
    elif price is not None:
        score -= 50
    else:
        score -= 100

    if category:
        score += 60

    if variant_name and variant_value:
        score += 80

    return score, title_len, desc_len, str(doc.get("_id", ""))


def allocate_category_limits(categories, total_limit, min_per_category, max_per_category):
    chosen = []
    remaining = total_limit

    for item in categories:
        if remaining < min_per_category:
            break
        base = min(min_per_category, item["count"], max_per_category)
        if base < min_per_category:
            continue
        chosen.append(
            {
                "name": item["name"],
                "count": item["count"],
                "allocated": base,
            }
        )
        remaining -= base

    for item in chosen:
        if remaining <= 0:
            break
        max_allowed = min(item["count"], max_per_category)
        extra_capacity = max_allowed - item["allocated"]
        if extra_capacity <= 0:
            continue
        extra = min(extra_capacity, remaining)
        item["allocated"] += extra
        remaining -= extra

    return chosen, remaining


def normalize_export_row(doc):
    row = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        if isinstance(value, list):
            row[key] = sanitize_excel_text(", ".join(str(item).strip() for item in value if str(item).strip()))
        elif value is None:
            row[key] = ""
        else:
            row[key] = sanitize_excel_text(value)
    for column in REQUIRED_COLUMNS:
        row.setdefault(column, "")
    ordered_columns = REQUIRED_COLUMNS + [key for key in row.keys() if key not in REQUIRED_COLUMNS]
    return row, ordered_columns


def export_rows_to_excel(rows, collection_name, export_dir=EXPORT_DIR):
    ensure_export_dir(export_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{clean_name(collection_name)}_{timestamp}_{len(rows)}条.xlsx"
    file_path = os.path.join(export_dir, file_name)

    normalized_rows = []
    column_order = list(REQUIRED_COLUMNS)
    for doc in rows:
        row, ordered_columns = normalize_export_row(doc)
        normalized_rows.append(row)
        for column in ordered_columns:
            if column not in column_order:
                column_order.append(column)

    df = pd.DataFrame(normalized_rows)
    if df.empty:
        raise ValueError("没有可导出的商品数据")
    df = df[column_order]
    df.to_excel(file_path, index=False)
    return file_path


def backup_and_delete_docs(source_collection, backup_collection, docs):
    if not docs:
        return 0
    deleted_total = 0
    for start in range(0, len(docs), BACKUP_BATCH_SIZE):
        chunk = docs[start : start + BACKUP_BATCH_SIZE]
        if not chunk:
            continue
        backup_collection.insert_many(chunk, ordered=False)
        ids = [doc["_id"] for doc in chunk]
        result = source_collection.delete_many({"_id": {"$in": ids}})
        deleted_total += int(result.deleted_count or 0)
    return deleted_total


def export_clean_collection_direct(
    collection_name,
    limit=0,
    logger=None,
    stop_callback=None,
    export_dir=EXPORT_DIR,
):
    """Export one *_clean collection, back it up, then delete exported docs."""
    if not collection_name:
        raise ValueError("请选择要导出的集合")
    if not collection_name.endswith("_clean"):
        raise ValueError("数据库直接导出商品只允许导出 shopify_data_new 下的 *_clean 集合")

    limit = int(limit or 0)
    if limit < 0:
        raise ValueError("导出数量不能小于 0")

    def log(message):
        if logger:
            logger(message)

    ensure_export_dir(export_dir)

    client = MongoClient(MONGO_URI)
    try:
        source_db = client[SOURCE_DB_NAME]
        backup_db = client[BACKUP_DB_NAME]
        source_collection = source_db[collection_name]
        backup_collection = backup_db[collection_name]

        if collection_name not in source_db.list_collection_names():
            raise ValueError(f"集合不存在: {collection_name}")

        total = source_collection.count_documents({})
        if total <= 0:
            raise ValueError(f"集合 {collection_name} 没有可导出的商品数据")

        export_count = min(total, limit) if limit > 0 else total
        log(
            f"开始数据库直接导出商品: {SOURCE_DB_NAME}.{collection_name}, "
            f"计划导出 {export_count}/{total} 条。"
        )

        _raise_if_stop_requested(stop_callback)
        cursor = source_collection.find({})
        if limit > 0:
            cursor = cursor.limit(limit)
        docs = list(cursor)
        if not docs:
            raise ValueError("没有可导出的商品数据")

        file_path = export_rows_to_excel(docs, collection_name, export_dir=export_dir)
        _raise_if_stop_requested(stop_callback)
        deleted_count = backup_and_delete_docs(source_collection, backup_collection, docs)

        log(f"导出文件已生成: {file_path}")
        log(f"已备份到 {BACKUP_DB_NAME}.{collection_name} 并删除源集合商品 {deleted_count} 条。")

        return {
            "collection_name": collection_name,
            "file_path": file_path,
            "exported_count": len(docs),
            "deleted_count": deleted_count,
            "source_total": total,
            "stopped": False,
        }
    except ExportStopRequested:
        log("收到停止请求，数据库直接导出已提前结束")
        return {
            "collection_name": collection_name,
            "file_path": "",
            "exported_count": 0,
            "deleted_count": 0,
            "source_total": 0,
            "stopped": True,
        }
    finally:
        client.close()


def export_collection_by_category_rules(
    collection_name,
    total_limit,
    min_per_category,
    max_per_category,
    logger=None,
    stop_callback=None,
    export_dir=EXPORT_DIR,
):
    if not collection_name:
        raise ValueError("请先选择要导出的集合")
    if total_limit <= 0:
        raise ValueError("总导出数量必须大于 0")
    if min_per_category <= 0 or max_per_category <= 0:
        raise ValueError("每个小分类的最小值和最大值必须大于 0")
    if min_per_category > max_per_category:
        raise ValueError("小分类最小值不能大于最大值")
    if total_limit < min_per_category:
        raise ValueError("总导出数量不能小于小分类最小值")

    def log(message):
        if logger:
            logger(message)

    ensure_export_dir(export_dir)

    client = MongoClient(MONGO_URI)
    try:
        source_db = client[SOURCE_DB_NAME]
        backup_db = client[BACKUP_DB_NAME]
        source_collection = source_db[collection_name]
        backup_collection = backup_db[collection_name]

        _raise_if_stop_requested(stop_callback)
        categories = list_collection_categories(source_collection)
        eligible = [item for item in categories if item["count"] >= min_per_category]
        skipped = [item for item in categories if item["count"] < min_per_category]

        if not eligible:
            raise ValueError("当前集合里没有满足最小值条件的小分类")

        chosen_categories, remaining = allocate_category_limits(
            eligible,
            total_limit=total_limit,
            min_per_category=min_per_category,
            max_per_category=max_per_category,
        )

        if not chosen_categories:
            raise ValueError("总导出数量不足以覆盖任何一个符合条件的小分类")

        log(
            f"集合 {collection_name}：共检测到 {len(categories)} 个小分类，"
            f"符合条件 {len(eligible)} 个，跳过 {len(skipped)} 个。"
        )
        log(
            f"已按商品数优先选中 {len(chosen_categories)} 个小分类，"
            f"目标导出 {sum(item['allocated'] for item in chosen_categories)} 条，剩余未分配 {remaining} 条。"
        )

        exported_docs = []
        category_summary = []

        for item in chosen_categories:
            _raise_if_stop_requested(stop_callback)
            category_name = item["name"]
            allocated = item["allocated"]
            docs = list(source_collection.find(category_match(category_name)))
            docs.sort(key=title_quality_sort_key, reverse=True)
            selected_docs = docs[:allocated]
            exported_docs.extend(selected_docs)
            category_summary.append(
                {
                    "name": category_name,
                    "count": item["count"],
                    "allocated": allocated,
                    "selected": len(selected_docs),
                }
            )
            log(
                f"小分类 {category_name}：库存 {item['count']}，"
                f"分配 {allocated}，按标题质量优先选出 {len(selected_docs)} 条。"
            )

        _raise_if_stop_requested(stop_callback)
        if not exported_docs:
            raise ValueError("没有可导出的商品数据")

        file_path = export_rows_to_excel(exported_docs, collection_name, export_dir=export_dir)
        _raise_if_stop_requested(stop_callback)
        deleted_count = backup_and_delete_docs(source_collection, backup_collection, exported_docs)

        log(f"导出文件已生成：{file_path}")
        log(f"已备份并删除 {deleted_count} 条商品。")

        return {
            "collection_name": collection_name,
            "file_path": file_path,
            "exported_count": len(exported_docs),
            "deleted_count": deleted_count,
            "eligible_category_count": len(eligible),
            "skipped_category_count": len(skipped),
            "selected_category_count": len(chosen_categories),
            "category_summary": category_summary,
            "stopped": False,
        }
    except ExportStopRequested:
        log("收到停止请求，分类导出已提前结束")
        return {
            "collection_name": collection_name,
            "file_path": "",
            "exported_count": 0,
            "deleted_count": 0,
            "eligible_category_count": 0,
            "skipped_category_count": 0,
            "selected_category_count": 0,
            "category_summary": [],
            "stopped": True,
        }
    finally:
        client.close()
