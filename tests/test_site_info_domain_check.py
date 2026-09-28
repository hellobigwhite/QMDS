# -*- coding: utf-8 -*-
"""生成域名占用检查测试（西部数码 whois）

覆盖：
- whois 页面解析：未注册 / 已注册 / 无法判断（未注册页的科普文案不误判）
- check_domain_available：进程内缓存、失败不缓存
- _process_site_folder：已注册域名换创意方向重新生成、连续失败报错、
  查询失败保留域名并在备注标注、check_domain=False 时跳过查询
- 提示词：已占用域名写进 ALREADY TAKEN 段禁止复用
"""

import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest

from qmds.modules.web.services import site_info_generator
from qmds.modules.web.services.site_info_generator import (
    DOMAIN_REGEN_RETRIES,
    _parse_whois_page,
    build_site_info_prompt,
    check_domain_available,
)
from qmds.modules.web.task_manager import task_manager

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]

CONFIG = {"provider": "agentrouter", "model_id": "m",
          "base_url": "https://agentrouter.org/v1", "label": "t"}

STATS = {"categories": [{"category": "Bathroom|||Faucets", "count": 5}],
         "summary": {"产品总数": 5}}


@pytest.fixture
def workdir():
    d = Path(".tmp") / f"domain_check_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_site(root: Path, name: str, cat: str):
    site = root / name
    site.mkdir()
    df = pd.DataFrame([{"SKU": f"S{i}", "Name": f"P{i}", "Description": "d",
                        "Regular price": 9.9, "Categories": cat, "Images": "",
                        "cf_opingts": "", "自定义分类": "卫浴", "原站域名": "e.com",
                        "分布网站识别": 0, "语言": "en"} for i in range(5)],
                      columns=EXPORT_COLUMNS)
    df.to_excel(site / f"main{name}.xlsx", index=False, engine="openpyxl")
    return site


def _fake_llm_factory(domains):
    """按调用顺序返回域名（超出则重复最后一个）"""
    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        domain = domains[min(len(calls) - 1, len(domains) - 1)]
        return {"domain": domain, "theme": "T", "title": "Title", "description": "Desc",
                "address": "1 Main St, Fort Collins, CO 80521", "keywords": ["k"]}

    return fake_llm, calls


# ── whois 页面解析 ────────────────────────────

def test_parse_whois_page_free():
    html = ('<div>该域名可能尚未注册（或被注册局保留、限制注册,若是VIP保留域名，'
            '可联系经纪人代购）, xx.com 查询能否注册</div>')
    assert _parse_whois_page(html) is True


def test_parse_whois_page_taken():
    html = '<td>Sponsoring Registrar</td><td>MarkMonitor Inc.</td>'
    assert _parse_whois_page(html) is False
    assert _parse_whois_page("Registry Domain ID: 2138514_DOMAIN_COM-VRSN") is False


def test_parse_whois_page_taken_not_misjudged_by_js_template():
    """已注册页的 JS 模板里含「该域名可能尚未注册」，不能因此判成未注册"""
    html = ("var value = value.replace(/(No match for)/, "
            "\"<span class='fgreen'>该域名可能尚未注册</span>\");"
            "Domain Name: GOOGLE.COM Registry Domain ID: 2138514_DOMAIN_COM-VRSN "
            "Sponsoring Registrar: MarkMonitor Inc.")
    assert _parse_whois_page(html) is False


def test_parse_whois_page_free_with_boilerplate():
    """未注册页的科普文案含「注册日期/注册商」等词也不能判成已注册"""
    html = ('域名whois查询简单来说，就是一个用来查询域名是否已经被注册，以及注册域名的'
            '详细信息的数据库（如域名所有人、域名注册商、域名注册日期和过期日期等）'
            'xx.com 查询能否注册')
    assert _parse_whois_page(html) is True


def test_parse_whois_page_unknown():
    assert _parse_whois_page("<html>页面结构变了</html>") is None
    assert _parse_whois_page("") is None


# ── check_domain_available ────────────────────

def test_check_domain_available_cached(monkeypatch):
    site_info_generator._domain_check_cache.clear()
    calls = []

    class FakeResp:
        encoding = "gb2312"
        text = "xx.com 查询能否注册"

    class FakeSession:
        def get(self, url, timeout=None):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr(site_info_generator, "_get_domain_check_session",
                        lambda: FakeSession())
    assert check_domain_available("Cache-Test.com") is True   # 大写会被规范化
    assert check_domain_available("cache-test.com") is True
    assert len(calls) == 1                     # 第二次走缓存
    assert calls[0].endswith("cache-test.com")
    site_info_generator._domain_check_cache.clear()


