# -*- coding: utf-8 -*-
"""手动筛选精准类目 - 人工审核（批量打开网站 / 保留 / 移到其他类目）单元测试

覆盖：
- filtered_review 纯函数：审核队列构建、移动目标校验与归一化、筛选参数归一化；
- MongoDBClient.mark_filtered_review / move_filtered_records：
  用内存假集合验证文档改写与 _counters 增减（不上真实数据库）。
"""

import sys
from pathlib import Path

import pytest
from bson import ObjectId

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.config.categories import make_collection_prefix
from qmds.db.mongodb import (
    CRAWL_STATUS_UNCRAWLED,
    REVIEW_STATUS_KEPT,
    REVIEW_STATUS_PENDING,
    MongoDBClient,
)
from qmds.modules.web.services.filtered_review import (
    build_review_queue,
    normalize_review_filter,
    resolve_move_target,
    split_target_shopify_category,
)


# ── 内存假集合（只实现被测代码用到的子集） ──────────────────

class _Result:
    def __init__(self, upserted_id=None, modified_count=0, deleted_count=0):
        self.upserted_id = upserted_id
        self.modified_count = modified_count
        self.deleted_count = deleted_count


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, str):
            keys = [(key_or_list, 1 if direction is None else direction)]
        else:
            keys = list(key_or_list)
        for key, direction in reversed(keys):
            self._docs.sort(key=lambda d: (d.get(key) is None, str(d.get(key))),
                            reverse=direction == -1)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _match(doc, query):
        for key, cond in (query or {}).items():
            if key == "$or":
                if not any(FakeCollection._match(doc, sub) for sub in cond):
                    return False
                continue
            if isinstance(cond, dict):
                if "$in" in cond:
                    if doc.get(key) not in cond["$in"]:
                        return False
                elif "$ne" in cond:
                    if doc.get(key) == cond["$ne"]:
                        return False
                elif "$exists" in cond:
                    if (key in doc) != bool(cond["$exists"]):
                        return False
                elif doc.get(key) != cond:
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    def find(self, query=None, projection=None):
        return _Cursor([d for d in self.docs if self._match(d, query)])

    def find_one(self, query=None, projection=None):
        for d in self.docs:
            if self._match(d, query):
                return d
        return None

    def count_documents(self, query=None):
        return len([d for d in self.docs if self._match(d, query)])

    def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return _Result(modified_count=1)
        if not upsert:
            return _Result()
        new_doc = dict(query)
        new_doc["_id"] = ObjectId()
        new_doc.update(update.get("$set", {}))
        self.docs.append(new_doc)
        return _Result(upserted_id=new_doc["_id"], modified_count=0)

    def update_many(self, query, update):
        count = 0
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                count += 1
        return _Result(modified_count=count)

    def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._match(d, query):
                self.docs.pop(i)
                return _Result(deleted_count=1)
        return _Result()

    def delete_many(self, query):
        keep = [d for d in self.docs if not self._match(d, query)]
        deleted = len(self.docs) - len(keep)
        self.docs = keep
        return _Result(deleted_count=deleted)


class FakeMongoDB(MongoDBClient):
    """只保留被测方法所需的存储行为"""

    def __init__(self, collections=None):
        super().__init__()
        self.cols = {k: FakeCollection(v) for k, v in (collections or {}).items()}
        self.counter_incs = []

    def filtered_col(self, category, subcategory=""):
        prefix = make_collection_prefix(category, subcategory)
        return self.cols.setdefault(prefix, FakeCollection())

    def ensure_indexes(self, category, subcategory=""):
        return None

    def _inc_counters(self, collection_key, increments, doc_delta=0):
        self.counter_incs.append((collection_key, dict(increments), doc_delta))

    def _set_counter_type(self, collection_key, collection_type, category="", subcategory=""):
        return None


def _doc(domain, handle, **extra):
    doc = {
        "_id": ObjectId(),
        "domain": domain,
        "store_url": f"https://{domain}",
        "url": f"https://{domain}/collections/{handle}",
        "collection_title": handle.title(),
        "collection_handle": handle,
        "category": "hardware",
        "subcategory": "other",
        "filter_status": "filtered",
        "crawl_status": CRAWL_STATUS_UNCRAWLED,
    }
    doc.update(extra)
    return doc


# ── 纯函数 ─────────────────────────────────────────────

def test_build_review_queue_builds_openable_urls():
    docs = [
        _doc("a.com", "tools"),
        {"_id": ObjectId(), "domain": "b.com", "store_url": "https://b.com/",
         "collection_handle": "gadgets", "collection_title": ""},
        {"_id": ObjectId(), "domain": "c.com"},  # 无 URL，跳过
    ]
    queue = build_review_queue(docs)
    assert len(queue) == 2
    assert queue[0]["url"] == "https://a.com/collections/tools"
    assert queue[0]["title"] == "Tools"
    assert queue[0]["kept"] is False
    # store_url + handle 回退拼接，顺手去掉结尾斜杠
    assert queue[1]["url"] == "https://b.com/collections/gadgets"
    assert queue[1]["title"] == "gadgets"


def test_build_review_queue_marks_kept():
    queue = build_review_queue([_doc("a.com", "tools", review_status=REVIEW_STATUS_KEPT)])
    assert queue[0]["kept"] is True


