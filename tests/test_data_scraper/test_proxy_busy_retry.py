"""代理服务并发满（BUSY）等待重试测试

验证 fetch_json / fetch_meta 的 busy_retries 逻辑：
- BUSY 时等待重试，名额释放后拿到 200 数据
- 持续 BUSY 时最终返回 BUSY（不误判）
- 默认 busy_retries=0 不重试（sitemap 高频场景快速降级）
"""

import threading

import pytest

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler, ProxyServiceClient


class FakeResp:
    status_code = 403  # 直连降级用：403 模拟 CF 拦截（真实场景 BUSY 站直连必被拦）
    headers = {}
    text = ""

    def json(self):
        raise ValueError


class FakeSession:
    def get(self, *a, **k):
        return FakeResp()


class FakeProxy:
    """按调用序列返回 (data, status) 的假代理服务"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def fetch(self, url, timeout=None):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


def build_module(proxy):
    module = ProductCrawler.__new__(ProductCrawler)
    module._lock = threading.Lock()
    module._direct_blocked = {}
    module.proxy_service = proxy
    module.session = FakeSession()
    module.get_next_proxy = lambda: None
    module._fetch_json_via_cloudscraper = lambda url, t: None
    module.currency_map = {}
    return module


def test_busy_then_success(monkeypatch):
    """第一次 BUSY，重试后拿到 200 → fetch_json 成功"""
    data = {"published_products_count": 100}
    proxy = FakeProxy([(None, ProxyServiceClient.STATUS_BUSY), (data, 200)])
    module = build_module(proxy)
    got, status = module.fetch_json("https://a.com/meta.json", busy_retries=2)
    assert got == data
    assert status == 200
    assert proxy.calls == 2


def test_busy_then_busy_exhausts_retries(monkeypatch):
    """一直 BUSY：重试耗尽（1 初始 + 2 重试），不误判为成功"""
    proxy = FakeProxy([(None, ProxyServiceClient.STATUS_BUSY)] * 3)
    module = build_module(proxy)
    got, status = module.fetch_json("https://a.com/meta.json", busy_retries=2)
    assert got is None
    assert proxy.calls == 3  # 1 次初始 + 2 次重试
    # 降级链直连 403 → 不可能是 200，也不会是 absent 结论
    assert status != 200
    assert status not in (404, 410)


def test_no_retry_by_default(monkeypatch):
    """默认 busy_retries=0：BUSY 不重试，直接降级"""
    proxy = FakeProxy([(None, ProxyServiceClient.STATUS_BUSY)])
    module = build_module(proxy)
    got, status = module.fetch_json("https://a.com/meta.json")
    assert got is None
    assert proxy.calls == 1


def test_fetch_meta_busy_retry_recovers(monkeypatch):
    """fetch_meta 传入 busy_retries：BUSY 后重试成功 → verdict=ok"""
    monkeypatch.setattr(pc_mod.cloudflare_client, "get", lambda *a, **k: None)
    data = {"published_products_count": 100, "currency": "USD"}
    proxy = FakeProxy([(None, ProxyServiceClient.STATUS_BUSY), (data, 200)])
    module = build_module(proxy)
    meta, verdict = module.fetch_meta("https://a.com")
    assert verdict == "ok"
    assert meta == data
    assert proxy.calls == 2
