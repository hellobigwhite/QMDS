"""Sitemap 兜底通道 + cloudscraper 兜底 测试

覆盖：
- sitemap 索引解析、多语言子路径排除、gzip 支持、商品链接提取与去重
- 商品页 JSON-LD 兜底解析
- 三条通道共用的商品记录映射
- crawl_site 的兜底触发判定（products.json 不可用 / 商品数达上限）
- 代理服务熔断与 fetch_json 的 cloudscraper 兜底
"""

import gzip
import time

import pytest
import requests

from qmds.modules.data_scraper import product_crawler, sitemap_fetcher
from qmds.modules.data_scraper.product_crawler import ProxyServiceClient, ProductCrawler
from qmds.modules.data_scraper.sitemap_fetcher import (
    collect_product_urls,
    discover_product_sitemaps,
    extract_jsonld_product,
    extract_product_urls,
    is_locale_prefixed,
    jsonld_to_product,
    parse_sitemap_xml,
)
from qmds.utils import cloudflare_client

INDEX_URL = "https://demo.com/sitemap.xml"
PRODUCTS_URL = "https://demo.com/sitemap_products_1.xml?from=1&to=250"

INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://demo.com/sitemap_products_1.xml?from=1&amp;to=250</loc></sitemap>
  <sitemap><loc>https://demo.com/sitemap_pages_1.xml</loc></sitemap>
  <sitemap><loc>https://demo.com/sitemap_collections_1.xml</loc></sitemap>
  <sitemap><loc>https://demo.com/fr/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://demo.com/en-us/sitemap_products_1.xml</loc></sitemap>
</sitemapindex>"""

PRODUCTS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://demo.com/products/alpha</loc></url>
  <url><loc>https://demo.com/products/beta</loc></url>
  <url><loc>https://demo.com/pages/about</loc></url>
  <url><loc>https://demo.com/collections/all</loc></url>
</urlset>"""

JSONLD_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget","description":"A nice widget",
 "image":["https://cdn.demo.com/a.jpg"],"sku":"W1",
 "offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}