def test_resolve_move_target_defaults_and_validation():
    assert resolve_move_target("hardware", "other", "electronics", "Audio") == ("electronics", "audio")
    # 一级分类留空 -> 沿用当前一级分类
    assert resolve_move_target("hardware", "other", "", "Tools") == ("hardware", "tools")
    # 二级分类留空 -> other
    assert resolve_move_target("hardware", "tools", "hardware", "") == ("hardware", "other")
    with pytest.raises(ValueError):
        resolve_move_target("hardware", "tools", "hardware", "tools")
    with pytest.raises(ValueError):
        resolve_move_target("", "other", "", "tools")


def test_normalize_review_filter_and_split_target():
    assert normalize_review_filter("kept") == "kept"
    assert normalize_review_filter("PENDING") == "pending"
    assert normalize_review_filter("bogus") == ""
    assert normalize_review_filter(None) == ""
    assert split_target_shopify_category("hardware__tools") == ("hardware", "tools")
    assert split_target_shopify_category("hardware") == ("hardware", "")


# ── 保留 / 取消保留 ─────────────────────────────────────

def test_mark_filtered_review_keeps_and_counter():
    pending = _doc("a.com", "tools")
    already = _doc("b.com", "gadgets", review_status=REVIEW_STATUS_KEPT)
    db = FakeMongoDB({"hardware__other": [pending, already]})

    result = db.mark_filtered_review("hardware", "other", [str(pending["_id"]), str(already["_id"])])

    assert result == {"marked": 1, "unchanged": 1}
    stored = db.filtered_col("hardware", "other").find_one({"_id": pending["_id"]})
    assert stored["review_status"] == REVIEW_STATUS_KEPT
    assert stored["review_time"]
    assert db.counter_incs == [("hardware__other", {"kept": 1}, 0)]


def test_mark_filtered_review_unkeep_removes_fields():
    kept = _doc("a.com", "tools", review_status=REVIEW_STATUS_KEPT)
    db = FakeMongoDB({"hardware__other": [kept]})

    result = db.mark_filtered_review("hardware", "other", [str(kept["_id"])], REVIEW_STATUS_PENDING)

    assert result == {"marked": 1, "unchanged": 0}
    stored = db.filtered_col("hardware", "other").find_one({"_id": kept["_id"]})
    assert "review_status" not in stored
    assert db.counter_incs == [("hardware__other", {"kept": -1}, 0)]


def test_mark_filtered_review_ignores_invalid_ids():
    doc = _doc("a.com", "tools")
    db = FakeMongoDB({"hardware__other": [doc]})
    assert db.mark_filtered_review("hardware", "other", ["not-an-id"]) == {"marked": 0, "unchanged": 0}
    assert db.counter_incs == []


# ── 移到其他类目 ────────────────────────────────────────

def test_move_filtered_records_moves_and_syncs_counters():
    keep_doc = _doc("a.com", "tools")
    kept_doc = _doc("b.com", "gadgets", review_status=REVIEW_STATUS_KEPT)
    db = FakeMongoDB({"hardware__other": [keep_doc, kept_doc]})

    result = db.move_filtered_records("hardware", [str(keep_doc["_id"]), str(kept_doc["_id"])],
                                      "other", "electronics", "Audio")

    assert result == {"moved": 2, "merged": 0, "errors": []}
    assert db.filtered_col("hardware", "other").count_documents({}) == 0
    target_docs = list(db.filtered_col("electronics", "audio").find({}))
    assert len(target_docs) == 2
    assert {d["subcategory"] for d in target_docs} == {"audio"}
    assert {d["category"] for d in target_docs} == {"electronics"}
    assert {d["moved_from"] for d in target_docs} == {"hardware__other"}

    # 目标新增 2 条（其中 1 条已保留）；源集合减 2 条
    assert ("electronics__audio", {"filtered": 1, "uncrawled": 1}, 1) in db.counter_incs
    assert ("electronics__audio", {"filtered": 1, "uncrawled": 1, "kept": 1}, 1) in db.counter_incs
    assert ("hardware__other", {"filtered": -1, "uncrawled": -1}, -1) in db.counter_incs
    assert ("hardware__other", {"filtered": -1, "uncrawled": -1, "kept": -1}, -1) in db.counter_incs


def test_move_filtered_records_merges_into_existing_target():
    source = _doc("a.com", "tools")
    existing = _doc("a.com", "tools")
    existing["_id"] = ObjectId()
    db = FakeMongoDB({"hardware__other": [source], "electronics__tools": [existing]})

    result = db.move_filtered_records("hardware", [str(source["_id"])], "other", "electronics", "tools")

    assert result == {"moved": 0, "merged": 1, "errors": []}
    target_docs = list(db.filtered_col("electronics", "tools").find({}))
    assert len(target_docs) == 1  # 未产生重复记录
    assert db.filtered_col("hardware", "other").count_documents({}) == 0
    # 合并场景：目标不增计数，源照常减计数
    assert all(cnt[0] != "electronics__tools" for cnt in db.counter_incs)
    assert ("hardware__other", {"filtered": -1, "uncrawled": -1}, -1) in db.counter_incs


def test_move_filtered_records_rejects_same_target_and_missing_docs():
    doc = _doc("a.com", "tools")
    db = FakeMongoDB({"hardware__tools": [doc]})

    same = db.move_filtered_records("hardware", [str(doc["_id"])], "tools", "hardware", "tools")
    assert same["moved"] == 0 and same["merged"] == 0 and same["errors"]

    missing = db.move_filtered_records("hardware", [str(ObjectId())], "tools", "electronics", "tools")
    assert missing["moved"] == 0
    assert missing["errors"] and "不存在" in missing["errors"][0]
