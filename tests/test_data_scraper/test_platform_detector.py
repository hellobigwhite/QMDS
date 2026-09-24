"""平台检测器（单通道代理 meta.json）单元测试

覆盖新检测逻辑：
- 代理访问 meta.json，published_products_count > 20 判定 Shopify；
- ≤20 / 无字段 / 404 / 410 → 非 Shopify；
- 访问失败/被拦（429/503/超时/代理不可用）→ inconclusive，不误杀真店；
- engine._detect_platforms 二轮复检后仍无法确认的站 → uncertain（不算非 Shopify）。
"""

import threading

import pytest

from qmds.modules.data_scraper import engine as engine_mod
from qmds.modules.data_scraper import product_crawler as pc_mod
from qmds.modules.data_scraper.detection import platform as platform_mod
from qmds.modules.data_scraper.detection.platform import (
    SHOPIFY_MIN_PRODUCTS,
    DetectionResult,
    PlatformDetector,
)
from qmds.modules.data_scraper.engine import DataScraperModule
from qmds.modules.data_scraper.models.schemas import Platform

SHOPIFY_META = {"published_products_count": 150, "name": "Technique Records", "currency": "USD"}


@pytest.fixture(autouse=True)
def _skip_proxy_health(monkeypatch):
    """测试中跳过代理服务健康检查（避免真实网络请求拖慢/依赖外网）"""
    monkeypatch.setattr(engine_mod, "_proxy_health_checked", True)


class FakeProxyClient:
    """按 URL 路由返回 (data, status) 的假代理服务客户端"""

    def __init__(self, routes, default=(None, 404)):
        self.routes = routes
        self.default = default
        self.calls = []

    def fetch(self, url, timeout=None):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return self.default


def make_detector(monkeypatch, client):
    detector = PlatformDetector()
    detector._proxy_service = client
    detector._proxy_service_failed = False
    return detector


def meta_url_for(domain: str) -> str:
    return f"https://{domain}/meta.json"