</script></head><body></body></html>"""


def make_fetcher(routes):
    """按精确 URL 返回预置内容，未知 URL 返回 None"""
    def fetch(url, timeout):
        return routes.get(url)
    return fetch


def make_crawler(monkeypatch, *, meta, probe, page_sleep=True):
    """构造不发真实请求的 ProductCrawler

    probe(url) -> (data, status)，用于应答 products.json 分页请求。
    """
    crawler = ProductCrawler(currency_map={"USD": 1.0})
    monkeypatch.setattr(crawler, "fetch_meta",
                        lambda url: (meta, "ok" if meta else "absent"))
    monkeypatch.setattr(crawler, "fetch_json",
                        lambda url, timeout=25, direct_first=False: probe(url))
    if page_sleep:
        monkeypatch.setattr(product_crawler, "PAGE_SLEEP_RANGE", (0, 0))
    calls = {}

    def fake_sitemap(url, category, currency, rate, progress_callback=None,
                     stop_event=None, subcategory="", reason="", flush_callback=None):
        calls["reason"] = reason
        calls["currency"] = currency
        return {"success": True, "products": [], "count": 0, "crawl_mode": "sitemap"}

    monkeypatch.setattr(crawler, "crawl_site_via_sitemap", fake_sitemap)
    return crawler, calls


# ── sitemap 解析 ──────────────────────────────────────────

class TestSitemapParsing:
    def test_parse_gzip(self):
        root = parse_sitemap_xml(gzip.compress(PRODUCTS_XML))
        assert root is not None
        assert sitemap_fetcher._localname(root.tag) == "urlset"

    def test_parse_invalid_returns_none(self):
        assert parse_sitemap_xml(b"not xml at all") is None
        assert parse_sitemap_xml(b"") is None
        assert parse_sitemap_xml(None) is None

    @pytest.mark.parametrize("url,expected", [
        ("https://demo.com/fr/sitemap_products_1.xml", True),
        ("https://demo.com/en-us/sitemap_products_1.xml", True),
        ("https://demo.com/sitemap_products_1.xml", False),
        ("https://demo.com/sitemap.xml_products", False),
    ])
    def test_locale_detection(self, url, expected):
        assert is_locale_prefixed(url) is expected

    def test_discover_from_index(self):
        fetcher = make_fetcher({INDEX_URL: INDEX_XML})
        found = discover_product_sitemaps("https://demo.com", fetcher)
        # 只保留主站点的产品 sitemap（pages/collections 与非产品无关，多语言被排除）
        assert found == [PRODUCTS_URL]

    def test_discover_urlset_containing_products(self):
        fetcher = make_fetcher({INDEX_URL: PRODUCTS_XML})
        assert discover_product_sitemaps("https://demo.com", fetcher) == [INDEX_URL]

    def test_discover_without_products(self):
        pages_only = PRODUCTS_XML.replace(b"/products/alpha", b"/pages/a").replace(
            b"/products/beta", b"/pages/b")
        fetcher = make_fetcher({INDEX_URL: pages_only})
        assert discover_product_sitemaps("https://demo.com", fetcher) == []

    def test_extract_filters_non_product_urls(self):
        fetcher = make_fetcher({PRODUCTS_URL: PRODUCTS_XML})
        urls = extract_product_urls(PRODUCTS_URL, fetcher)
        assert urls == ["https://demo.com/products/alpha", "https://demo.com/products/beta"]

    def test_collect_dedupes(self):
        fetcher = make_fetcher({INDEX_URL: INDEX_XML, PRODUCTS_URL: PRODUCTS_XML})
        urls = collect_product_urls("https://demo.com", fetcher)
        assert urls == ["https://demo.com/products/alpha", "https://demo.com/products/beta"]

    def test_collect_returns_empty_when_sitemap_missing(self):
        assert collect_product_urls("https://demo.com", make_fetcher({})) == []


# ── JSON-LD 兜底解析 ──────────────────────────────────────

class TestJsonLdFallback:
    def test_extract_from_html(self):
        product = extract_jsonld_product(JSONLD_HTML)
        assert product["title"] == "Widget"
        assert product["body_html"] == "A nice widget"
        assert product["images"] == [{"src": "https://cdn.demo.com/a.jpg"}]
        assert product["variants"][0]["price"] == "19.99"
        assert product["variants"][0]["sku"] == "W1"

    def test_extract_from_graph(self):
        html = ('<script type="application/ld+json">'
                '{"@graph":[{"@type":"WebSite"},'
                '{"@type":"Product","name":"Graphed","description":"desc",'
                '"image":"https://cdn.demo.com/g.jpg","offers":{"price":"5"}}]}'
                '</script>')
        product = extract_jsonld_product(html)
        assert product["title"] == "Graphed"
        assert product["images"] == [{"src": "https://cdn.demo.com/g.jpg"}]

    def test_missing_required_fields_returns_none(self):
        assert extract_jsonld_product("<html></html>") is None
        assert jsonld_to_product({"name": "No description"}) is None


# ── 商品记录映射 ──────────────────────────────────────────

class TestParseProductRecord:
    RAW = {
        "id": 9,
        "title": "Nice Product",
        "body_html": "<p>desc</p>",
        "images": [{"src": "https://cdn.demo.com/i.jpg?v=2"}],
        "variants": [{"price": "20.00", "compare_at_price": "30.00", "sku": "SKU1"}],
        "options": [{"name": "Size", "values": ["S", "M"]}],
        "product_type": "Toys",
    }

    def _record(self, raw, **kwargs):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        params = dict(rate=1.0, currency="USD", url="https://demo.com", domain="demo.com",
                      category_label=None, category_fallback="Home",
                      source_category="Home", subcategory_norm="other")
        params.update(kwargs)
        return crawler.parse_product_record(raw, **params)

    def test_full_mapping(self):
        record = self._record(self.RAW)
        assert record["标题"] == "Nice Product"
        assert record["描述"] == "<p>desc</p>"
        # 原价沿用既有契约：转成字符串（折扣价保持数值）
        assert record["原价"] == "30.0"
        assert record["折扣价"] == 20.0
        assert record["图片"] == "https://cdn.demo.com/i.jpg"
        assert record["分类"] == "Toys"
        assert record["变体"] == "Size^S#M"
        assert record["unique_key"] == "nice product"
        assert record["source_category"] == "Home"

    def test_category_label_takes_priority(self):
        assert self._record(self.RAW, category_label="Audio")["分类"] == "Audio"

    def test_missing_image_dropped(self):
        raw = dict(self.RAW, images=[])
        assert self._record(raw) is None

    def test_missing_price_dropped(self):
        raw = dict(self.RAW, variants=[])
        assert self._record(raw) is None

    def test_non_dict_dropped(self):
        assert self._record("not a dict") is None


# ── crawl_site 兜底触发判定 ───────────────────────────────

class TestSitemapFallbackTrigger:
    def test_trigger_when_product_count_at_cap(self, monkeypatch):
        """meta.json 商品数达到 25000 → 直接走 sitemap，不请求 products.json"""
        probed = []

        def probe(url):
            probed.append(url)
            return {"products": []}, 200

        crawler, calls = make_crawler(
            monkeypatch, meta={"currency": "USD", "published_products_count": 25000}, probe=probe)
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["crawl_mode"] == "sitemap"
        assert "上限" in calls["reason"]
        assert probed == []

    def test_trigger_when_products_json_unavailable(self, monkeypatch):
        """products.json 被禁用（403）→ 走 sitemap"""
        crawler, calls = make_crawler(
            monkeypatch,
            meta={"currency": "USD", "published_products_count": 120},
            probe=lambda url: (None, 403),
        )
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["crawl_mode"] == "sitemap"
        assert "products.json" in calls["reason"]

    def test_trigger_when_products_json_not_json(self, monkeypatch):
        """products.json 返回 HTML（反爬页）→ 走 sitemap"""
        crawler, calls = make_crawler(
            monkeypatch,
            meta={"currency": "USD", "published_products_count": 120},
            probe=lambda url: (None, 200),
        )
        assert crawler.crawl_site("https://demo.com", "Home")["crawl_mode"] == "sitemap"

    def test_normal_site_keeps_pagination(self, monkeypatch):
        """常规店铺仍走 products.json 分页通道"""
        page1 = {"products": [{
            "id": 1, "title": "Alpha", "body_html": "<p>d</p>",
            "images": [{"src": "https://cdn.demo.com/a.jpg"}],
            "variants": [{"price": "15.00"}], "options": [], "product_type": "Toys",
        }]}

        def probe(url):
            return (page1, 200) if "page=1" in url else ({"products": []}, 200)

        monkeypatch.setattr(product_crawler, "MAX_EMPTY_PAGES", 1)
        crawler, _calls = make_crawler(
            monkeypatch, meta={"currency": "USD", "published_products_count": 120}, probe=probe)
        result = crawler.crawl_site("https://demo.com", "Home")
        assert "crawl_mode" not in result
        assert result["success"] is True
        assert [p["标题"] for p in result["products"]] == ["Alpha"]

    def test_empty_products_json_is_not_a_fallback(self, monkeypatch):
        """products.json 正常返回空列表 = 店铺确实无商品，不应触发 sitemap"""
        crawler, _calls = make_crawler(
            monkeypatch,
            meta={"currency": "USD", "published_products_count": 0},
            probe=lambda url: ({"products": []}, 200),
        )
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["success"] is False
        assert result["error"] == "无商品数据"

    def test_missing_currency_rate_fails_fast(self, monkeypatch):
        crawler, _calls = make_crawler(
            monkeypatch,
            meta={"currency": "XYZ", "published_products_count": 10},
            probe=lambda url: ({"products": []}, 200),
        )
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["success"] is False
        assert "无汇率配置" in result["error"]


class TestCrawlSiteViaSitemap:
    """sitemap 通道端到端（传输层全部打桩）"""

    def _crawler(self, monkeypatch, *, json_result, html):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(sitemap_fetcher, "collect_product_urls",
                            lambda *a, **k: ["https://demo.com/products/alpha",
                                             "https://demo.com/products/beta"])
        monkeypatch.setattr(crawler, "fetch_json",
                            lambda url, timeout=25, direct_first=False: json_result)
        monkeypatch.setattr(crawler, "fetch_text", lambda url, timeout=25, status_holder=None: html)
        return crawler

    def test_jsonld_channel_dedupes_by_title(self, monkeypatch):
        """.json 被禁用时改走商品页 JSON-LD，并按标题去重"""
        crawler = self._crawler(monkeypatch, json_result=(None, 403), html=JSONLD_HTML)
        result = crawler.crawl_site_via_sitemap(
            "https://demo.com", "Home", "USD", 1.0, reason="单测")
        assert result["success"] is True
        assert result["crawl_mode"] == "sitemap"
        # 两条链接返回同一商品标题 → 去重为 1 条
        assert result["count"] == 1
        assert result["products"][0]["标题"] == "Widget"
        assert result["products"][0]["unique_key"] == "widget"

    def test_all_fetches_failed_is_not_success(self, monkeypatch):
        """全部商品取数失败应报失败，避免被标记为已爬取后永久丢失"""
        crawler = self._crawler(monkeypatch, json_result=(None, 403), html="")
        result = crawler.crawl_site_via_sitemap(
            "https://demo.com", "Home", "USD", 1.0, reason="单测")
        assert result["success"] is False
        assert "取数全部失败" in result["error"]

    def test_no_product_urls_fails(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(sitemap_fetcher, "collect_product_urls", lambda *a, **k: [])
        result = crawler.crawl_site_via_sitemap(
            "https://demo.com", "Home", "USD", 1.0, reason="单测")
        assert result["success"] is False
        assert "未发现商品链接" in result["error"]


class TestSitemap429Retry:
    """429 限流下的单商品取数：冷却重试 + 代理抢救，不轻易丢商品"""

    def _crawler(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        # 冷却等待与限速等待在测试里立即返回
        monkeypatch.setattr(crawler, "_wait_if_throttled", lambda *a, **k: None)
        monkeypatch.setattr(crawler, "_domain_pace_delay", lambda domain: 0.0)
        return crawler

    def test_double_429_rescued_via_proxy(self, monkeypatch):
        """直连两次 429 后经代理（不同出口 IP）抢救商品"""
        crawler = self._crawler(monkeypatch)
        calls = []

        def fake_fetch_json(url, timeout=25, direct_first=False):
            calls.append(direct_first)
            if direct_first:
                return None, 429
            return {"product": {"title": "Widget", "variants": [], "options": []}}, 200

        monkeypatch.setattr(crawler, "fetch_json", fake_fetch_json)
        product = crawler._fetch_sitemap_product("https://demo.com/products/w")
        assert product is not None
        assert product["title"] == "Widget"
        # 直连 429 → 冷却后重试直连 429 → 代理抢救
        assert calls == [True, True, False]

    def test_429_retry_success_after_cooldown(self, monkeypatch):
        """首次 429、冷却重试成功：商品不丢，无需代理"""
        crawler = self._crawler(monkeypatch)
        calls = []

        def fake_fetch_json(url, timeout=25, direct_first=False):
            calls.append(direct_first)
            if len(calls) == 1:
                return None, 429
            return {"product": {"title": "Widget", "variants": [], "options": []}}, 200

        monkeypatch.setattr(crawler, "fetch_json", fake_fetch_json)
        product = crawler._fetch_sitemap_product("https://demo.com/products/w")
        assert product is not None
        assert product["title"] == "Widget"
        assert calls == [True, True]

    def test_all_429_gives_up_without_html_hammering(self, monkeypatch):
        """直连与代理均 429：放弃本商品，不再发起 HTML 兜底轰炸目标站"""
        crawler = self._crawler(monkeypatch)
        fetch_text_calls = []

        def fake_fetch_json(url, timeout=25, direct_first=False):
            return None, 429

        monkeypatch.setattr(crawler, "fetch_json", fake_fetch_json)
        monkeypatch.setattr(crawler, "fetch_text",
                            lambda url, timeout=25, status_holder=None: fetch_text_calls.append(url))
        assert crawler._fetch_sitemap_product("https://demo.com/products/w") is None
        assert fetch_text_calls == []


# ── 代理服务熔断 / cloudscraper 兜底 ──────────────────────

class FakeHttpResponse:
    def __init__(self, status_code=200, payload=None, content=b"", text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestProxyServiceCircuitBreaker:
    def test_trips_after_threshold(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("down")

        client = ProxyServiceClient()
        monkeypatch.setattr(client._http, "get", boom)
        for _ in range(ProxyServiceClient.FAILURE_THRESHOLD):
            assert client.fetch("https://target") == (None, 0)
        assert client.available is False

        # 熔断期间不再发起真实请求，直接返回 STATUS_SKIPPED
        calls = []
        monkeypatch.setattr(client._http, "get", lambda *a, **k: calls.append(1))
        assert client.fetch("https://target") == (None, ProxyServiceClient.STATUS_SKIPPED)
        assert calls == []

    def test_success_resets_counter(self, monkeypatch):
        client = ProxyServiceClient()
        monkeypatch.setattr(client._http, "get",
                            lambda *a, **k: FakeHttpResponse(payload={"ok": True}))
        assert client.fetch("https://target") == ({"ok": True}, 200)
        assert client.available is True

    @pytest.mark.parametrize("status,expected", [
        (0, True), (500, True), (403, True), (429, True),
        (404, False), (401, False), (200, False),
    ])
    def test_service_level_failure_classification(self, status, expected):
        assert ProxyServiceClient._is_service_level_failure(status) is expected

    def test_inflight_cap_returns_busy_fast(self, monkeypatch):
        """并发名额满时短暂等待后快速返回 STATUS_BUSY，而不是排队到超时"""
        monkeypatch.setattr(ProxyServiceClient, "QUEUE_WAIT", 0.1)
        client = ProxyServiceClient()
        # 占满全部进程级名额
        held = [ProxyServiceClient._inflight.acquire()
                for _ in range(ProxyServiceClient.INFLIGHT_LIMIT)]
        try:
            import time as _time
            t0 = _time.time()
            data, status = client.fetch("https://target")
            dt = _time.time() - t0
            assert status == ProxyServiceClient.STATUS_BUSY
            assert data is None
            assert dt < 3  # 快速失败，不等待长超时
        finally:
            for _ in held:
                ProxyServiceClient._inflight.release()

    def test_busy_not_counted_as_service_failure(self, monkeypatch):
        """STATUS_BUSY 不计入熔断统计（请求根本没发出去）"""
        monkeypatch.setattr(ProxyServiceClient, "QUEUE_WAIT", 0.05)
        client = ProxyServiceClient()
        held = [ProxyServiceClient._inflight.acquire()
                for _ in range(ProxyServiceClient.INFLIGHT_LIMIT)]
        try:
            client.fetch("https://target")
            assert client._failure == 0
            assert client._consecutive_failures == 0
            assert client.available is True
        finally:
            for _ in held:
                ProxyServiceClient._inflight.release()


class TestDirectBlockedLatch:
    """直连被 CF 拒绝（403/401）后按域记住，后续 direct_first 跳过直连"""

    def test_403_latches_then_skips_direct(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        # 禁用第 4 级 cloudscraper 兜底（它也调 cloudflare_client.get，会干扰计数）
        monkeypatch.setattr(crawler, "_fetch_json_via_cloudscraper", lambda url, timeout=25: None)
        direct_calls = []

        def fake_direct(url, timeout=20, **k):
            direct_calls.append(url)
            return FakeHttpResponse(status_code=403, text="challenge")

        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get", fake_direct)
        monkeypatch.setattr(crawler.proxy_service, "fetch", lambda url, **k: (None, 403))
        monkeypatch.setattr(crawler, "get_next_proxy", lambda: None)
        monkeypatch.setattr(crawler.session, "get",
                            lambda *a, **k: FakeHttpResponse(status_code=403, text="challenge"))

        crawler.fetch_json("https://demo.com/products.json", direct_first=True)
        crawler.fetch_json("https://demo.com/products.json", direct_first=True)
        # 第一次直连 403 触发闩锁，第二次不再直连
        assert len(direct_calls) == 1
        assert crawler._direct_blocked.get("demo.com") is True

    def test_429_does_not_latch(self, monkeypatch):
        """429 是限流不是封禁：不闩锁，冷却后直连仍可重试"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, timeout=20, **k: FakeHttpResponse(status_code=429))
        monkeypatch.setattr(crawler.proxy_service, "fetch", lambda url, **k: (None, 502))
        monkeypatch.setattr(crawler, "get_next_proxy", lambda: None)
        monkeypatch.setattr(crawler.session, "get",
                            lambda *a, **k: FakeHttpResponse(status_code=200, payload={"ok": 1}))

        crawler.fetch_json("https://demo.com/products.json", direct_first=True)
        assert "demo.com" not in crawler._direct_blocked


