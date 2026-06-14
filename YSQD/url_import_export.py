import os
from datetime import datetime
from io import BytesIO

from pymongo import MongoClient, UpdateOne


MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"


def get_mongo_client():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    return client, db


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url.rstrip("/")


def get_category_list():
    client, db = get_mongo_client()
    try:
        all_collections = db.list_collection_names()
        category_list = []
        for coll_name in all_collections:
            if coll_name.endswith("_Unfiltered_URLs"):
                category_list.append(coll_name.replace("_Unfiltered_URLs", ""))
        return sorted(category_list)
    finally:
        client.close()


def import_urls_from_txt_file(input_file, category_name):
    category_name = (category_name or "").strip()
    if not category_name:
        raise ValueError("分类名称不能为空")
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"没有找到文件: {input_file}")

    collection_name = f"{category_name}_Unfiltered_URLs"
    client, db = get_mongo_client()
    coll = db[collection_name]

    try:
        urls = set()
        with open(input_file, "r", encoding="utf-8") as handle:
            for line in handle:
                url = normalize_url(line)
                if url:
                    urls.add(url)

        if not urls:
            return {
                "collection_name": collection_name,
                "total_urls": 0,
                "new_count": 0,
                "skipped_count": 0,
            }

        existing_urls = set()
        for doc in coll.find({}, {"URL": 1, "_id": 0}):
            if doc.get("URL"):
                existing_urls.add(normalize_url(doc["URL"]))

        new_urls = [url for url in urls if url not in existing_urls]
        if not new_urls:
            return {
                "collection_name": collection_name,
                "total_urls": len(urls),
                "new_count": 0,
                "skipped_count": len(urls),
            }

        now = datetime.now()
        operations = []
        for url in sorted(new_urls):
            operations.append(
                UpdateOne(
                    {"URL": url},
                    {
                        "$setOnInsert": {
                            "URL": url,
                            "CreatedAt": now,
                            "FirstUsedAt": None,
                            "LastUsedAt": None,
                            "NextReusableAt": None,
                            "UseCount": 0,
                            "Status": "new",
                        }
                    },
                    upsert=True,
                )
            )

        result = coll.bulk_write(operations)
        return {
            "collection_name": collection_name,
            "total_urls": len(urls),
            "new_count": result.upserted_count,
            "skipped_count": len(urls) - len(new_urls),
        }
    finally:
        client.close()


def export_urls_to_memory(category_name):
    category_name = (category_name or "").strip()
    if not category_name:
        raise ValueError("分类名称不能为空")

    collection_name = f"{category_name}_Unfiltered_URLs"
    client, db = get_mongo_client()
    coll = db[collection_name]

    try:
        urls = set()
        for doc in coll.find({}, {"URL": 1, "_id": 0}):
            url = doc.get("URL")
            if url:
                normalized = normalize_url(url)
                if normalized:
                    urls.add(normalized)

        if not urls:
            raise ValueError(f"集合 {collection_name} 中没有可导出的 URL")

        content = "\n".join(sorted(urls)) + "\n"
        buffer = BytesIO(content.encode("utf-8"))
        buffer.seek(0)
        return buffer, len(urls), collection_name
    finally:
        client.close()