class TestSingleChannelMeta:
    def test_meta_count_over_threshold_is_shopify(self, monkeypatch):
        client = FakeProxyClient({"meta.json": (SHOPIFY_META, 200)})
        result = make_detector(monkeypatch, client).detect("https://techniquerecords.com")
        assert result.platform == Platform.SHOPIFY
        assert result.product_count == 150
        assert result.store_name == "Technique Records"
        assert result.currency == "USD"
        assert result.confidence == 0.95
        assert result.inconclusive is False

    def test_meta_url_ends_with_meta_json(self, monkeypatch):
        client = FakeProxyClient({"meta.json": (SHOPIFY_META, 200)})
        make_detector(monkeypatch, client).detect("https://techniquerecords.com")
        assert client.calls[0] == "https://techniquerecords.com/meta.json"

    def test_url_without_scheme_normalized(self, monkeypatch):
        client = FakeProxyClient({"meta.json": (SHOPIFY_META, 200)})
        result = make_detector(monkeypatch, client).detect("techniquerecords.com")
        assert result.platform == Platform.SHOPIFY
        assert client.calls[0] == "https://techniquerecords.com/meta.json"

    def test_count_at_threshold_is_not_shopify(self, monkeypatch):
        """published_products_count == 20（临界值）不算 Shopify"""
        meta = dict(SHOPIFY_META, published_products_count=SHOPIFY_MIN_PRODUCTS)
        client = FakeProxyClient({"meta.json": (meta, 200)})
        result = make_detector(monkeypatch, client).detect("https://small-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_count_below_threshold_is_not_shopify(self, monkeypatch):
        meta = dict(SHOPIFY_META, published_products_count=12)
        client = FakeProxyClient({"meta.json": (meta, 200)})
        result = make_detector(monkeypatch, client).detect("https://small-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_json_without_count_field_is_not_shopify(self, monkeypatch):
        client = FakeProxyClient({"meta.json": ({"name": "X"}, 200)})
        result = make_detector(monkeypatch, client).detect("https://not-shopify.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_404_is_not_shopify(self, monkeypatch):
        client = FakeProxyClient({"meta.json": (None, 404)})
        result = make_detector(monkeypatch, client).detect("https://plain-site.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_410_is_not_shopify(self, monkeypatch):
        client = FakeProxyClient({"meta.json": (None, 410)})
        result = make_detector(monkeypatch, client).detect("https://gone-site.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False


class TestInconclusiveNoMisjudge:
    """访问失败/被拦绝不能作为"非 Shopify"定案（techniquerecords.com 教训）"""

    @pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504, -1, -2])
    def test_blocked_is_inconclusive(self, monkeypatch, status):
        client = FakeProxyClient({"meta.json": (None, status)})
        result = make_detector(monkeypatch, client).detect("https://blocked-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_single_timeout_is_not_shopify(self, monkeypatch):
        """单站挂起超时（杂站无响应）→ 判非 Shopify，不堆积待确认"""
        monkeypatch.setattr(platform_mod, "_consecutive_timeouts", 0)
        client = FakeProxyClient({"meta.json": (None, 0)})
        result = make_detector(monkeypatch, client).detect("https://slow-site.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_consecutive_timeouts_trigger_guard(self, monkeypatch):
        """连续超时（疑似代理服务故障）→ 恢复无法确认，防大面积误杀"""
        monkeypatch.setattr(platform_mod, "_consecutive_timeouts", 0)
        client = FakeProxyClient({"meta.json": (None, 0)})
        detector = make_detector(monkeypatch, client)
        results = [detector.detect(f"https://slow{i}.com") for i in range(platform_mod.MAX_CONSECUTIVE_TIMEOUTS)]
        # 前 MAX-1 个判非 Shopify，第 MAX 个触发防护判无法确认
        assert all(r.inconclusive is False for r in results[:-1])
        assert results[-1].inconclusive is True
        assert platform_mod._consecutive_timeouts >= platform_mod.MAX_CONSECUTIVE_TIMEOUTS

    def test_response_resets_timeout_counter(self, monkeypatch):
        """有响应（404）重置连续超时计数，后续单次超时不会触发防护"""
        monkeypatch.setattr(platform_mod, "_consecutive_timeouts", 0)
        client = FakeProxyClient({"meta.json": (None, 0)})
        detector = make_detector(monkeypatch, client)
        detector.detect("https://slow1.com")   # 超时 #1
        detector.detect("https://slow2.com")   # 超时 #2
        client.routes = {"meta.json": (None, 404)}  # 换成 404 → 重置计数
        detector.detect("https://ok.com")
        client.routes = {"meta.json": (None, 0)}   # 再超时
        result = detector.detect("https://slow3.com")
        assert result.inconclusive is False  # 计数已重置，未触发防护

    def test_proxy_unavailable_is_inconclusive(self, monkeypatch):
        detector = PlatformDetector()
        detector._proxy_service = None
        detector._proxy_service_failed = True  # 代理已停用
        result = detector.detect("https://blocked-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_unexpected_exception_is_inconclusive(self, monkeypatch):
        detector = PlatformDetector()

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        detector._get_proxy_service = boom
        result = detector.detect("https://weird-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_detect_none_is_inconclusive(self, monkeypatch):
        result = PlatformDetector().detect(None)
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True


# ── engine._detect_platforms：二轮复检后仍无法确认 → uncertain ──

class TestDetectPlatformsUncertain:
    def test_still_inconclusive_goes_to_uncertain(self, monkeypatch):
        """第一轮 a/b 不确定、c 是 Shopify；二轮 a 仍不确定、b 恢复 → a 进 uncertain"""
        module = DataScraperModule.__new__(DataScraperModule)
        module._lock = threading.Lock()
        module._executor = None  # 避免 __del__ 时 shutdown() 访问未初始化属性
        module._search_executor = None

        first_pass = {
            "https://a.com/": "inconclusive",
            "https://b.com/": "inconclusive",
            "https://c.com/": "shopify",
        }
        retry = {
            "https://a.com/": "inconclusive",
            "https://b.com/": "shopify",
        }

        class FakeDetector:
            def __init__(self):
                self.calls = []

            def detect(self, url, url_map=None):
                self.calls.append(url)
                state = retry.get(url) if self.calls.count(url) > 1 else first_pass.get(url, "none")
                if state == "shopify":
                    return DetectionResult(platform=Platform.SHOPIFY, product_count=30,
                                           store_name="Demo", currency="USD", confidence=0.95)
                if state == "inconclusive":
                    return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)
                return DetectionResult(platform=Platform.UNKNOWN)

        module.detector = FakeDetector()
        # 避免二轮复检前的 sleep 拖慢测试
        monkeypatch.setattr(engine_mod.time, "sleep", lambda *a, **k: None)

        urls = ["https://a.com/", "https://b.com/", "https://c.com/"]
        results, uncertain = module._detect_platforms(urls, {}, workers=3)

        assert set(results) == {"https://b.com/", "https://c.com/"}
        assert uncertain == ["https://a.com/"]  # 二轮仍被拦 → uncertain，不按非 Shopify 定案

    def test_all_shopify_no_uncertain(self, monkeypatch):
        module = DataScraperModule.__new__(DataScraperModule)
        module._lock = threading.Lock()
        module._executor = None
        module._search_executor = None

        class FakeDetector:
            def detect(self, url, url_map=None):
                return DetectionResult(platform=Platform.SHOPIFY, product_count=50,
                                       store_name="S", currency="USD", confidence=0.95)

        module.detector = FakeDetector()
        results, uncertain = module._detect_platforms(["https://x.com/"], {}, workers=2)
        assert set(results) == {"https://x.com/"}
        assert uncertain == []


class TestDetectConcurrencyCapped:
    def test_concurrency_capped_at_proxy_capacity(self):
        """检测并发被全局信号量压到代理服务容量（4），避免 STATUS_BUSY(-2) 大量不确定"""
        from qmds.modules.data_scraper import engine as engine_mod

        module = DataScraperModule.__new__(DataScraperModule)
        module._lock = threading.Lock()
        module._executor = None
        module._search_executor = None

        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        class FakeDetector:
            def detect(self, url, url_map=None):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                try:
                    return DetectionResult(platform=Platform.SHOPIFY, product_count=50,
                                           store_name="S", currency="USD", confidence=0.95)
                finally:
                    with state_lock:
                        state["active"] -= 1

        module.detector = FakeDetector()
        urls = [f"https://s{i}.com/" for i in range(62)]
        results, uncertain = module._detect_platforms(urls, {}, workers=16)
        assert len(results) == 62
        assert uncertain == []
        assert state["max_active"] <= engine_mod.PROXY_DETECT_CONCURRENCY,             f"max_active={state['max_active']} 超过代理容量 {engine_mod.PROXY_DETECT_CONCURRENCY}"

class TestProxyHealthCheck:
    def test_checked_only_once_per_process(self, monkeypatch):
        """健康检查进程内只探测一次，不随每个关键词重复请求"""
        calls = []

        class FakeClient:
            def __init__(self, *a, **k):
                calls.append("init")

            def fetch(self, u, timeout=None):
                calls.append(u)
                return None, 404

        monkeypatch.setattr(pc_mod, "ProxyServiceClient", FakeClient)
        monkeypatch.setattr(engine_mod, "_proxy_health_checked", False)
        module = DataScraperModule.__new__(DataScraperModule)
        module._executor = None
        module._search_executor = None
        module._check_proxy_health()
        module._check_proxy_health()
        probes = [c for c in calls if isinstance(c, str) and c.startswith("https")]
        assert len(probes) == 1, f"应只探测一次，实际 {len(probes)}"

    def test_failure_does_not_raise(self, monkeypatch):
        """代理服务故障（status=0）时健康检查只告警不抛异常"""
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def fetch(self, u, timeout=None):
                return None, 0

        monkeypatch.setattr(pc_mod, "ProxyServiceClient", FakeClient)
        monkeypatch.setattr(engine_mod, "_proxy_health_checked", False)
        module = DataScraperModule.__new__(DataScraperModule)
        module._executor = None
        module._search_executor = None
        module._check_proxy_health()  # 不应抛异常


