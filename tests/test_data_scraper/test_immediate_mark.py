"""每站爬完立即标记 crawl_status 测试

验证 _crawl_single_site：
- 成功站点 → 立即 _mark_crawled（不再等集合收尾）
- blocked（CF 拦截）→ 不标记，留待重试
- 停止信号 → 不标记，下次继续
- 真异常 → 立即标记失败（success=False），避免反复重试
"""

import threading

import pytest

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler


class FakeCrawler:
    def __init__(self, result=None, exc=None):
        self.result = result or {"success": True, "products": [], "count": 0}
        self.exc = exc

    def crawl_site(self, *a, **k):
        if self.exc:
            raise self.exc
        return self.result

    def close(self):
        pass


class FakeProductDB:
    def save_raw_products(self, *a, **k):
        return 0

    def close(self):
        pass


class FakeMongoDB:
    def close(self):
        pass


def build_module(monkeypatch, *, crawl_result=None, exc=None, stop_event=None):
    marked = []
    module = ProductCrawler.__new__(ProductCrawler)
    # 真正 acquire 全局站点信号量，保证 finally 里的 release 平衡
    module._acquire_site_slot = lambda *a, **k: pc_mod._site_concurrency.acquire(timeout=5)
    module._mark_crawled = lambda db, cat, crawled, subcategory="": marked.append((cat, crawled, subcategory))
    monkeypatch.setattr(pc_mod, "create_crawler", lambda: FakeCrawler(crawl_result, exc))
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakeProductDB())
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: FakeMongoDB())
    return module, marked


def test_success_marks_immediately(monkeypatch):
    """成功站点：爬完立即标记，不等集合收尾"""
    module, marked = build_module(monkeypatch, crawl_result={"success": True, "products": [], "count": 0})
    result = module._crawl_single_site({"url": "https://a.com", "domain": "a.com"}, "Home", 1, 1, subcategory="art")
    assert result["success"] is True
    assert len(marked) == 1
    cat, crawled, sub = marked[0]
    assert cat == "Home"
    assert sub == "art"
    assert "a.com" in crawled
    assert crawled["a.com"]["success"] is True


def test_blocked_not_marked(monkeypatch):
    """CF 拦截（blocked）：不标记，留待重试"""
    module, marked = build_module(monkeypatch,
        crawl_result={"success": False, "products": [], "count": 0, "blocked": True})
    result = module._crawl_single_site({"url": "https://b.com", "domain": "b.com"}, "Home", 1, 1)
    assert result["blocked"] is True
    assert marked == []


def test_stopped_not_marked(monkeypatch):
    """收到停止信号：不标记，下次继续"""
    stop_event = threading.Event()
    stop_event.set()
    module, marked = build_module(monkeypatch, stop_event=stop_event)
    result = module._crawl_single_site({"url": "https://c.com", "domain": "c.com"}, "Home", 1, 1,
                                       stop_event=stop_event)
    assert result.get("stopped") is True
    assert marked == []


def test_exception_marks_failed(monkeypatch):
    """真异常（非停止）：立即标记失败，避免反复重试"""
    module, marked = build_module(monkeypatch, exc=RuntimeError("boom"))
    result = module._crawl_single_site({"url": "https://d.com", "domain": "d.com"}, "Home", 1, 1)
    assert result["success"] is False
    assert len(marked) == 1
    _, crawled, _ = marked[0]
    assert crawled["d.com"]["success"] is False


def test_exception_during_stop_not_marked(monkeypatch):
    """异常但已收到停止信号：不标记"""
    stop_event = threading.Event()
    stop_event.set()
    module, marked = build_module(monkeypatch, exc=RuntimeError("boom"), stop_event=stop_event)
    module._crawl_single_site({"url": "https://e.com", "domain": "e.com"}, "Home", 1, 1,
                              stop_event=stop_event)
    assert marked == []


def test_failed_nonblocked_marked(monkeypatch):
    """非 Shopify/无商品等确定性失败（非 blocked 非 stopped）：标记失败避免反复爬"""
    module, marked = build_module(monkeypatch,
        crawl_result={"success": False, "products": [], "count": 0, "error": "非 Shopify 站点"})
    module._crawl_single_site({"url": "https://f.com", "domain": "f.com"}, "Home", 1, 1)
    assert len(marked) == 1
    _, crawled, _ = marked[0]
    assert crawled["f.com"]["success"] is False
