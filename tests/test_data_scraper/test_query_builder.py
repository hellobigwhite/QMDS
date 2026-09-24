"""搜索变体构建与噪声过滤的单元测试

覆盖 query_builder 的长尾词变体/黑名单/dork 命中判定，
以及 google_search 的 filter_urls 黑名单过滤与 scrape() 翻页提前停止。
"""

from types import SimpleNamespace

import pytest

from qmds.modules.data_scraper.discovery import google_search
from qmds.modules.data_scraper.discovery.google_search import filter_urls
from qmds.modules.data_scraper.discovery.query_builder import (
    build_exa_variants,
    build_query_variants,
    count_dork_hits,
    extract_dork_tokens,
    is_noise_domain,
)


# ── build_query_variants ────────────────────────────────────

class TestBuildQueryVariants:
    def test_plain_kw_yields_five(self):
        """普通关键词生成 5 条：2 指纹 + 3 长尾×指纹"""
        variants = build_query_variants("dog toys")
        assert len(variants) == 5

    def test_contains_collections_base(self):
        variants = build_query_variants("dog toys")
        assert "dog toys inurl:collections/all" in variants[0]

    def test_contains_powered_by_shopify_family(self):
        variants = build_query_variants("dog toys")
        assert any('"powered by shopify"' in v for v in variants)

    def test_contains_shopping_longtails(self):
        """变体包含用户购物常用长尾词（best/cheap/for sale...）"""
        variants = build_query_variants("dog toys")
        joined = " | ".join(variants)
        assert "best dog toys inurl:collections/all" in joined
        assert "cheap dog toys inurl:collections/all" in joined
        assert "dog toys for sale inurl:collections/all" in joined

    def test_no_site_exclusion_concatenation(self):
        """查询保持干净：不拼接 -site: 排除串，也没有 site: 运算符"""
        variants = build_query_variants("dog toys")
        for v in variants:
            assert "-site:" not in v
            assert "site:" not in v

    def test_no_page_diversifier(self):
        variants = build_query_variants("dog toys")
        assert all("- page" not in v for v in variants)

    def test_kw_already_has_intent_skips_duplicate(self):
        """关键词已含购买意图词时不重复生成（如 for sale / near me）"""
        variants = build_query_variants("dog toys for sale near me")
        assert len(variants) >= 2  # 指纹变体始终保留
        assert not any("for sale for sale" in v for v in variants)
        assert not any("near me near me" in v for v in variants)
        # 前缀型长尾仍然生成
        joined = " | ".join(variants)
        assert "best dog toys for sale near me" in joined

    def test_kw_with_best_skips_best(self):
        variants = build_query_variants("best dog toys")
        joined = " | ".join(variants)
        assert "best best" not in joined
        assert "cheap best dog toys" in joined

    def test_whitespace_normalized(self):
        variants = build_query_variants("  dog   toys  ")
        assert all("dog toys" in v for v in variants)
        assert all("dog   toys" not in v for v in variants)

    def test_empty_kw_fallback(self):
        variants = build_query_variants("   ")
        assert variants == ["inurl:collections/all"]


# ── build_exa_variants ──────────────────────────────────────

class TestBuildExaVariants:
    def test_no_google_operators(self):
        for v in build_exa_variants("dog toys"):
            assert "inurl:" not in v
            assert "site:" not in v
            assert "-site:" not in v

    def test_contains_keyword(self):
        for v in build_exa_variants("dog toys"):
            assert "dog toys" in v

    def test_online_store_intent(self):
        variants = build_exa_variants("dog toys")
        assert "dog toys online store" in variants

    def test_longtail_included(self):
        variants = build_exa_variants("dog toys")
        assert any(v.startswith("best dog toys") for v in variants)

    def test_empty_kw_fallback(self):
        assert build_exa_variants("   ") == ["online store powered by shopify"]


# ── is_noise_domain ─────────────────────────────────────────

class TestIsNoiseDomain:
    def test_exact_match(self):
        assert is_noise_domain("youtube.com")
        assert is_noise_domain("amazon.com")
        assert is_noise_domain("shopify.com")

    def test_subdomain_match(self):
        assert is_noise_domain("m.youtube.com")
        assert is_noise_domain("en.wikipedia.org")
        assert is_noise_domain("us.amazon.com")
        assert is_noise_domain("apps.apple.com")

    def test_prefix_tld_match(self):
        assert is_noise_domain("amazon.in")
        assert is_noise_domain("amazon.co.uk")
        assert is_noise_domain("ebay.de")
        assert is_noise_domain("google.co.jp")
        assert is_noise_domain("walmart.ca")

    def test_non_noise_kept(self):
        assert not is_noise_domain("acmestore.com")
        assert not is_noise_domain("notyoutube.com")
        assert not is_noise_domain("bestbuygadgets.com")  # 相似域名不清除
        assert not is_noise_domain("mystore.myshopify.com")  # myshopify 店铺必须保留

    def test_empty(self):
        assert not is_noise_domain("")
        assert not is_noise_domain(None)


# ── extract_dork_tokens / count_dork_hits ───────────────────

