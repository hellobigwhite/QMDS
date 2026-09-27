"""0 件原因日志 测试

_crawl_single_site 完成时若 0 件新增，日志必须说明原因：
- count>0 且 saved=0 → "已全部入库"（去重命中）
- count>0 且 saved<count → "X 件已存在跳过"
- count=0 → 输出具体 error 原因
"""

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler


class FakeCrawler:
    def __init__(self, result=None, flush_batches=None):
        self.result = result or {"success": True, "products": [], "count": 0}
        self.flush_batches = flush_batches or []

    def crawl_site(self, url, category, progress_callback, stop_event=None,
                   subcategory="", flush_callback=None):
        for batch in self.flush_batches:
            if flush_callback:
                flush_callback(batch)
        return self.result

    def close(self):
        pass


class FakePDB:
    def save_raw_products(self, category, subcategory, products):
        return len(products)

    def close(self):
        pass


class FakeMongo:
    def close(self):
        pass


class FakeLog:
    def __init__(self, records):
        self.records = records

    def info(self, msg, *a, **k):
        self.records.append(str(msg))

    def warning(self, msg, *a, **k):
        self.records.append(str(msg))

    def error(self, msg, *a, **k):
        self.records.append(str(msg))

    def debug(self, msg, *a, **k):
        pass


def build(monkeypatch, *, crawl_result, flush_batches=None):
    msgs = []
    log_records = []
    module = ProductCrawler.__new__(ProductCrawler)
    module._acquire_site_slot = lambda *a, **k: pc_mod._site_concurrency.acquire(timeout=5)
    module._mark_crawled = lambda *a, **k: None
    monkeypatch.setattr(pc_mod, "create_crawler", lambda: FakeCrawler(crawl_result, flush_batches))
    monkeypatch.setattr(pc_mod, "ProductDBClient", lambda: FakePDB())
    monkeypatch.setattr(pc_mod, "MongoDBClient", lambda: FakeMongo())
    monkeypatch.setattr(pc_mod, "log", FakeLog(log_records))
    return module, msgs, log_records


def run(module, msgs):
    return module._crawl_single_site(
        {"url": "https://a.com", "domain": "a.com"}, "Home", 1, 1,
        progress_callback=lambda m: msgs.append(m))


def test_zero_new_all_existed_logged(monkeypatch):
    """count=69 但 0 入库（全部已存在）→ 进度与日志都说明"已全部入库" """
    module, msgs, logs = build(monkeypatch,
        crawl_result={"success": True, "products": [], "count": 69})
    result = run(module, msgs)
    assert result["saved"] == 0
    joined = " | ".join(msgs)
    assert "0 件新增" in joined and "已全部入库" in joined
    assert "69 件商品已全部入库过" in joined
    assert any("已全部入库" in m for m in logs)


def test_partial_existed_logged(monkeypatch):
    """count=69 但本次仅入库 2（67 件已存在）→ 日志说明跳过数"""
    module, msgs, logs = build(monkeypatch,
        crawl_result={"success": True, "products": [], "count": 69},
        flush_batches=[[{"unique_key": "k1"}, {"unique_key": "k2"}]])
    result = run(module, msgs)
    assert result["saved"] == 2
    assert any("67 件已存在跳过" in m for m in logs)


def test_zero_products_with_error_logged(monkeypatch):
    """count=0（真没爬到）→ 日志带出具体 error 原因"""
    module, msgs, logs = build(monkeypatch,
        crawl_result={"success": False, "products": [], "count": 0, "error": "products.json 返回空列表"})
    run(module, msgs)
    assert any("0 件商品" in m and "products.json 返回空列表" in m for m in logs)


def test_normal_first_crawl_no_extra_warning(monkeypatch):
    """首次爬取全新增（saved==count）→ 不输出误导性日志"""
    module, msgs, logs = build(monkeypatch,
        crawl_result={"success": True, "products": [], "count": 3},
        flush_batches=[[{"unique_key": "k1"}, {"unique_key": "k2"}, {"unique_key": "k3"}]])
    result = run(module, msgs)
    assert result["saved"] == 3
    assert not any("已全部入库" in m or "已存在跳过" in m for m in logs)
