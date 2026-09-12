# -*- coding: utf-8 -*-
"""网站分类统计 + AgentRouter 模型调用 + AI 生成网站信息 单元测试

覆盖：
- category_stats：分类层级拆分、跨表聚合、统计表写入/读取回环
  （分配流程内嵌统计的端到端见 test_data_allocator.py）；
- llm_models：AgentRouter 模型注册、自定义模型 ID 解析、API Key 解析、
  extra_body 关闭；
- site_info_generator：提示词构建、返回解析、任务端到端（stub LLM 调用，
  自动生成统计表 + 写出 网站信息.json）。
"""

import json
import shutil
import sys
import uuid
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.config.llm_models import (
    DEFAULT_AGENTROUTER_MODEL,
    get_llm_api_key,
    get_llm_default_headers,
    get_llm_extra_body,
    get_llm_model_config,
    has_llm_api_key,
)
from qmds.utils import agentrouter_client
from qmds.utils.agentrouter_client import fetch_agentrouter_models
from qmds.modules.web.services.category_stats import (
    INFO_FILE_NAME,
    STATS_FILE_NAME,
    aggregate_folder_categories,
    build_stats_rows,
    collect_stats_files,
    read_stats_excel,
    split_category_levels,
    write_stats_excel,
)
from qmds.modules.web.services import site_info_generator
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    build_site_info_prompt,
    collect_site_folders,
    parse_site_info,
    read_site_info_excel,
    run_batch_site_info_task,
    run_site_info_task,
)
from qmds.modules.web.task_manager import task_manager

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"catstats_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_df(categories):
    """按 [分类名 x 数量] 生成测试数据（含空分类行）"""
    rows = []
    for i, cat in enumerate(categories):
        for j in range(cat[1]):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": cat[0], "Images": "", "cf_opingts": "",
                         "自定义分类": "", "原站域名": "example.com",
                         "分布网站识别": 0, "语言": "en"})
    return pd.DataFrame(rows, columns=EXPORT_COLUMNS)


class StubSiteDB:
    """测试用 SiteDB 桩：提供 get_setting / set_setting / close"""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        return True

    def close(self):
        pass


@pytest.fixture
def ar_env_isolated(monkeypatch):
    """清空 .env 提供的 AgentRouter 回退配置（key/模型/地址）

    llm_models 在 site_db 为空时会回退读 settings（.env）；开发机 .env
    可能保存了真实配置，必须清空才能测试「未配置」分支。
    """
    from qmds.config.settings import settings as settings_obj

    monkeypatch.setattr(settings_obj, "agentrouter_api_key", "", raising=False)
    monkeypatch.setattr(settings_obj, "agentrouter_model", "", raising=False)
    monkeypatch.setattr(settings_obj, "agentrouter_base_url",
                        "https://agentrouter.org/v1", raising=False)


# ── 分类层级拆分 ─────────────────────────────

def test_split_category_levels():
    assert split_category_levels("Hardware|||Plumbing & Fittings") == ["Hardware", "Plumbing & Fittings"]
    assert split_category_levels("Toilet Tank Lid") == ["Toilet Tank Lid"]
    assert split_category_levels("A|||B|||C") == ["A", "B", "C"]
    # 4 级：更深层级并入第三级
    assert split_category_levels("A|||B|||C|||D") == ["A", "B", "C > D"]
    assert split_category_levels("A|||B|||C|||D|||E") == ["A", "B", "C > D > E"]
    assert split_category_levels("") == []
    assert split_category_levels(None) == []
    assert split_category_levels("  |||  ") == []


# ── 文件收集与聚合 ─────────────────────────────

def test_collect_stats_files_skips_generated(workdir):
    """收集时跳过 Excel 临时文件、已生成的统计表与网站信息表"""
    (workdir / "a.xlsx").write_bytes(b"x")
    (workdir / "~$a.xlsx").write_bytes(b"x")
    (workdir / STATS_FILE_NAME).write_bytes(b"x")
    (workdir / INFO_FILE_NAME).write_bytes(b"x")
    sub = workdir / "sub"
    sub.mkdir()
    (sub / "b.xlsx").write_bytes(b"x")
    (workdir / "c.txt").write_bytes(b"x")

    files = collect_stats_files(workdir)
    assert [p.name for p in files] == ["a.xlsx", "b.xlsx"]


