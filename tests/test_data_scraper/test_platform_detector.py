"""PlatformDetector 漏判防护测试

验证检测机制不会把被拦截/临时故障的 Shopify 站点误判为"非 Shopify"：
- 403/429/5xx 等可重试状态 -> inconclusive（交由上层二轮复检），而非否定结论
- Magento /static/version 的 catch-all 200 不再误判（避免挤占 Shopify 兜底判定）
"""

from qmds.modules.data_scraper.detection import platform as platform_mod
from qmds.modules.data_scraper.detection.platform import (
    PlatformDetector,
    _is_bare_version_text,
    _is_retryable_status,
)
from qmds.modules.data_scraper.models.schemas import Platform


class FakeResponse:
    def __init__(self, status_code, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("not json")


def make_detector():
    detector = PlatformDetector()
    # 永久停用远程代理服务复检、cloudscraper 复检与 DNS 预检，测试不发起真实网络请求
    detector._proxy_service_failed = True
    detector.cloudflare_fallback_enabled = False
    detector.dns_check_enabled = False
    return detector


def patch_responses(monkeypatch, routes, homepage=None):
    """按 URL 子串路由返回 FakeResponse；首页（以 / 结尾）可用 homepage 指定，其余返回 404"""

    def fake_request(url, proxy_manager=None, headers=None, timeout=15, max_retries=2):
        for key, resp in routes.items():
            if key in url:
                return resp
        if homepage is not None and url.endswith("/"):
            return homepage
        return FakeResponse(404, text="not found")

    monkeypatch.setattr(platform_mod, "_request_with_retry", fake_request)


PLAIN_HTML = "<html><head><title>Store</title></head><body>Hello world</body></html>"
SHOPIFY_META = {"published_products_count": 12, "name": "Demo", "currency": "USD"}


class TestRetryableStatus:
    def test_blocked_statuses_retryable(self):
        for code in (403, 429, 500, 502, 503, 504):
            assert _is_retryable_status(code), code

    def test_definitive_statuses_not_retryable(self):
        for code in (200, 301, 302, 404, 410):
            assert not _is_retryable_status(code), code


class TestBareVersionText:
    def test_version_string_accepted(self):
        assert _is_bare_version_text("1.2.3")
        assert _is_bare_version_text("  1712345678  ")

    def test_catchall_html_rejected(self):
        assert not _is_bare_version_text("<!doctype html><html>home page</html>")
        assert not _is_bare_version_text("<html>redirected home page</html>")

    def test_empty_or_long_rejected(self):
        assert not _is_bare_version_text("")
        assert not _is_bare_version_text("x" * 200)


class TestDetectInconclusive:
    def test_meta_json_429_is_inconclusive(self, monkeypatch):
        """meta.json 429（限流）不应作为非 Shopify 定案"""
        patch_responses(monkeypatch, {"meta.json": FakeResponse(429)})
        result = make_detector().detect("https://blocked-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_meta_json_503_is_inconclusive(self, monkeypatch):
        patch_responses(monkeypatch, {"meta.json": FakeResponse(503)})
        result = make_detector().detect("https://blocked-shop.com")
        assert result.inconclusive is True

    def test_meta_404_with_blocked_homepage_is_inconclusive(self, monkeypatch):
        """meta.json 404 + 首页被拦：不能定案（headless Shopify 也可能如此）"""
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(404)},
            homepage=FakeResponse(403),
        )
        result = make_detector().detect("https://not-shopify.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_unexpected_exception_is_inconclusive(self):
        """detect() 内部未预见异常不构成否定证据"""
        result = make_detector().detect(None)
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is True

    def test_clean_negative_not_inconclusive(self, monkeypatch):
        """meta.json 404 + 首页 200 无任何特征：确认非 Shopify（不进二轮复检）"""
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(404)},
            homepage=FakeResponse(200, text=PLAIN_HTML),
        )
        result = make_detector().detect("https://plain-site.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False


class TestDetectPositive:
    def test_meta_json_confirms_shopify(self, monkeypatch):
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(200, json_data=SHOPIFY_META)},
            homepage=FakeResponse(200, text=PLAIN_HTML),
        )
        result = make_detector().detect("https://real-shop.com")
        assert result.platform == Platform.SHOPIFY
        assert result.product_count == 12
        assert result.confidence == 1.0
        assert result.inconclusive is False

    def test_strong_homepage_fingerprint_confirms_shopify(self, monkeypatch):
        """直连被拦（403）+ 首页强指纹（cdn.shopify.）→ 确认 Shopify

        旧版只认 meta.json，被拦的真店全部被误杀（成功率 <10% 的主因之一）。
        """
        shopify_html = '<html><script src="https://cdn.shopify.com/s/files/x.js"></script></html>'
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(403)},
            homepage=FakeResponse(200, text=shopify_html),
        )
        result = make_detector().detect("https://fingerprint-shop.com")
        assert result.platform == Platform.SHOPIFY
        assert result.confidence == 0.9
        assert result.inconclusive is False

    def test_weak_homepage_fingerprint_not_enough(self, monkeypatch):
        """弱特征（window.shopify 等）不能单独确认 Shopify，防误报"""
        weak_html = '<html><script>window.shopify = {};</script></html>'
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(403)},
            homepage=FakeResponse(200, text=weak_html),
        )
        result = make_detector().detect("https://weak-shop.com")
        assert result.platform == Platform.UNKNOWN
        assert result.inconclusive is False

    def test_proxy_502_not_definitive_negative(self, monkeypatch):
        """远程代理 502 不得作为"非 Shopify"定案（历史误杀主因）

        修复前：代理 502 → 判定非 Shopify → 真店永久丢失；
        修复后：502 视为无法确定，继续走首页强指纹确认。
        """
        detector = make_detector()
        monkeypatch.setattr(
            detector, "_detect_shopify_via_proxy_service",
            lambda meta_url: (None, 502),
        )
        shopify_html = '<html><script src="https://cdn.shopify.com/s/files/x.js"></script></html>'
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(403)},
            homepage=FakeResponse(200, text=shopify_html),
        )
        result = detector.detect("https://blocked-real-shop.com")
        assert result.platform == Platform.SHOPIFY
        assert result.confidence == 0.9

    def test_dns_hit_confirms_shopify(self, monkeypatch):
        """域名解析到 Shopify 边缘 IP 段（23.227.38.0/24）→ 直接收录"""
        detector = make_detector()
        detector.dns_check_enabled = True
        monkeypatch.setattr(detector, "_dns_points_to_shopify", lambda domain: True)
        patch_responses(
            monkeypatch,
            {"meta.json": FakeResponse(404)},
        )
        result = detector.detect("https://dns-shop.com")
        assert result.platform == Platform.SHOPIFY
        assert result.confidence == 0.85