def test_check_domain_available_failure_not_cached(monkeypatch):
    site_info_generator._domain_check_cache.clear()

    class FakeSession:
        def get(self, url, timeout=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(site_info_generator, "_get_domain_check_session",
                        lambda: FakeSession())
    monkeypatch.setattr(site_info_generator.time, "sleep", lambda s: None)
    assert check_domain_available("fail-test.com") is None
    assert "fail-test.com" not in site_info_generator._domain_check_cache


# ── 提示词：已占用域名 ─────────────────────────

def test_prompt_lists_taken_domains():
    prompt = build_site_info_prompt(STATS, "Faucets",
                                    main_category="Bathroom|||Faucets",
                                    avoid_domains=["taken-a.com", "taken-b.com"])
    assert "ALREADY TAKEN" in prompt
    assert "- taken-a.com" in prompt and "- taken-b.com" in prompt
    # 不传时不出现该段
    assert "ALREADY TAKEN" not in build_site_info_prompt(STATS, "Faucets")


# ── _process_site_folder 集成 ─────────────────

def _run(workdir, monkeypatch, fake_llm, fake_check, **kwargs):
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)
    if fake_check is not None:
        monkeypatch.setattr(site_info_generator, "check_domain_available", fake_check)
    _make_site(workdir, "Faucets", "Bathroom|||Faucets")
    task_id = f"test_domain_{uuid.uuid4().hex[:8]}"
    task_manager.create(task_id, "site_info", "test")
    logs = []
    row = site_info_generator._process_site_folder(
        task_id, workdir / "Faucets", CONFIG, "sk-test",
        lambda m, level="info": logs.append(m), **kwargs)
    return row, logs


def test_taken_domain_regenerates(workdir, monkeypatch):
    """第一个域名已被注册：换创意方向重新生成，采用第二个未被注册的域名"""
    fake_llm, calls = _fake_llm_factory(["taken.com", "free.com"])
    checks = []

    def fake_check(domain, log_fn=None):
        checks.append(domain)
        return domain != "taken.com"

    row, logs = _run(workdir, monkeypatch, fake_llm, fake_check)
    assert row["域名"] == "free.com"
    assert checks == ["taken.com", "free.com"]
    assert len(calls) == 2
    # 第二次提示词带上了已占用域名 + 换了一套创意方向
    assert "ALREADY TAKEN" in calls[1] and "- taken.com" in calls[1]
    assert calls[0] != calls[1]
    assert any("已被注册" in m for m in logs)
    assert row["备注"] == ""


def test_all_domains_taken_raises(workdir, monkeypatch):
    """连续 DOMAIN_REGEN_RETRIES+1 次生成的域名都被注册：该网站失败"""
    fake_llm, calls = _fake_llm_factory(["taken1.com", "taken2.com", "taken3.com",
                                         "taken4.com", "taken5.com"])
    with pytest.raises(ValueError, match="均已被注册"):
        _run(workdir, monkeypatch, fake_llm, lambda d, log_fn=None: False)
    assert len(calls) == DOMAIN_REGEN_RETRIES + 1


def test_check_failure_keeps_domain_with_note(workdir, monkeypatch):
    """whois 查询失败（网络异常）：保留域名继续流程，备注标注未验证"""
    fake_llm, calls = _fake_llm_factory(["unknown.com"])
    row, logs = _run(workdir, monkeypatch, fake_llm, lambda d, log_fn=None: None)
    assert row["域名"] == "unknown.com"
    assert len(calls) == 1
    assert "未验证" in row["备注"]
    assert any("查询失败" in m for m in logs)


def test_check_disabled_skips_query(workdir, monkeypatch):
    """check_domain=False：不查询 whois（供离线/测试场景使用）"""
    fake_llm, calls = _fake_llm_factory(["any.com"])
    checks = []
    row, _logs = _run(workdir, monkeypatch, fake_llm,
                      lambda d, log_fn=None: checks.append(d) or True,
                      check_domain=False)
    assert row["域名"] == "any.com"
    assert checks == []
    assert len(calls) == 1