def test_aggregate_folder_categories(workdir):
    """跨表聚合分类计数，跳过无分类列的表格"""
    df1 = make_df([("Cat A", 5), ("Cat B", 3)])
    df1.to_excel(workdir / "main1.xlsx", index=False, engine="openpyxl")
    df2 = make_df([("Cat A", 2), ("", 4)])
    df2.to_excel(workdir / "supp1.xlsx", index=False, engine="openpyxl")
    # 无分类列的表格被跳过
    pd.DataFrame({"A": [1]}).to_excel(workdir / "bad.xlsx", index=False, engine="openpyxl")

    files = collect_stats_files(workdir)
    agg = aggregate_folder_categories(files)

    assert agg["counts"] == Counter({"Cat A": 7, "Cat B": 3})
    assert agg["total_rows"] == 14  # 8 (main1) + 6 (supp2)，bad.xlsx 被跳过
    assert agg["empty_rows"] == 4
    assert [f[0] for f in agg["files"]] == ["main1.xlsx", "supp1.xlsx"]
    assert [s[0] for s in agg["skipped"]] == ["bad.xlsx"]


# ── 统计表写入 / 读取回环 ─────────────────────────────

def test_write_and_read_stats_excel(workdir):
    agg = {"counts": Counter({"Hardware|||Plumbing": 30, "Toilet Tank Lid": 50}),
           "total_rows": 85, "empty_rows": 5,
           "files": [("a.xlsx", 85)], "skipped": []}
    out = write_stats_excel(workdir / STATS_FILE_NAME, agg, folder_label="MySite")

    assert out.name == STATS_FILE_NAME
    stats = read_stats_excel(out)
    cats = stats["categories"]
    # 按产品数降序
    assert [c["category"] for c in cats] == ["Toilet Tank Lid", "Hardware|||Plumbing"]
    assert cats[0]["count"] == 50
    assert cats[0]["level1"] == "Toilet Tank Lid"
    assert cats[1]["level1"] == "Hardware"
    assert cats[1]["level2"] == "Plumbing"
    assert cats[1]["count"] == 30
    # 汇总指标
    assert stats["summary"]["数据文件夹"] == "MySite"
    assert stats["summary"]["产品总数"] == 85
    assert stats["summary"]["分类总数"] == 2
    assert stats["summary"]["一级分类数"] == 2


def test_read_stats_excel_missing(workdir):
    with pytest.raises(FileNotFoundError):
        read_stats_excel(workdir / STATS_FILE_NAME)


def test_build_stats_rows_order_and_pct():
    rows = build_stats_rows({"A": 75, "B": 25})
    assert rows[0]["分类"] == "A"
    assert rows[0]["产品数"] == 75
    assert rows[0]["占比(%)"] == 75.0
    assert rows[1]["占比(%)"] == 25.0


# ── AgentRouter 模型注册与解析（模型名不硬编码） ─────────────────────────────

def test_agentrouter_model_registry():
    """注册表中只有一个动态 agentrouter 条目，不含任何硬编码平台模型名"""
    assert DEFAULT_AGENTROUTER_MODEL == "agentrouter"
    ar_entries = [m for m in _all_models()
                  if m.get("provider") == "agentrouter"]
    assert len(ar_entries) == 1
    assert ar_entries[0]["value"] == "agentrouter"
    # 占位 model_id：真实模型 ID 只能来自设置或实时列表选择
    assert ar_entries[0]["model_id"] == ""

    cfg = get_llm_model_config(
        "agentrouter", StubSiteDB({"agentrouter_model": "real-model-x"}))
    assert cfg["provider"] == "agentrouter"
    assert cfg["base_url"] == "https://agentrouter.org/v1"
    assert cfg["model_id"] == "real-model-x"

    assert get_llm_extra_body(cfg) is None  # AgentRouter 不发 thinking 参数
    assert get_llm_extra_body(get_llm_model_config("mimo-v2.5")) is not None


def _all_models():
    from qmds.config.llm_models import list_llm_models
    return list_llm_models()


def test_agentrouter_model_id_resolution_priority(ar_env_isolated):
    """模型 ID 解析优先级：显式覆盖 > site_db 设置 > .env；未选择时明确报错"""
    site_db = StubSiteDB({"agentrouter_model": "from-setting"})
    # 显式覆盖（来自平台实时列表的选择）优先
    cfg = get_llm_model_config("agentrouter", site_db,
                               model_id_override="from-live-list")
    assert cfg["model_id"] == "from-live-list"
    # 其次 site_db 设置
    assert get_llm_model_config("agentrouter", site_db)["model_id"] == "from-setting"

    # 未选择任何模型时给出明确错误（不回退到硬编码模型名）
    with pytest.raises(ValueError, match="AgentRouter 未选择模型"):
        get_llm_model_config("agentrouter", StubSiteDB())


def test_agentrouter_base_url_override():
    """API 地址可从配置覆盖（平台如提供其他接口地址时无需改代码）"""
    site_db = StubSiteDB({"agentrouter_model": "m",
                          "agentrouter_base_url": "https://ar.example.com/v1/"})
    cfg = get_llm_model_config("agentrouter", site_db)
    assert cfg["base_url"] == "https://ar.example.com/v1"


