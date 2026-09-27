"""meta.json 404/禁用但首页有 Shopify 特征 → sitemap 兜底 测试

覆盖 crawl_site 的 absent 分支增强：
- meta 404 + 首页有 cdn.shopify → 走 sitemap 兜底（不误判非 Shopify）
- meta 404 + 首页无特征 → 仍判非 Shopify
- _looks_like_shopify 各特征识别
"""

import pytest

from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.product_crawler import ProductCrawler
from qmds.utils import cloudflare_client


def make_crawler(monkeypatch, *, meta=None, homepage_body=""):
    """meta 返回 absent，首页返回给定 HTML 的爬取器"""
    crawler = ProductCrawler(currency_map={"USD": 1.0})
    monkeypatch.setattr(crawler, "fetch_meta",
                        lambda url: (meta, "absent" if meta is None else "ok"))
    monkeypatch.setattr(crawler, "fetch_json",
                        lambda url, timeout=25, direct_first=False, busy_retries=0: (None, 404))
    monkeypatch.setattr(crawler, "_looks_like_shopify",
                        lambda url: "cdn.shopify.com" in homepage_body)
    calls = {}

    def fake_sitemap(url, category, currency, rate, progress_callback=None,
                     stop_event=None, subcategory="", reason="", flush_callback=None):
        calls["reason"] = reason
        calls["currency"] = currency
        return {"success": True, "products": [], "count": 0, "crawl_mode": "sitemap"}

    monkeypatch.setattr(crawler, "crawl_site_via_sitemap", fake_sitemap)
    return crawler, calls


class TestAbsentMetaShopifyFallback:
    def test_absent_meta_with_shopify_homepage_uses_sitemap(self, monkeypatch):
        """meta 404 但首页有 cdn.shopify → 走 sitemap 兜底，不误判"""
        crawler, calls = make_crawler(monkeypatch, homepage_body="<html><script src='https://cdn.shopify.com/s/files/1/1234.js'></script></html>")
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["crawl_mode"] == "sitemap"
        assert "Shopify 特征" in calls["reason"]

    def test_absent_meta_without_shopify_homepage_is_not_shopify(self, monkeypatch):
        """meta 404 且首页无 Shopify 特征 → 仍判非 Shopify"""
        crawler, _calls = make_crawler(monkeypatch, homepage_body="<html><title>Plain Store</title></html>")
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["success"] is False
        assert result["error"] == "非 Shopify 站点"
        assert "crawl_mode" not in result

    def test_looks_like_shopify_cdn_script(self, monkeypatch):
        """cdn.shopify.com 引用 → True"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, timeout=10: FakeResp(200, "<html><script src='https://cdn.shopify.com/s/files/1/x.js'></script></html>"))
        assert crawler._looks_like_shopify("https://demo.com") is True

    def test_looks_like_shopify_generator_meta(self, monkeypatch):
        """Shopify generator meta 标签 → True"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, timeout=10: FakeResp(200, '<meta name="generator" content="Shopify" />'))
        assert crawler._looks_like_shopify("https://demo.com") is True

    def test_looks_like_shopify_plain_site_false(self, monkeypatch):
        """普通站首页 → False"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, timeout=10: FakeResp(200, "<html><title>Plain</title></html>"))
        assert crawler._looks_like_shopify("https://demo.com") is False

    def test_looks_like_shopify_direct_fail_uses_proxy(self, monkeypatch):
        """直连失败 → 走代理服务验证"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(cloudflare_client, "get", lambda url, timeout=10: None)
        monkeypatch.setattr(crawler.proxy_service, "fetch_bytes",
                            lambda url, timeout=15: (b"<html>cdn.shopify.com</html>", 200))
        assert crawler._looks_like_shopify("https://demo.com") is True

    def test_looks_like_shopify_all_fail_false(self, monkeypatch):
        """直连失败 + 代理无数据 → False"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(cloudflare_client, "get", lambda url, timeout=10: None)
        monkeypatch.setattr(crawler.proxy_service, "fetch_bytes", lambda url, timeout=15: (None, 0))
        assert crawler._looks_like_shopify("https://demo.com") is False


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.headers = {}
