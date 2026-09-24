"""Serper API key 调用次数记录测试

验证 KeyUsageTracker：
- 计数累计与线程安全
- 原子持久化到 JSON，重新加载不丢失
- SerperProvider.search 每次发起请求都会记录（不重复计数）
"""

import json
import shutil
import threading
import uuid
from pathlib import Path

import pytest

from qmds.config import search_providers as sp
from qmds.config.search_providers import (
    KeyPool,
    KeyUsageTracker,
    ProviderConfig,
    SerperProvider,
    _get_serper_usage_tracker,
    get_serper_key_usage,
)

CFG = ProviderConfig(name="serper", keys_file="", base_url="https://google.serper.dev/search",
                     method="POST", timeout=5)


@pytest.fixture
def usage_dir():
    """工作区内的临时目录（系统 TEMP 被沙箱限制，不能用 pytest tmp_path）"""
    d = Path(f".tmp_usage_test_{uuid.uuid4().hex[:8]}")
    d.mkdir(exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestKeyUsageTracker:
    def test_record_and_summary(self, usage_dir):
        tracker = KeyUsageTracker(filepath=usage_dir / "usage.json")
        tracker.record_call("key-a")
        tracker.record_call("key-a")
        tracker.record_call("key-b")
        assert tracker.summary() == {"key-a": 2, "key-b": 1}

    def test_persistence_across_instances(self, usage_dir):
        path = usage_dir / "usage.json"
        tracker1 = KeyUsageTracker(filepath=path)
        tracker1.record_call("key-a")
        tracker1.record_call("key-a")
        tracker1.record_call("key-b")

        tracker2 = KeyUsageTracker(filepath=path)
        assert tracker2.summary() == {"key-a": 2, "key-b": 1}

    def test_thread_safety(self, usage_dir):
        tracker = KeyUsageTracker(filepath=usage_dir / "usage.json")
        errors = []

        def worker():
            try:
                for _ in range(200):
                    tracker.record_call("key-a")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert tracker.summary()["key-a"] == 1600

    def test_load_ignores_corrupt_file(self, usage_dir):
        path = usage_dir / "usage.json"
        path.write_text("not json{{{", encoding="utf-8")
        tracker = KeyUsageTracker(filepath=path)
        assert tracker.summary() == {}
        tracker.record_call("key-a")
        assert tracker.summary() == {"key-a": 1}


class TestSerperUsageHook:
    def test_search_records_call_once(self, usage_dir, monkeypatch):
        """每次 search() 发起请求只记录一次计数"""
        tracker = KeyUsageTracker(filepath=usage_dir / "usage.json")
        monkeypatch.setattr(sp, "_serper_usage_tracker", tracker)

        pool = KeyPool("serper", ["key-1", "key-2"])
        prov = SerperProvider(CFG, pool)

        class FakeResp:
            status_code = 200
            text = json.dumps({"organic": [{"link": "https://shop1.com/"}]})
            def raise_for_status(self):
                pass
            def json(self):
                return json.loads(self.text)

        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())
        prov.search("test query", 1)
        prov.search("test query", 1)
        # KeyPool 轮换：两次调用分别用 key-1、key-2，各记录一次
        assert tracker.summary() == {"key-1": 1, "key-2": 1}

    def test_no_key_no_record(self, usage_dir, monkeypatch):
        tracker = KeyUsageTracker(filepath=usage_dir / "usage.json")
        monkeypatch.setattr(sp, "_serper_usage_tracker", tracker)

        pool = KeyPool("serper", [])
        prov = SerperProvider(CFG, pool)
        assert prov.search("test", 1) == []
        assert tracker.summary() == {}

    def test_module_level_accessor(self, usage_dir, monkeypatch):
        tracker = KeyUsageTracker(filepath=usage_dir / "usage.json")
        monkeypatch.setattr(sp, "_serper_usage_tracker", tracker)
        tracker.record_call("key-z")
        assert get_serper_key_usage() == {"key-z": 1}
        assert _get_serper_usage_tracker() is tracker
