from pymongo import MongoClient, ASCENDING, DESCENDING

MONGO_URI = "mongodb://localhost:27017/"
SHOPIFY_URL_DB = "shopify_url"
SHOPIFY_DATA_DB = "shopify_data_new"


def ensure_index(collection, keys, name=None, **kwargs):
    existing = collection.index_information()
    new_keys = list(keys) if isinstance(keys, list) else [(keys, ASCENDING)]
    for idx_name, idx_info in existing.items():
        if idx_info["key"] == new_keys:
            if idx_name != name:
                pass
            return
    collection.create_index(keys, name=name, **kwargs)


def ensure_shopify_url_indexes(db=None):
    close = db is None
    if db is None:
        client = MongoClient(MONGO_URI)
        db = client[SHOPIFY_URL_DB]
    else:
        client = None

    for name in db.list_collection_names():
        if name.endswith("_Unfiltered_URLs") or name.endswith("_Filtered_URLs"):
            coll = db[name]
            ensure_index(coll, "URL", name="idx_url")
            ensure_index(coll, "Domain", name="idx_domain")
        elif name.endswith("_Reuse_Queue"):
            coll = db[name]
            ensure_index(coll, "URL", name="idx_url")
            ensure_index(coll, "Status", name="idx_status")
            ensure_index(coll, "LastMatched", name="idx_last_matched")
            ensure_index(coll, "UpdatedAt", name="idx_updated_at")
            ensure_index(coll, "NextReusableAt", name="idx_next_reusable_at")
            ensure_index(
                coll,
                [("Status", ASCENDING), ("NextReusableAt", ASCENDING)],
                name="idx_status_next_reusable",
            )
        elif name == "crawler_domain_blacklist":
            coll = db[name]
            ensure_index(coll, "URL", name="idx_url")

    if close and client:
        client.close()


def ensure_shopify_data_indexes(db=None):
    close = db is None
    if db is None:
        client = MongoClient(MONGO_URI)
        db = client[SHOPIFY_DATA_DB]
    else:
        client = None

    for name in db.list_collection_names():
        coll = db[name]
        for field in ("unique_key", "source_url", "crawl_time"):
            ensure_index(coll, field, name=f"idx_{field}")

    if close and client:
        client.close()


def ensure_all_indexes():
    ensure_shopify_url_indexes()
    ensure_shopify_data_indexes()
