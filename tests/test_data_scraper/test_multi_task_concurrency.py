"""多任务并发爬取 测试

验证两个爬取任务同时运行时：
- 站点并发被模块级 _site_concurrency（MAX_CONCURRENT_SITES=12）限制，
  两个任务合计不超过 12 个站点同时在爬（不会各开各的导致 16+ 并发）
- 任务都能正常完成、统计正确
"""

import threading
import time

import pytest

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler


class FakeCol:
    def __init__(self, sites):
        self.sites = sites

    def find(self, query, projection):
        return list(self.sites)


class FakeMongo:
    def __init__(self, sub_sites):
        self.sub_sites = sub_sites

    def list_filtered_subcategories(self, category):
        return list(self.sub_sites.keys())

    def filtered_col(self, category, subcategory):
        return FakeCol(self.sub_sites.get(subcategory, []))

    def close(self):
        pass


class FakePDB:
    def ensure_product_indexes(self, *a, **k):
        pass

    def close(self):
        pass


def make_sites(prefix, n):
    return [{"domain": f"{prefix}{i}.com", "url": f"https://{prefix}{i}.com",
             "store_url": f"https://{prefix}{i}.com"} for i in range(n)]


def test_two_tasks_share_site_slot_cap(monkeypatch):
    """两个任务同时爬：总站点并发 ≤ MAX_CONCURRENT_SITES，且确实并发执行"""
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    class FakeCrawler:
        def crawl_site(self, *a, **k):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.03)  # 模拟爬取耗时，制造并发窗口
            with lock:
                state["active"] -= 1
            return {"success": True, "products": [], "count": 0}

        def close(self):
            pass

    monkeypatch.setattr(pc_mod, "create_crawler", lambda: FakeCrawler())
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakePDB())
    sub_sites = {"art": make_sites("a", 8), "music": make_sites("m", 8)}
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: FakeMongo(sub_sites))

    module = ProductCrawler.__new__(ProductCrawler)
    module._mark_crawled = lambda *a, **k: None
    module._retry_blocked_sites = lambda *a, **k: (0, 0, {})

    results = []

    def run():
        results.append(module.crawl_category_all_subcategories("Home", workers=8))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    cap = pc_mod.MAX_CONCURRENT_SITES
    assert state["max"] <= cap, f"两个任务合计并发 {state['max']} 超过全局上限 {cap}"
    assert state["max"] >= 2, "两个任务应并发执行（否则说明没并行）"
    # 两个任务各自统计正确（每个任务 16 个站点）
    assert len(results) == 2
    for r in results:
        assert r["total_sites"] == 16
        assert r["success_sites"] == 16


def test_site_concurrency_acquired_and_released(monkeypatch):
    """站点名额在任务结束后全部释放（无泄漏）"""
    # 任务前名额全空
    assert pc_mod._site_concurrency._value == pc_mod.MAX_CONCURRENT_SITES
    module = ProductCrawler.__new__(ProductCrawler)
    assert module._acquire_site_slot() is True  # 获取一个
    assert pc_mod._site_concurrency._value == pc_mod.MAX_CONCURRENT_SITES - 1
    pc_mod._site_concurrency.release()
    assert pc_mod._site_concurrency._value == pc_mod.MAX_CONCURRENT_SITES