def test_agentrouter_api_key_resolution(ar_env_isolated):
    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    cfg = get_llm_model_config("agentrouter",
                               StubSiteDB({"agentrouter_model": "m"}))
    assert get_llm_api_key(cfg, site_db) == "sk-ar-test"
    assert has_llm_api_key(cfg, site_db) is True

    # 未配置时明确报错 / 返回 False
    empty_cfg = get_llm_model_config("agentrouter",
                                     StubSiteDB({"agentrouter_model": "m"}))
    with pytest.raises(RuntimeError, match="AgentRouter API Key"):
        get_llm_api_key(empty_cfg, StubSiteDB())
    assert has_llm_api_key(empty_cfg, StubSiteDB()) is False


# ── AgentRouter 真实连接（/models 接口，mock 网络层） ─────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_fetch_agentrouter_models_openai_shape(monkeypatch):
    """OpenAI 标准响应: {"data": [{"id": ...}]}；请求须带平台白名单 UA（实测）"""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["auth"] = headers.get("Authorization", "")
        captured["ua"] = headers.get("User-Agent", "")
        return _FakeResponse(payload={"object": "list",
                                      "data": [{"id": "model-b"},
                                               {"id": "model-a"},
                                               {"id": "model-b"}]})

    monkeypatch.setattr(agentrouter_client.requests, "get", fake_get)
    models = fetch_agentrouter_models("sk-ar-test",
                                      "https://agentrouter.org/v1")
    assert models == ["model-a", "model-b"]  # 去重排序
    assert captured["url"] == "https://agentrouter.org/v1/models"
    assert captured["auth"] == "Bearer sk-ar-test"
    # 平台按 UA 白名单拦截（python-requests/OpenAI SDK 默认 UA -> 401），
    # 必须携带编程工具 UA
    assert captured["ua"] == agentrouter_client.TOOL_USER_AGENT
    assert captured["ua"]  # 非空


def test_fetch_agentrouter_models_list_shape(monkeypatch):
    """简单列表响应: ["model-a", ...]"""
    monkeypatch.setattr(agentrouter_client.requests, "get",
                        lambda url, headers=None, timeout=None:
                        _FakeResponse(payload=["x-model", "y-model"]))
    assert fetch_agentrouter_models("k", "https://agentrouter.org/v1") == ["x-model", "y-model"]


def test_fetch_agentrouter_models_errors(monkeypatch):
    """被拒(401) / 非 JSON 响应 / 未配置 Key 均给出明确错误"""
    monkeypatch.setattr(agentrouter_client.requests, "get",
                        lambda url, headers=None, timeout=None:
                        _FakeResponse(status_code=401, text="unauthorized client detected"))
    with pytest.raises(RuntimeError, match="拒绝访问"):
        fetch_agentrouter_models("bad-key")

    monkeypatch.setattr(agentrouter_client.requests, "get",
                        lambda url, headers=None, timeout=None:
                        _FakeResponse(text="<html>gateway error</html>"))
    with pytest.raises(RuntimeError, match="不是 JSON"):
        fetch_agentrouter_models("k")

    with pytest.raises(ValueError, match="未配置"):
        fetch_agentrouter_models("  ")


def test_llm_default_headers_whitelist_ua():
    """AgentRouter 的 OpenAI client 必须携带平台白名单 UA，其他 provider 不带"""
    ar_cfg = get_llm_model_config(
        "agentrouter", StubSiteDB({"agentrouter_model": "m"}))
    headers = get_llm_default_headers(ar_cfg)
    assert headers["User-Agent"] == agentrouter_client.TOOL_USER_AGENT

    mimo_cfg = get_llm_model_config("mimo-v2.5")
    assert get_llm_default_headers(mimo_cfg) == {}
    ark_cfg = get_llm_model_config("ark-ep-20260822151623")
    assert get_llm_default_headers(ark_cfg) == {}


# ── AgentRouter 模型列表缓存（获取一次后保存，页面加载不重新获取） ───────────────

def test_ar_models_cache_roundtrip():
    """缓存读写回环：保存后按相同 base_url 可读回，含拉取时间"""
    from qmds.modules.web.routes.product_data import (
        _load_ar_models_cache,
        _save_ar_models_cache,
    )

    db = StubSiteDB()
    fetched_at = _save_ar_models_cache(db, ["model-a", "model-b"],
                                       "https://agentrouter.org/v1")
    assert fetched_at  # 返回拉取时间

    cache = _load_ar_models_cache(db, "https://agentrouter.org/v1")
    assert cache["models"] == ["model-a", "model-b"]
    assert cache["fetched_at"] == fetched_at

    # 尾部斜杠差异不影响匹配
    assert _load_ar_models_cache(db, "https://agentrouter.org/v1/")["models"]