class TestBlockedNotMisjudged:
    """CF 限流/拦截绝不判为"非 Shopify"（历史误杀 65 个真店的教训）"""

    def test_meta_blocked_returns_blocked_outcome(self, monkeypatch):
        """meta 被拦截（429/403/超时）→ blocked 结果，而非非 Shopify 定论"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(crawler, "fetch_meta", lambda url: (None, "blocked"))
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["success"] is False
        assert result.get("blocked") is True
        assert "拦截" in result["error"]
        assert "非 Shopify" not in result["error"]

    def test_meta_absent_still_non_shopify(self, monkeypatch):
        """meta 404（headless 之外的真非 Shopify）→ 维持原判定"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(crawler, "fetch_meta", lambda url: (None, "absent"))
        result = crawler.crawl_site("https://demo.com", "Home")
        assert result["success"] is False
        assert result.get("blocked") is None or result.get("blocked") is False
        assert result["error"] == "非 Shopify 站点"

    def test_fetch_meta_verdicts(self, monkeypatch):
        """verdict 三态：ok / absent（404、410、200非JSON）/ blocked（其余）"""
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        for status, expected in [(200, "ok"), (404, "absent"), (410, "absent"),
                                 (429, "blocked"), (403, "blocked"), (0, "blocked"),
                                 (ProxyServiceClient.STATUS_BUSY, "blocked"),
                                 (ProxyServiceClient.STATUS_SKIPPED, "blocked")]:
            payload = {"currency": "USD"} if status == 200 else None
            monkeypatch.setattr(crawler, "fetch_json",
                                lambda url, timeout=15, direct_first=True, proxy_timeout=20:
                                (payload, status))
            meta, verdict = crawler.fetch_meta("https://demo.com")
            assert verdict == expected, f"HTTP {status} 应为 {expected}"
            if expected == "ok":
                assert meta == {"currency": "USD"}
            else:
                assert meta is None

    def test_429_storm_triggers_global_cooldown(self, monkeypatch):
        """60 秒内 3 个不同域直连 429 → 触发全局直连冷却"""
        # 重置模块级风暴状态，避免与其他测试相互影响
        monkeypatch.setattr(product_crawler, "_direct_429_hits", [])
        monkeypatch.setattr(product_crawler, "_direct_429_until", 0.0)
        assert product_crawler._direct_cooldown_active() is False
        product_crawler._note_direct_429("a.com")
        product_crawler._note_direct_429("b.com")
        assert product_crawler._direct_cooldown_active() is False  # 未到阈值
        product_crawler._note_direct_429("c.com")
        assert product_crawler._direct_cooldown_active() is True
        assert product_crawler._direct_cooldown_remaining() > 0

    def test_cooldown_skips_direct_attempts(self, monkeypatch):
        """冷却期内 direct_first 跳过直连，直接走代理"""
        monkeypatch.setattr(product_crawler, "_direct_429_until", time.time() + 60)
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        direct_calls = []
        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, timeout=20, **k: direct_calls.append(url))
        monkeypatch.setattr(crawler.proxy_service, "fetch",
                            lambda url, **k: ({"product": {"title": "W"}}, 200))
        data, status = crawler.fetch_json("https://demo.com/p.json", direct_first=True)
        assert status == 200 and data == {"product": {"title": "W"}}
        assert direct_calls == []  # 冷却期内未发起任何直连


