"""待确认站点重新检测测试

验证 recheck_uncertain：
- 确认 Shopify → filter_status 更新为 unfiltered（转正）
- 明确非 Shopify → 更新为 not_shopify
- 仍无法确认 → 保留 uncertain，记录 rechecked_at
"""

import threading
import uuid

import pytest

from qmds.modules.data_scraper import engine as engine_mod
from qmds.modules.data_scraper.detection.platform import DetectionResult
from qmds.modules.data_scraper.engine import DataScraperModule
from qmds.modules.data_scraper.models.schemas import Platform
from qmds.db.mongodb import FILTER_STATUS_NOT_SHOPIFY, FILTER_STATUS_UNFILTERED


class FakeCol:
    """最小化 fake 集合：find 返回文档，update_one 应用 $set 到内存文档"""

    def __init__(self, docs):
        self.docs = docs  # list[dict]（含 _id）
        self.calls = []

    def find(self, query, limit=None):
        matched = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if limit:
            matched = matched[:limit]
        return list(matched)

    def update_one(self, filt, update):
        self.calls.append((filt, update))
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                d.update(update.get("$set", {}))


class FakeDB:
    def __init__(self, docs):
        self.col = FakeCol(docs)
        self.closed = False

    def unfiltered_col(self, category):
        return self.col

    def close(self):
        self.closed = True


def make_doc(domain, status="uncertain"):
    return {"_id": f"id-{domain}", "domain": domain, "url": f"https://{domain}",
            "platform": "Blocked/待确认", "product_count": 0, "filter_status": status}


class FakeDetector:
    """按 URL 返回固定检测结果的假检测器"""

    def __init__(self, url_map):
        self.url_map = url_map  # url -> "shopify" | "none" | "inconclusive"

    def detect(self, url, url_map_arg=None):
        state = self.url_map.get(url, "none")
        if state == "shopify":
            return DetectionResult(platform=Platform.SHOPIFY, product_count=123,
                                   store_name="Demo", currency="USD", confidence=0.95)
        if state == "inconclusive":
            return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)
        return DetectionResult(platform=Platform.UNKNOWN)


def build_module(fake_db, url_map):
    module = DataScraperModule.__new__(DataScraperModule)
    module._lock = threading.Lock()
    module._executor = None
    module._search_executor = None
    module.detector = FakeDetector(url_map)
    return module


def test_shopify_confirmed_promotes_to_unfiltered(monkeypatch):
    db = FakeDB([make_doc("shop-a.com")])
    monkeypatch.setattr(engine_mod, "MongoDBClient", lambda: db)
    module = build_module(db, {"https://shop-a.com": "shopify"})
    result = module.recheck_uncertain("media", workers=1)
    assert result["shopify"] == 1
    doc = db.col.docs[0]
    assert doc["filter_status"] == FILTER_STATUS_UNFILTERED
    assert doc["platform"] == "Shopify"
    assert doc["product_count"] == 123
    assert doc["store_name"] == "Demo"


def test_not_shopify_marked(monkeypatch):
    db = FakeDB([make_doc("plain.com")])
    monkeypatch.setattr(engine_mod, "MongoDBClient", lambda: db)
    module = build_module(db, {"https://plain.com": "none"})
    result = module.recheck_uncertain("media", workers=1)
    assert result["not_shopify"] == 1
    doc = db.col.docs[0]
    assert doc["filter_status"] == FILTER_STATUS_NOT_SHOPIFY
    assert doc["platform"] == "Not Shopify"


def test_still_inconclusive_kept(monkeypatch):
    db = FakeDB([make_doc("blocked.com")])
    monkeypatch.setattr(engine_mod, "MongoDBClient", lambda: db)
    module = build_module(db, {"https://blocked.com": "inconclusive"})
    result = module.recheck_uncertain("media", workers=1)
    assert result["still_uncertain"] == 1
    doc = db.col.docs[0]
    assert doc["filter_status"] == "uncertain"  # 保留
    assert "rechecked_at" in doc


def test_empty_noop(monkeypatch):
    db = FakeDB([])
    monkeypatch.setattr(engine_mod, "MongoDBClient", lambda: db)
    module = build_module(db, {})
    result = module.recheck_uncertain("media", workers=1)
    assert result["total"] == 0
    assert db.closed


def test_limit_respected(monkeypatch):
    db = FakeDB([make_doc(f"site{i}.com") for i in range(10)])
    monkeypatch.setattr(engine_mod, "MongoDBClient", lambda: db)
    module = build_module(db, {})
    result = module.recheck_uncertain("media", limit=3, workers=1)
    assert result["total"] == 3
    assert result["not_shopify"] == 3