def test_ar_models_cache_base_url_mismatch():
    """base_url 变更后缓存失效（不同网关的模型列表不同）"""
    from qmds.modules.web.routes.product_data import (
        _load_ar_models_cache,
        _save_ar_models_cache,
    )

    db = StubSiteDB()
    _save_ar_models_cache(db, ["model-a"], "https://agentrouter.org/v1")
    other = _load_ar_models_cache(db, "https://other.example.com/v1")
    assert other == {"models": [], "fetched_at": ""}


def test_ar_models_cache_corrupt_or_missing():
    """无缓存 / JSON 损坏 / 结构异常时安全回退为空"""
    from qmds.modules.web.routes.product_data import (
        AR_MODELS_CACHE_KEY,
        _load_ar_models_cache,
    )

    assert _load_ar_models_cache(StubSiteDB(), "https://agentrouter.org/v1") == {
        "models": [], "fetched_at": ""}

    bad = StubSiteDB({AR_MODELS_CACHE_KEY: "{not json"})
    assert _load_ar_models_cache(bad, "https://agentrouter.org/v1")["models"] == []

    wrong_shape = StubSiteDB({AR_MODELS_CACHE_KEY: '["plain", "list"]'})
    assert _load_ar_models_cache(wrong_shape, "https://agentrouter.org/v1")["models"] == []


def test_api_agentrouter_models_saves_cache(monkeypatch):
    """路由层：拉取成功后自动写入缓存；返回体带 fetched_at"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    db = StubSiteDB({"agentrouter_api_key": "sk-ar-test",
                     "agentrouter_model": "model-b"})
    monkeypatch.setattr(product_data, "get_site_db", lambda: db)

    def fake_fetch(api_key, base_url, timeout=30):
        return ["model-a", "model-b"]

    monkeypatch.setattr(product_data, "fetch_agentrouter_models", fake_fetch)

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/api/product-data/agentrouter/models")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["data"]["models"] == ["model-a", "model-b"]
    assert payload["data"]["saved_model"] == "model-b"
    assert payload["data"]["fetched_at"]  # 返回拉取时间

    # 缓存已写入 site_db（下次页面加载直接使用，不重新获取）
    cache = product_data._load_ar_models_cache(db, "https://agentrouter.org/v1")
    assert cache["models"] == ["model-a", "model-b"]
    assert cache["fetched_at"] == payload["data"]["fetched_at"]


def test_export_page_uses_cached_models(monkeypatch):
    """导出页渲染：模型 ID 为下拉框，选项直接用缓存（不连平台），预选默认模型"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    db = StubSiteDB({"agentrouter_api_key": "sk-ar-test",
                     "agentrouter_model": "model-b"})
    product_data._save_ar_models_cache(db, ["model-a", "model-b", "model-c"],
                                       "https://agentrouter.org/v1")
    monkeypatch.setattr(product_data, "get_site_db", lambda: db)

    # 防御：页面渲染路径绝不能触发真实平台连接
    def no_fetch(*args, **kwargs):
        raise AssertionError("页面渲染不应连接平台")

    monkeypatch.setattr(product_data, "fetch_agentrouter_models", no_fetch)

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/product-data/export")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # 模型 ID 是真正的下拉框（select），name 仍为 model_id
    assert '<select id="info_model_id" name="model_id"' in html
    # 下拉选项来自缓存，已保存默认模型被预选
    for m in ("model-a", "model-b", "model-c"):
        assert f'<option value="{m}"' in html
    assert '<option value="model-b" selected' in html
    # 保留手动输入入口
    assert 'value="__custom__"' in html
    assert 'name="model_id_manual"' in html
    # 状态提示显示缓存信息
    assert "已缓存 3 个模型" in html