class TestDorkTokens:
    def test_extract_collections(self):
        assert extract_dork_tokens("dog toys inurl:collections/all") == ["collections/all"]

    def test_extract_site_wildcard(self):
        assert extract_dork_tokens("gloves site:*.myshopify.com") == ["myshopify.com"]

    def test_no_tokens_for_plain_query(self):
        assert extract_dork_tokens("dog toys online store") == []

    def test_count_dork_hits(self):
        urls = [
            "https://acmestore.com/collections/all",
            "https://www.youtube.com/watch?v=abc",  # Google 放宽后的噪声
        ]
        assert count_dork_hits(urls, "dog toys inurl:collections/all") == 1

    def test_myshopify_url_always_hit(self):
        urls = ["https://acmestore.myshopify.com"]
        assert count_dork_hits(urls, "dog toys inurl:collections/all") == 1

    def test_plain_query_never_triggers(self):
        urls = ["https://www.youtube.com/watch?v=abc"]
        assert count_dork_hits(urls, "dog toys") == 1  # 返回 len(urls)

    def test_empty_urls(self):
        assert count_dork_hits([], "dog toys inurl:collections/all") == 0


# ── filter_urls 噪声过滤 ────────────────────────────────────

class TestFilterUrlsNoise:
    def test_noise_domains_dropped(self):
        urls = [
            "https://acmestore.com/collections/all",
            "https://www.youtube.com/watch?v=abc",
            "https://www.pinterest.com/pin/123",
            "https://en.wikipedia.org/wiki/Dog",
        ]
        cleaned, _ = filter_urls(urls)
        assert len(cleaned) == 1
        assert cleaned[0] == "https://acmestore.com"

    def test_myshopify_url_kept_and_mapped(self):
        urls = ["https://acmestore.myshopify.com/collections/all"]
        cleaned, url_map = filter_urls(urls)
        assert cleaned == ["https://acmestore.com"]
        assert url_map == {"https://acmestore.com": "https://acmestore.myshopify.com"}

    def test_block_noise_false_keeps_noise(self):
        urls = ["https://www.youtube.com/watch?v=abc", "https://acmestore.com"]
        cleaned, _ = filter_urls(urls, block_noise=False)
        # clean_url 会剥掉 www 前缀
        assert "https://youtube.com" in cleaned
        assert "https://acmestore.com" in cleaned

    def test_noise_filter_applies_with_existing_domains(self):
        urls = ["https://www.youtube.com/watch?v=abc", "https://acmestore.com/collections/all"]
        cleaned, _ = filter_urls(urls, existing_domains={"acmestore.com"})
        assert cleaned == []


# ── GoogleShopifySearcher.scrape 翻页提前停止 ────────────────

class FakeManager:
    """按页返回预设 URL 的假搜索管理器，并记录调用次数"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def search(self, query, page=1, provider_name=""):
        self.calls += 1
        idx = min(page - 1, len(self.pages) - 1)
        return SimpleNamespace(urls=self.pages[idx])

    def get_status(self):
        return []


@pytest.fixture
def fake_searcher(monkeypatch):
    """构造不触碰真实 SearchManager 的 GoogleShopifySearcher"""
    manager = FakeManager([])

    def fake_get_manager():
        return manager

    monkeypatch.setattr(google_search, "_get_search_manager", fake_get_manager)
    searcher = google_search.GoogleShopifySearcher()
    searcher._manager = manager
    return searcher, manager


def test_scrape_stops_when_dork_exhausted(fake_searcher):
    """连续两页都不命中 dork 指纹即停止翻页（不再无限深翻页灌噪声）"""
    searcher, manager = fake_searcher
    manager.pages = [
        ["https://acmestore.com/collections/all"],          # page1: 命中
        ["https://www.youtube.com/watch?v=abc"],            # page2: 放宽噪声
        ["https://www.facebook.com/dogtoys"],               # page3: 放宽噪声
    ]
    result = searcher.scrape(query="dog toys inurl:collections/all")
    # 共调用 3 页（命中 1 + 噪声 2），第 3 页后 dork_miss=2 停止，不再翻第 4 页
    assert manager.calls == 3
    # 已取回的 URL 全部保留（不丢数据），由后续平台检测定真伪
    urls = [d["url"] for d in result.data]
    assert "https://acmestore.com/collections/all" in urls
    assert "https://www.youtube.com/watch?v=abc" in urls
    assert "https://www.facebook.com/dogtoys" in urls


def test_scrape_plain_query_never_early_stops(fake_searcher):
    """非 dork 查询（无 inurl:/site: 运算符）不触发提前停止"""
    searcher, manager = fake_searcher
    manager.pages = [["https://acmestore.com"], ["https://otherstore.com"]]
    result = searcher.scrape(query="dog toys", max_pages=2)
    assert manager.calls == 2
    assert len(result.data) == 2


def test_scrape_dork_hits_reset_counter(fake_searcher):
    """dork 命中会重置连续 miss 计数（命中、噪声、命中、噪声、噪声 → 共 5 页）"""
    searcher, manager = fake_searcher
    manager.pages = [
        ["https://acmestore.com/collections/all"],   # 命中
        ["https://www.youtube.com/watch?v=1"],       # miss (1)
        ["https://otherstore.com/collections/all"],  # 命中 → 重置
        ["https://www.facebook.com/x"],              # miss (1)
        ["https://www.reddit.com/y"],                # miss (2) → 停止
    ]
    result = searcher.scrape(query="dog toys inurl:collections/all")
    assert manager.calls == 5
