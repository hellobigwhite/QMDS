# -*- coding: utf-8 -*-
"""本地代理池可用性探测测试

覆盖：单代理探测判定、并发探测过滤、缓存、以及 settings.load_proxies()
在「关闭 / 开启全不可用 / 开启部分可用」三种状态下的返回。
"""

import pytest
import requests

import os
import uuid
from pathlib import Path

from qmds.config import settings
from qmds.core.exceptions import RateLimitError
from qmds.utils import proxy_probe
from qmds.utils.http_client import HttpClient

# 沙箱环境无法用 pytest 的 tmp_path（basetemp 清理被拒），
# 改在工作区 .tmp/ 下手动建临时代理文件。
_TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "probe_tests"


def _make_proxies_file(text: str) -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    p = _TMP_ROOT / f"proxies_{uuid.uuid4().hex[:8]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


class FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """每个用例前后重置进程级禁用/缓存，避免用例互相污染"""
    proxy_probe.reset_pool_ban()
    proxy_probe.clear_cache()
    yield
    proxy_probe.reset_pool_ban()
    proxy_probe.clear_cache()


# ── test_proxy ─────────────────────────────────────────────

def test_test_proxy_ok(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["proxies"] = kw.get("proxies")
        seen["timeout"] = kw.get("timeout")
        return FakeResp(200)

    monkeypatch.setattr(proxy_probe.requests, "get", fake_get)
    ok, reason = proxy_probe.test_proxy("http://p:1", target="https://t/", timeout=7)
    assert ok is True
    assert "200" in reason
    assert seen["proxies"] == {"http": "http://p:1", "https": "http://p:1"}
    assert seen["timeout"] == 7


def test_test_proxy_non_200(monkeypatch):
    monkeypatch.setattr(proxy_probe.requests, "get", lambda url, **kw: FakeResp(403))
    ok, reason = proxy_probe.test_proxy("http://p:1", target="https://t/")
    assert ok is False
    assert "403" in reason


def test_test_proxy_proxy_error(monkeypatch):
    def boom(url, **kw):
        raise requests.exceptions.ProxyError("402 payment required")

    monkeypatch.setattr(proxy_probe.requests, "get", boom)
    ok, reason = proxy_probe.test_proxy("http://p:1", target="https://t/")
    assert ok is False
    assert "ProxyError" in reason


def test_test_proxy_timeout(monkeypatch):
    def boom(url, **kw):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(proxy_probe.requests, "get", boom)
    ok, reason = proxy_probe.test_proxy("http://p:1", target="https://t/")
    assert ok is False
    assert "ReadTimeout" in reason


# ── probe_proxies / cached_probe ───────────────────────────

def test_probe_proxies_filters(monkeypatch):
    results = {"http://a:1": True, "http://b:1": False, "http://c:1": True}
    monkeypatch.setattr(proxy_probe, "test_proxy",
                        lambda p, target, timeout: (results[p], ""))
    ok = proxy_probe.probe_proxies(["http://a:1", "http://b:1", "http://c:1"], target="t")
    assert ok == ["http://a:1", "http://c:1"]


def test_probe_proxies_empty():
    assert proxy_probe.probe_proxies([], target="t") == []


def test_cached_probe_caches(monkeypatch):
    calls = []

    def fake_probe(proxies, **kw):
        calls.append(1)
        return ["http://a:1"]

    monkeypatch.setattr(proxy_probe, "probe_proxies", fake_probe)
    proxy_probe.clear_cache()
    assert proxy_probe.cached_probe(["http://a:1"], ttl=300) == ["http://a:1"]
    assert proxy_probe.cached_probe(["http://a:1"], ttl=300) == ["http://a:1"]
    assert len(calls) == 1  # 第二次走缓存
    proxy_probe.clear_cache()


# ── settings.load_proxies ──────────────────────────────────

def test_load_proxies_disabled(monkeypatch):
    f = _make_proxies_file("http://a:1\nhttp://b:2\n")
    monkeypatch.setattr(settings, "proxies_file", f)
    monkeypatch.setattr(settings, "local_proxy_pool_enabled", False)
    assert settings.load_proxies() == []


def test_load_proxies_enabled_all_bad(monkeypatch):
    f = _make_proxies_file("http://a:1\nhttp://b:2\n")
    monkeypatch.setattr(settings, "proxies_file", f)
    monkeypatch.setattr(settings, "local_proxy_pool_enabled", True)
    monkeypatch.setattr(proxy_probe, "cached_probe", lambda proxies, **kw: [])
    proxy_probe.clear_cache()
    assert settings.load_proxies() == []


def test_load_proxies_enabled_some_ok(monkeypatch):
    f = _make_proxies_file("http://a:1\nhttp://b:2\n")
    monkeypatch.setattr(settings, "proxies_file", f)
    monkeypatch.setattr(settings, "local_proxy_pool_enabled", True)
    monkeypatch.setattr(proxy_probe, "cached_probe", lambda proxies, **kw: ["http://b:2"])
    proxy_probe.clear_cache()
    assert settings.load_proxies() == ["http://b:2"]


def test_probe_all_bad_bans(monkeypatch):
    """探测全部不可用 -> 直接禁止本地代理（不进入冷却）"""
    monkeypatch.setattr(proxy_probe, "test_proxy", lambda p, target, timeout: (False, "x"))
    ok = proxy_probe.probe_proxies(["http://a:1", "http://b:1"], target="t")
    assert ok == []
    assert proxy_probe.is_local_pool_banned() is True
    # 禁止后 cached_probe 不再发起探测，直接返回空
    calls = []
    monkeypatch.setattr(proxy_probe, "probe_proxies",
                        lambda proxies, **kw: (calls.append(1) or ["http://a:1"]))
    assert proxy_probe.cached_probe(["http://a:1"]) == []
    assert calls == []


def test_cached_probe_banned_returns_empty(monkeypatch):
    """已被禁止时 cached_probe 直接返回空，不重复探测"""
    proxy_probe.ban_local_pool("test")
    calls = []
    monkeypatch.setattr(proxy_probe, "probe_proxies",
                        lambda proxies, **kw: (calls.append(1) or ["http://a:1"]))
    assert proxy_probe.cached_probe(["http://a:1"]) == []
    assert calls == []


def test_load_proxies_banned(monkeypatch):
    """已被禁止时 load_proxies 直接返回空（即使总开关开启）"""
    f = _make_proxies_file("http://a:1\n")
    monkeypatch.setattr(settings, "proxies_file", f)
    monkeypatch.setattr(settings, "local_proxy_pool_enabled", True)
    proxy_probe.ban_local_pool("test")
    assert settings.load_proxies() == []


class _FakePM:
    def get_proxy(self):
        return {"http": "http://p:1", "https": "http://p:1"}

    def mark_bad(self, proxy_dict, cooldown=60.0):
        pass


class _FakeSession:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def request(self, method, url, **kw):
        if self._exc is not None:
            raise self._exc
        return self._resp


def test_http_client_banned_uses_direct(monkeypatch):
    """禁止后 HttpClient 即使持有 proxy_manager 也直接走直连"""
    hc = HttpClient(proxy_manager=_FakePM())
    sent = {}
    class S:
        def request(self, method, url, **kw):
            sent["proxies"] = kw.get("proxies")
            return FakeResp(200)
    hc._session = S()
    proxy_probe.ban_local_pool("test")
    hc.get("https://x/")
    assert sent["proxies"] is None


def test_http_client_429_bans_pool():
    """本地代理请求收到 429 -> 直接禁止整池"""
    hc = HttpClient(proxy_manager=_FakePM())
    hc._session = _FakeSession(resp=FakeResp(429))
    with pytest.raises(RateLimitError):
        hc.get("https://x/")
    assert proxy_probe.is_local_pool_banned() is True


def test_http_client_retry429_bans_pool():
    """urllib3 重试耗尽（too many 429）-> 直接禁止整池（用户日志场景）"""
    hc = HttpClient(proxy_manager=_FakePM())
    hc._session = _FakeSession(exc=requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: / "
        "(Caused by ResponseError('too many 429 error responses'))"
    ))
    with pytest.raises(Exception):
        hc.get("https://x/")
    assert proxy_probe.is_local_pool_banned() is True


def test_load_proxies_converts_and_probes(monkeypatch):
    f = _make_proxies_file("ip:port:user:pw\n")
    monkeypatch.setattr(settings, "proxies_file", f)
    monkeypatch.setattr(settings, "local_proxy_pool_enabled", True)
    seen = []

    def fake_cached(proxies, **kw):
        seen.extend(proxies)
        return proxies

    monkeypatch.setattr(proxy_probe, "cached_probe", fake_cached)
    proxy_probe.clear_cache()
    got = settings.load_proxies()
    assert got == ["http://user:pw@ip:port"]
    assert seen == ["http://user:pw@ip:port"]