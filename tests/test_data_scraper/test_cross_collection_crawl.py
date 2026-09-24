"""跨集合并发爬取测试

验证 crawl_category_all_subcategories 重构：
- 多个二级分类的站点合并进全局线程池同时爬（不再等一个集合爬完再爬下一个）
- 总并发线程数 = workers（不超过 workers）
- 所有集合的站点都被处理，统计正确
- workers=1 时仍串行（max_active=1）
"""

import threading
import time

import pytest

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler


class FakeFilteredCol:
    """返回预置站点列表的假集合（忽略查询条件）"""

    def __init__(self, sites):
        self.sites = sites

    def find(self, query, projection):
        return list(self.sites)


class FakeSourceDB:
    """假 MongoDBClient：list_filtered_subcategories + filtered_col + close"""

    def __init__(self, sub_sites):
        self.sub_sites = sub_sites  # {sub_norm: [site dict]}
        self.closed = False

    def list_filtered_subcategories(self, category):
        return list(self.sub_sites.keys())

    def filtered_col(self, category, subcategory):
        return FakeFilteredCol(self.sub_sites.get(subcategory, []))

    def close(self):
        self.closed = True


class FakeProductDB:
    def ensure_product_indexes(self, *a, **k):
        pass

    def close(self):
        pass


def build_module(fake_crawl_site, workers=3):
    module = ProductCrawler.__new__(ProductCrawler)
    module._crawl_single_site = fake_crawl_site
    module._mark_crawled = lambda *a, **k: None
    module._retry_blocked_sites = lambda *a, **k: (0, 0, {})
    return module


def make_sites(prefix, n):
    return [{"domain": f"{prefix}{i}.com", "url": f"https://{prefix}{i}.com",
             "store_url": f"https://{prefix}{i}.com"} for i in range(n)]


def test_cross_collection_parallel(monkeypatch):
    """多个集合的站点应同时并发爬取，总并发不超过 workers"""
    sub_sites = {
        "art": make_sites("art", 4),
        "music": make_sites("music", 4),
        "crafts": make_sites("crafts", 4),
    }
    fdb = FakeSourceDB(sub_sites)
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: fdb)
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakeProductDB())

    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    crawled = []  # (subcategory, domain)

    def fake_crawl_site(url_doc, category, site_index, total_sites,
                        progress_callback=None, stop_event=None, subcategory=""):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)  # 制造并发窗口，让多个集合的站点真正同时执行
        crawled.append((subcategory, url_doc["domain"]))
        with lock:
            state["active"] -= 1
        return {"success": True, "saved": 5, "url": url_doc["url"], "domain": url_doc["domain"]}

    module = build_module(fake_crawl_site)
    result = module.crawl_category_all_subcategories("arts_entertainment", workers=3)

    assert result["total_sites"] == 12
    assert result["success_sites"] == 12
    assert result["total_products"] == 60
    # 跨集合并发：同时活跃站点数 > 1（不再是集合级串行）
    assert state["max_active"] > 1, f"应为跨集合并发，但 max_active={state['max_active']}"
    # 总并发不超过 workers（线程总数不变）
    assert state["max_active"] <= 3, f"总并发 {state['max_active']} 不应超过 workers=3"
    # 所有集合的站点都被处理
    assert {s for s, _ in crawled} == {"art", "music", "crafts"}
    assert len(crawled) == 12


def test_serial_when_workers_one(monkeypatch):
    """workers=1 时仍是串行执行（max_active=1）"""
    sub_sites = {
        "art": make_sites("art", 3),
        "music": make_sites("music", 3),
    }
    fdb = FakeSourceDB(sub_sites)
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: fdb)
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakeProductDB())

    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def fake_crawl_site(url_doc, category, site_index, total_sites,
                        progress_callback=None, stop_event=None, subcategory=""):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return {"success": True, "saved": 1, "url": url_doc["url"], "domain": url_doc["domain"]}

    module = build_module(fake_crawl_site)
    result = module.crawl_category_all_subcategories("arts_entertainment", workers=1)
    assert result["total_sites"] == 6
    assert state["max_active"] == 1


def test_no_sites_returns_error(monkeypatch):
    fdb = FakeSourceDB({"art": [], "music": []})
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: fdb)
    module = build_module(lambda *a, **k: None)
    result = module.crawl_category_all_subcategories("arts_entertainment", workers=2)
    assert result["total_sites"] == 0
    assert "error" in result


def test_max_sites_per_subcategory(monkeypatch):
    """max_sites 按每个集合分别截断"""
    sub_sites = {
        "art": make_sites("art", 5),
        "music": make_sites("music", 5),
    }
    fdb = FakeSourceDB(sub_sites)
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: fdb)
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakeProductDB())

    done = []

    def fake_crawl_site(url_doc, category, site_index, total_sites,
                        progress_callback=None, stop_event=None, subcategory=""):
        done.append(url_doc["domain"])
        return {"success": True, "saved": 1, "url": url_doc["url"], "domain": url_doc["domain"]}

    module = build_module(fake_crawl_site)
    result = module.crawl_category_all_subcategories("arts_entertainment", max_sites=2, workers=2)
    assert result["total_sites"] == 4  # 每集合最多 2 个
    assert result["success_sites"] == 4