def test_site_info_route_custom_model_id(workdir, monkeypatch):
    """路由层：下拉选「手动输入」时取配套文本框的值作为模型 ID"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    captured = {}

    def fake_task(task_id, folder, model, model_id, site_db=None):
        captured["model"] = model
        captured["model_id"] = model_id

    monkeypatch.setattr(product_data, "run_batch_site_info_task", fake_task)

    make_df([("Cat", 3)]).to_excel(workdir / "mainCat.xlsx",
                                   index=False, engine="openpyxl")
    export_dir = Path("data/exports") / "site_info_route_test"
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    shutil.move(str(workdir / "mainCat.xlsx"), export_dir / "mainCat.xlsx")

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # 选择「手动输入」+ 填入自定义模型 ID
    resp = client.post("/product-data/site-info", data={
        "folder": "site_info_route_test",
        "model": "agentrouter",
        "model_id": "__custom__",
        "model_id_manual": "my-custom-model",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert captured["model_id"] == "my-custom-model"

    shutil.rmtree(export_dir, ignore_errors=True)


# ── AI 生成网站信息 ─────────────────────────────

def test_build_site_info_prompt():
    stats = {"categories": [
                 {"category": "Toilet Tank Lid", "count": 320, "level1": "Toilet Tank Lid"},
                 {"category": "Hardware|||Plumbing", "count": 120,
                  "level1": "Hardware", "level2": "Plumbing"},
             ],
             "summary": {"产品总数": 440}}
    prompt = build_site_info_prompt(stats, "Toilet_Tank_Lid")
    # 主类目名已清洗（下划线还原空格）且突出展示
    assert "STORE SPECIALTY (main category): Toilet Tank Lid" in prompt
    assert "Toilet_Tank_Lid" not in prompt
    assert "440" in prompt
    # 主类目相关分类在主打区，补充分类在背景区
    assert "Toilet Tank Lid" in prompt
    assert "Hardware|||Plumbing" in prompt
    assert "SPECIALTY categories" in prompt
    assert "SUPPLEMENTARY product mix" in prompt
    assert "domain" in prompt and "theme" in prompt and "keywords" in prompt
    # 英文网站、面向美国用户
    assert "United States" in prompt


def test_build_site_info_prompt_filters_junk_and_splits():
    """垃圾类目过滤 + 主类目分类置顶主打、补充数据降级为背景"""
    cats = [
        {"category": "Toilet Tank Lid", "count": 320, "level1": "Toilet Tank Lid"},
        {"category": "Hardware|||Plumbing", "count": 120,
         "level1": "Hardware", "level2": "Plumbing"},
        {"category": "25", "count": 9, "level1": "25"},               # 纯数字垃圾
        {"category": "New Products", "count": 8, "level1": "New Products"},  # 占位垃圾
    ]
    stats = {"categories": cats, "summary": {"产品总数": 457}}
    prompt = build_site_info_prompt(stats, "Toilet_Tank_Lid")

    # 垃圾类目被过滤
    assert "[9] 25" not in prompt
    assert "New Products" not in prompt
    # 主打区含主类目分类，补充区含 Hardware（且在主打区之后）
    assert "[320] Toilet Tank Lid" in prompt
    core_pos = prompt.index("SPECIALTY categories")
    supp_pos = prompt.index("SUPPLEMENTARY product mix")
    assert core_pos < supp_pos
    assert prompt.index("Hardware|||Plumbing") > supp_pos
    # 明确指示补充数据不得主导品牌信息
    assert "do NOT let these dominate" in prompt


def test_build_site_info_prompt_core_not_dominated_by_extra():
    """主类目仅数百条、补充数据数万条时，主打区仍是主类目分类（真实痛点场景）"""
    cats = [{"category": "2-piece Toilets - Toilet Tanks", "count": 178,
             "level1": "2-piece Toilets - Toilet Tanks"},
            {"category": "2-piece Toilets - Toilet Bowls", "count": 129,
             "level1": "2-piece Toilets - Toilet Bowls"}]
    cats += [{"category": f"Hardware|||Sub {i}", "count": 500 - i,
              "level1": "Hardware", "level2": f"Sub {i}"} for i in range(40)]
    stats = {"categories": cats, "summary": {"产品总数": 19527}}

    prompt = build_site_info_prompt(stats, "2-piece_Toilets_-_Toilet_Tanks")
    # 文件夹名下划线还原为空格、连续破折号折叠
    assert "STORE SPECIALTY (main category): 2-piece Toilets - Toilet Tanks" in prompt
    # 主打区：两个主类目分类（词元重叠匹配）；Hardware 全部在背景区
    core_pos = prompt.index("SPECIALTY categories")
    supp_pos = prompt.index("SUPPLEMENTARY product mix")
    assert prompt.index("2-piece Toilets - Toilet Tanks", core_pos) < supp_pos
    assert prompt.index("2-piece Toilets - Toilet Bowls", core_pos) < supp_pos
    assert prompt.index("Hardware|||Sub 0") > supp_pos
    # 数量标注：主类目产品数 vs 补充产品数
    assert "307 in the specialty" in prompt
    assert "19220 supplementary" in prompt


def test_parse_site_info():
    info = parse_site_info(json.dumps({
        "domain": "toilet-tank-lids-store.com",
        "theme": "Toilet Parts",
        "title": "Toilet Tank Lids & Repair Parts Store",
        "description": "Shop replacement toilet tank lids.",
        "address": "123 Main St, Austin, TX 78701",
        "keywords": ["toilet tank lid", "tank lid replacement"],
    }))
    assert info["domain"] == "toilet-tank-lids-store.com"
    assert info["title"] == "Toilet Tank Lids & Repair Parts Store"
    assert info["keywords"] == ["toilet tank lid", "tank lid replacement"]

    # 域名容错：带 www. / https:// 前缀时剥离
    info2 = parse_site_info(json.dumps({
        "domain": "https://WWW.Example-Store.com/",
        "title": "T", "description": "D"}))
    assert info2["domain"] == "example-store.com"

    # markdown 围栏剥离
    fenced = "```json\n" + json.dumps(
        {"domain": "a.com", "title": "T", "description": "D"}) + "\n```"
    assert parse_site_info(fenced)["title"] == "T"

    # 缺少必填字段报错
    with pytest.raises(ValueError, match="domain"):
        parse_site_info(json.dumps({"title": "T", "description": "D"}))
    with pytest.raises(ValueError, match="title"):
        parse_site_info(json.dumps({"domain": "a.com", "description": "D"}))
    with pytest.raises(ValueError, match="description"):
        parse_site_info(json.dumps({"domain": "a.com", "title": "T"}))


def test_run_site_info_task(workdir, monkeypatch):
    """端到端（stub LLM）：自动生成统计表 -> 调用 LLM -> 写出 网站信息.xlsx"""
    site = workdir / "Toilet_Tank_Lid"
    site.mkdir()
    make_df([("Toilet Tank Lid", 12), ("Other", 5)]).to_excel(
        site / "mainToilet_Tank_Lid.xlsx", index=False, engine="openpyxl")

    captured = {}

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        captured["model_id"] = config["model_id"]
        captured["api_key"] = api_key
        captured["prompt"] = prompt
        return {"domain": "toilet-tank-lids-store.com",
                "theme": "Toilet Parts",
                "title": "Toilet Tank Lids & Repair Parts",
                "description": "Quality replacement toilet tank lids.",
                "address": "123 Main St, Austin, TX 78701",
                "keywords": ["toilet tank lid", "toilet tank"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    task_id = "test_site_info"
    task_manager.create(task_id, "site_info", "test")
    # model_id 来自平台实时列表的选择（此处用假名 stub LLM 层，不实际联网）
    run_site_info_task(task_id, site, "agentrouter", "live-model-x",
                       site_db=site_db)

    assert task_manager.get(task_id)["status"] == "completed", task_manager.get_logs(task_id)

    # 自动生成了分类统计表
    stats_path = site / STATS_FILE_NAME
    assert stats_path.is_file()

    # LLM 收到正确的模型与提示词
    assert captured["model_id"] == "live-model-x"
    assert captured["api_key"] == "sk-ar-test"
    assert "Toilet Tank Lid" in captured["prompt"]

    # 网站信息已保存为表格（一行）
    info_path = site / INFO_FILE_NAME
    assert info_path.is_file()
    rows = read_site_info_excel(info_path)
    assert len(rows) == 1
    result = rows[0]
    assert result["域名"] == "toilet-tank-lids-store.com"
    assert result["标题"] == "Toilet Tank Lids & Repair Parts"
    assert result["地址"] == "123 Main St, Austin, TX 78701"
    assert result["关键词"] == "toilet tank lid, toilet tank"
    assert result["主类目"] == "Toilet Tank Lid"
    assert result["模型"] == "live-model-x"
    assert result["产品数"] == 17


def test_run_site_info_task_model_override(workdir, monkeypatch):
    """模型 ID 覆盖：替换所选模型的 model_id"""
    make_df([("Cat", 3)]).to_excel(
        workdir / "mainCat.xlsx", index=False, engine="openpyxl")

    captured = {}

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        captured["model_id"] = config["model_id"]
        return {"domain": "t.com", "theme": "T", "title": "Title",
                "description": "Desc",
                "address": "1 Main St, Austin, TX 78701", "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    task_id = "test_site_info_override"
    task_manager.create(task_id, "site_info", "test")
    run_site_info_task(task_id, workdir, "agentrouter",
                       "another-live-model", site_db=site_db)

    assert task_manager.get(task_id)["status"] == "completed", task_manager.get_logs(task_id)
    assert captured["model_id"] == "another-live-model"


def test_run_site_info_task_missing_key(workdir, monkeypatch, ar_env_isolated):
    """已选模型但未配置 AgentRouter API Key -> 任务失败并提示"""
    make_df([("Cat", 3)]).to_excel(
        workdir / "mainCat.xlsx", index=False, engine="openpyxl")

    def fail_llm(config, api_key, prompt, log_fn=None):
        raise AssertionError("不应调用 LLM")

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fail_llm)

    task_id = "test_site_info_nokey"
    task_manager.create(task_id, "site_info", "test")
    run_site_info_task(task_id, workdir, "agentrouter", "live-model-x",
                       site_db=StubSiteDB())

    task = task_manager.get(task_id)
    assert task["status"] == "failed"
    assert "AgentRouter API Key" in task["message"]


def test_run_site_info_task_no_model_selected(workdir, monkeypatch, ar_env_isolated):
    """未从平台选择任何模型（也无默认模型）-> 明确失败，不回退硬编码模型名"""
    make_df([("Cat", 3)]).to_excel(
        workdir / "mainCat.xlsx", index=False, engine="openpyxl")

    def fail_llm(config, api_key, prompt, log_fn=None):
        raise AssertionError("不应调用 LLM")

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fail_llm)

    task_id = "test_site_info_nomodel"
    task_manager.create(task_id, "site_info", "test")
    run_site_info_task(task_id, workdir, "agentrouter", "",
                       site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "failed"
    assert "AgentRouter 未选择模型" in task["message"]


def test_run_site_info_task_uses_existing_stats(workdir, monkeypatch):
    """已有统计表时直接读取，不重新扫描"""
    agg = {"counts": Counter({"Existing Cat": 9}), "total_rows": 9, "empty_rows": 0,
           "files": [("x.xlsx", 9)], "skipped": []}
    write_stats_excel(workdir / STATS_FILE_NAME, agg, folder_label=workdir.name)

    captured = {}

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        captured["prompt"] = prompt
        return {"domain": "t.com", "theme": "T", "title": "Title",
                "description": "Desc",
                "address": "1 Main St, Austin, TX 78701", "keywords": []}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    task_id = "test_site_info_existing"
    task_manager.create(task_id, "site_info", "test")
    run_site_info_task(task_id, workdir, "agentrouter", "live-model-x",
                       site_db=site_db)

    assert task_manager.get(task_id)["status"] == "completed", task_manager.get_logs(task_id)
    assert "Existing Cat" in captured["prompt"]


# ── 批量生成：网站数据文件夹收集 + 逐个顺序生成 ─────────────────────────────

def test_collect_site_folders(workdir):
    """收集最后一层文件夹：每个含表格的叶子目录 = 一个网站；extra 跳过"""
    # 分配输出结构: root/Site_A/*.xlsx  root/Site_B/*.xlsx  root/extra1/*.xlsx
    for name, cats in (("Site_A", [("Cat A", 3)]),
                       ("Site_B", [("Cat B", 5)]),
                       ("extra1", [("Extra", 7)])):
        d = workdir / name
        d.mkdir()
        make_df(cats).to_excel(d / "main.xlsx", index=False, engine="openpyxl")

    sites = collect_site_folders(workdir)
    assert [s.name for s in sites] == ["Site_A", "Site_B"]  # extra1 排除

    # 深层嵌套：root/deep/Site_C/*.xlsx（最后一层是 Site_C）
    deep = workdir / "deep" / "Site_C"
    deep.mkdir(parents=True)
    make_df([("Cat C", 2)]).to_excel(deep / "main.xlsx", index=False,
                                     engine="openpyxl")
    sites = collect_site_folders(workdir)
    assert sorted(s.name for s in sites) == ["Site_A", "Site_B", "Site_C"]

    # 空叶子目录（无表格）不算网站
    (workdir / "empty_leaf").mkdir()
    assert collect_site_folders(workdir) == sites

    # root 本身是数据文件夹（无子文件夹、有表格）-> 作为唯一网站
    leaf = workdir / "single_site"
    leaf.mkdir()
    make_df([("Only", 4)]).to_excel(leaf / "main.xlsx", index=False,
                                    engine="openpyxl")
    assert collect_site_folders(leaf) == [leaf]

    # 只有统计表（数据表已清理）的叶子文件夹也算网站
    stats_only = workdir / "stats_only"
    stats_only.mkdir()
    agg = {"counts": Counter({"S": 1}), "total_rows": 1, "empty_rows": 0,
           "files": [("x.xlsx", 1)], "skipped": []}
    write_stats_excel(stats_only / STATS_FILE_NAME, agg, folder_label="stats_only")
    assert collect_site_folders(stats_only) == [stats_only]

    # root 本身无表格且无子文件夹 -> 空
    bare = workdir / "bare"
    bare.mkdir()
    assert collect_site_folders(bare) == []


def test_run_batch_site_info_task(workdir, monkeypatch):
    """批量任务：按顺序逐个网站生成，每次只送一个网站的分类结构

    结构: workdir/Site_A + Site_B + extra1（应跳过）；
    Site_A 无统计表（自动补生成），Site_B 已有统计表（直接复用）。
    """
    # Site_A / extra1：只有数据表
    for name, cats in (("Site_A", [("Faucet", 6), ("Sink", 2)]),
                       ("extra1", [("Extra", 7)])):
        d = workdir / name
        d.mkdir()
        make_df(cats).to_excel(d / "main.xlsx", index=False, engine="openpyxl")
    # Site_B：已有统计表
    site_b = workdir / "Site_B"
    site_b.mkdir()
    agg = {"counts": Counter({"Door Hardware": 9}), "total_rows": 9,
           "empty_rows": 0, "files": [("x.xlsx", 9)], "skipped": []}
    write_stats_excel(site_b / STATS_FILE_NAME, agg, folder_label="Site_B")

    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        # 每次提示词只含当前网站的分类（不混入其他网站的分类）
        if "Faucet" in prompt:
            assert "Door Hardware" not in prompt
            return {"domain": "bathroom-faucets-store.com", "theme": "Bathroom",
                    "title": "Bathroom Faucets & Sinks Store",
                    "description": "Quality bathroom faucets and sinks.",
                    "address": "1 Main St, Austin, TX 78701",
                    "keywords": ["bathroom faucet"]}
        assert "Faucet" not in prompt
        return {"domain": "door-hardware-store.com", "theme": "Hardware",
                "title": "Door Hardware Store",
                "description": "Door hardware and accessories.",
                "address": "2 Oak Ave, Dallas, TX 75201",
                "keywords": ["door hardware"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    task_id = "test_batch_site_info"
    task_manager.create(task_id, "site_info", "test")
    run_batch_site_info_task(task_id, workdir, "agentrouter", "live-model-x",
                             site_db=site_db)

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "批量生成 2/2" in task["message"], task["message"]

    # 顺序调用两次（Site_A 在前，extra1 被跳过）
    assert len(calls) == 2
    assert "Faucet" in calls[0] and "Door Hardware" in calls[1]

    # 所有网站汇总到所选文件夹下的一张 网站信息.xlsx（每行一个网站）
    table = workdir / INFO_FILE_NAME
    assert table.is_file()
    rows = read_site_info_excel(table)
    assert len(rows) == 2  # extra1 被跳过
    by_site = {r["网站（文件夹）"]: r for r in rows}
    assert by_site["Site_A"]["域名"] == "bathroom-faucets-store.com"
    assert by_site["Site_A"]["标题"] == "Bathroom Faucets & Sinks Store"
    assert by_site["Site_A"]["备注"] == ""
    assert by_site["Site_B"]["域名"] == "door-hardware-store.com"
    assert "extra1" not in by_site
    # 网站子文件夹不再生成单独的信息文件
    assert not (workdir / "Site_A" / INFO_FILE_NAME).exists()
    # Site_A 缺统计表被自动补生成
    assert (workdir / "Site_A" / STATS_FILE_NAME).is_file()


def test_run_batch_site_info_task_failure_continues(workdir, monkeypatch):
    """批量任务：单个网站失败记录日志并继续，汇总失败数"""
    for name in ("Site_A", "Site_B"):
        d = workdir / name
        d.mkdir()
        make_df([(f"Cat {name}", 3)]).to_excel(d / "main.xlsx", index=False,
                                               engine="openpyxl")

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        if "Site_A" in prompt or "Cat Site_A" in prompt:
            raise RuntimeError("模型返回异常")
        return {"domain": "site-b-store.com", "theme": "B", "title": "Site B Store",
                "description": "Site B description.",
                "address": "3 Elm St, Denver, CO 80202", "keywords": ["b"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    site_db = StubSiteDB({"agentrouter_api_key": "sk-ar-test"})
    task_id = "test_batch_fail"
    task_manager.create(task_id, "site_info", "test")
    run_batch_site_info_task(task_id, workdir, "agentrouter", "live-model-x",
                             site_db=site_db)

    task = task_manager.get(task_id)
    # Site_A 失败但 Site_B 成功 -> 任务整体完成（部分成功）
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "批量生成 1/2" in task["message"], task["message"]
    assert "失败 1" in task["message"], task["message"]
    # 表格：成功行 + 失败行（备注列记录原因）
    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    by_site = {r["网站（文件夹）"]: r for r in rows}
    assert len(rows) == 2
    assert by_site["Site_B"]["域名"] == "site-b-store.com"
    assert by_site["Site_A"]["域名"] == ""
    assert "模型返回异常" in by_site["Site_A"]["备注"]

    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("Site_A 生成失败" in m for m in logs)
    assert any("继续下一个" in m for m in logs)


def test_run_batch_site_info_task_no_sites(workdir):
    """所选文件夹下没有网站数据文件夹 -> 明确失败"""
    task_id = "test_batch_nosites"
    task_manager.create(task_id, "site_info", "test")
    run_batch_site_info_task(task_id, workdir, "agentrouter", "live-model-x",
                             site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "failed"
    assert "没有找到网站数据文件夹" in task["message"]