class TestFetchJsonCloudscraperFallback:
    def test_403_falls_back_to_cloudscraper(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        monkeypatch.setattr(crawler.proxy_service, "fetch", lambda url, **k: (None, 403))
        monkeypatch.setattr(crawler, "get_next_proxy", lambda: None)
        monkeypatch.setattr(crawler.session, "get",
                            lambda *a, **k: FakeHttpResponse(status_code=403, text="challenge"))
        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, **k: FakeHttpResponse(payload={"products": []}))

        data, status = crawler.fetch_json("https://demo.com/products.json")
        assert status == 200
        assert data == {"products": []}

    def test_cloudscraper_failure_returns_original_status(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        monkeypatch.setattr(crawler.proxy_service, "fetch", lambda url, **k: (None, 403))
        monkeypatch.setattr(crawler, "get_next_proxy", lambda: None)
        monkeypatch.setattr(crawler.session, "get",
                            lambda *a, **k: FakeHttpResponse(status_code=403, text="challenge"))
        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get", lambda url, **k: None)

        assert crawler.fetch_json("https://demo.com/products.json") == (None, 403)

    def test_404_does_not_invoke_cloudscraper(self, monkeypatch):
        crawler = ProductCrawler(currency_map={"USD": 1.0})
        monkeypatch.setattr(product_crawler.time, "sleep", lambda *a: None)
        monkeypatch.setattr(crawler.proxy_service, "fetch", lambda url, **k: (None, 404))
        monkeypatch.setattr(crawler, "get_next_proxy", lambda: None)
        monkeypatch.setattr(crawler.session, "get",
                            lambda *a, **k: FakeHttpResponse(status_code=404, text="nope"))
        called = []
        monkeypatch.setattr(cloudflare_client, "is_available", lambda: True)
        monkeypatch.setattr(cloudflare_client, "get",
                            lambda url, **k: called.append(url))

        assert crawler.fetch_json("https://demo.com/products.json") == (None, 404)
        assert called == []