class TestMagentoFalsePositive:
    def test_catchall_static_version_not_magento(self, monkeypatch):
        """catch-all 对 /static/version 返回整页 200 时不应判为 Magento

        修复前：Shopify 店（meta.json 被屏蔽 + catch-all）会被判成 Magento，
        提前返回后永远走不到 Shopify 首页指纹兜底，最终被 engine 丢弃。
        """
        catchall = "<!doctype html><html><body>redirected home page</body></html>"
        patch_responses(
            monkeypatch,
            {
                "meta.json": FakeResponse(404),
                "static/version": FakeResponse(200, text=catchall),
            },
            homepage=FakeResponse(200, text=PLAIN_HTML),
        )
        result = make_detector().detect("https://catchall-shop.com")
        assert result.platform == Platform.UNKNOWN

    def test_real_magento_version_still_detected(self, monkeypatch):
        """收紧判据不影响真实 Magento 检测（meta 被拦 → 首页干净 → 平台识别）"""
        patch_responses(
            monkeypatch,
            {
                "meta.json": FakeResponse(403),
                "magento_version": FakeResponse(200, text="Magento/2.4 (Community)"),
            },
            homepage=FakeResponse(200, text=PLAIN_HTML),
        )
        result = make_detector().detect("https://real-magento.com")
        assert result.platform == Platform.MAGENTO
