# -*- coding: utf-8 -*-
"""网站信息生成反模板化测试（避免被识别为批量建站）

覆盖：
- 创意方向抽取：同 key 稳定（重试幂等）、不同 key 互不相同（批量差异化）；
- 提示词注入 CREATIVE DIRECTION（品牌声线/命名风格/城市）与反指纹规则
  （禁用套路化域名后缀/标题句式/描述开头/陈词滥调），且保留原有结构段落；
- LLM 调用温度随方向抖动（0.7-0.95），不再固定 0.4；
- 批量任务域名去重：冲突时换创意方向重试一次，仍冲突则明确失败。
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from qmds.modules.web.services import site_info_generator
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    _ADDRESS_CITIES,
    _site_creative_direction,
    build_site_info_prompt,
    read_site_info_excel,
)
from qmds.modules.web.task_manager import task_manager

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"antitemplate_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_df(categories):
    """按 [(分类名, 数量), ...] 生成测试数据表"""
    rows = []
    for i, (cat, n) in enumerate(categories):
        for j in range(n):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": cat, "Images": "", "cf_opingts": "",
                         "自定义分类": "", "原站域名": "example.com",
                         "分布网站识别": 0, "语言": "en"})
    return pd.DataFrame(rows, columns=EXPORT_COLUMNS)


import pandas as pd  # noqa: E402  (放在 make_df 附近便于阅读)


class StubSiteDB:
    """测试用 SiteDB 桩"""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        return True

    def close(self):
        pass


CATS = [{"category": "Toilets|||Toilet Tank Lids", "count": 320,
         "level1": "Toilets", "level2": "Toilet Tank Lids", "level3": ""},
        {"category": "Hardware|||Plumbing", "count": 120,
         "level1": "Hardware", "level2": "Plumbing", "level3": ""}]
STATS = {"categories": CATS, "summary": {"产品总数": 440}}


# ── 创意方向抽取 ─────────────────────────────

def test_creative_direction_stable_and_varies():
    """同一 key 方向固定（重试/重跑单站幂等），不同 key 方向互不相同"""
    d1 = _site_creative_direction("taskA|Faucets|v0")
    assert d1 == _site_creative_direction("taskA|Faucets|v0")
    assert 0.7 <= d1["temperature"] <= 0.95
    assert (d1["city"], d1["state"]) in _ADDRESS_CITIES

    # 不同网站（同批）方向不同：城市/声线/命名/标题风格等至少多项差异
    differs = 0
    for other in ("taskA|Door_Hardware|v0", "taskA|Sinks|v0", "taskA|Mirrors|v0"):
        d2 = _site_creative_direction(other)
        fields = ("voice", "tone", "domain_style", "title_style",
                  "desc_angle", "keyword_recipe", "city", "state")
        if any(d1[f] != d2[f] for f in fields):
            differs += 1
    assert differs >= 2, "不同网站的创意方向应互不相同"

    # 同一网站换 variant（域名冲突重试）方向变化
    assert _site_creative_direction("taskA|Faucets|v1") != d1


def test_creative_direction_city_pool_spread():
    """城市池覆盖足够多的州（地址地理分散，不扎堆热门城市）"""
    states = {state for _city, state in _ADDRESS_CITIES}
    assert len(states) >= 40
    # 刻意避开模型最爱扎堆的城市
    banned = {"austin", "denver", "portland", "miami", "seattle", "chicago",
              "new york", "los angeles", "san francisco", "boston"}
    for city, _state in _ADDRESS_CITIES:
        assert city.lower() not in banned


# ── 提示词：创意方向 + 反指纹规则 ─────────────────────

def test_prompt_contains_direction_and_rules():
    """提示词注入该站专属创意方向与反指纹规则，且保留原有结构段落"""
    direction = {
        "voice": "family-run shop, second generation, plain-spoken",
        "tone": "warm and conversational",
        "domain_style": "a two-word compound of two real English words",
        "title_style": "brand word first, then a plain descriptor after a colon",
        "title_len": "40-60", "desc_angle": "open with the concrete product range",
        "desc_len": "100-150", "keyword_recipe": "10-14 keywords, head terms first",
        "city": "Fort Collins", "state": "CO",
        "street_hint": "a simple street number and name",
        "temperature": 0.82,
    }
    prompt = build_site_info_prompt(STATS, "Toilets_Toilet_Tank_Lids",
                                    main_category="Toilets|||Toilet Tank Lids",
                                    direction=direction)

    # 原有结构保留（主类目/主打/补充段落，向后兼容）
    assert "STORE SPECIALTY (main category): Toilets|||Toilet Tank Lids" in prompt
    assert "SPECIALTY categories" in prompt
    assert "SUPPLEMENTARY product mix" in prompt
    assert "do NOT let these dominate" in prompt
    assert "United States" in prompt

    # 创意方向注入
    assert "CREATIVE DIRECTION" in prompt
    assert "family-run shop" in prompt
    assert "warm and conversational" in prompt
    assert "Fort Collins" in prompt and "CO" in prompt

    # 反指纹规则
    assert "one-stop shop" in prompt            # 陈词滥调禁用清单
    assert '"pro", "hub"' in prompt              # 套路化域名后缀禁用
    assert "Welcome to" in prompt                # 描述开头禁用
    assert "<keyword> Store" in prompt           # 标题句式禁用

    # 旧版锚定示例已移除（曾把整批域名带成 xxxpro.com）
    assert "toilettanklidpro" not in prompt


def test_prompt_differs_across_sites_and_is_deterministic():
    """不同网站的提示词互不相同（默认按文件夹名确定性抽取方向）"""
    folders = ["Faucets", "Door_Hardware", "Sinks", "Mirrors", "Tile"]
    prompts = [build_site_info_prompt(STATS, f) for f in folders]
    assert len(set(prompts)) > 1, "同批网站的提示词不应完全相同"

    # 同一网站重复构建完全一致（测试可复现）
    assert (build_site_info_prompt(STATS, "Faucets")
            == build_site_info_prompt(STATS, "Faucets"))


# ── LLM 调用温度 ─────────────────────────────

def test_llm_temperature_forwarded(monkeypatch):
    """创意方向的温度（0.7-0.95 抖动）透传到 LLM 调用"""
    captured = {}

    def fake_completion(client, *, config, messages, temperature,
                        max_completion_tokens, top_p, timeout):
        captured["temperature"] = temperature
        captured["max_completion_tokens"] = max_completion_tokens
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=(
                '{"domain": "lidworks.com", "theme": "T", "title": "Title",'
                ' "description": "Desc", "address": "1 Main St, Fort Collins, CO 80521",'
                ' "keywords": ["toilet tank lid"]}')))])

    monkeypatch.setattr(site_info_generator, "chat_completion_with_fallback",
                        fake_completion)
    config = {"provider": "agentrouter", "model_id": "m",
              "base_url": "https://agentrouter.org/v1", "label": "t"}

    info = site_info_generator._call_site_info_llm(
        config, "sk-test", "prompt", temperature=0.77)

    assert captured["temperature"] == 0.77
    assert info["domain"] == "lidworks.com"


# ── 批量任务：域名去重 ─────────────────────────────

def _make_site(root: Path, name: str, cat: str):
    site = root / name
    site.mkdir()
    make_df([(cat, 5)]).to_excel(site / f"main{name}.xlsx", index=False,
                                 engine="openpyxl")
    return site


def test_batch_domain_dedup_reroll(workdir, monkeypatch):
    """批量生成域名重复：换创意方向重试一次，重试域名不同则采用"""
    _make_site(workdir, "Faucets", "Bathroom|||Faucets")
    _make_site(workdir, "Door_Hardware", "Hardware|||Door Hardware")

    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        # 第 1 次: Faucets -> a.com；第 2 次: Door_Hardware v0 -> a.com（重复）；
        # 第 3 次: Door_Hardware v1（换创意方向）-> b.com
        domain = ["first-a.com", "first-a.com", "second-b.com"][len(calls) - 1]
        return {"domain": domain, "theme": "T", "title": "Title",
                "description": "Desc",
                "address": "1 Main St, Fort Collins, CO 80521",
                "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_dedup_reroll"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "批量生成 2/2" in task["message"]

    # 共 3 次调用：冲突站点重试一次；重试提示词方向变化（与冲突时不同）
    assert len(calls) == 3
    assert calls[1] != calls[2]

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    domains = {r["域名"] for r in rows}
    assert domains == {"first-a.com", "second-b.com"}

    # 冲突重试有告警日志
    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("重新生成" in m for m in logs)


def test_batch_domain_dedup_gives_up(workdir, monkeypatch):
    """域名重试后仍冲突：该网站记为失败（备注说明），其余网站不受影响"""
    _make_site(workdir, "Faucets", "Bathroom|||Faucets")
    _make_site(workdir, "Door_Hardware", "Hardware|||Door Hardware")

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        return {"domain": "always-same.com", "theme": "T", "title": "Title",
                "description": "Desc",
                "address": "1 Main St, Fort Collins, CO 80521",
                "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_dedup_giveup"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    # 第一个成功、第二个两次都冲突 -> 部分成功
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "批量生成 1/2" in task["message"]
    assert "失败 1" in task["message"]

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    by_site = {r["网站（文件夹）"]: r for r in rows}
    # 站点按字母序处理：Door_Hardware 先成功，Faucets 两次冲突失败
    assert by_site["Door_Hardware"]["域名"] == "always-same.com"
    assert "重复" in by_site["Faucets"]["备注"]


# ── 网站大类：数据表 自定义分类 列聚合 ─────────────────────

def _make_site_table(site: Path, name: str, custom_cats):
    """写一个带 自定义分类 值的数据表：custom_cats = [(自定义分类, 行数), ...]"""
    rows = []
    for i, (custom, n) in enumerate(custom_cats):
        for j in range(n):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": f"Cat{i}", "Images": "", "cf_opingts": "",
                         "自定义分类": custom, "原站域名": "old-site.com",
                         "分布网站识别": 0, "语言": "en"})
    pd.DataFrame(rows, columns=EXPORT_COLUMNS).to_excel(
        site / name, index=False, engine="openpyxl")


def test_resolve_site_major_category(workdir):
    """网站大类 = 该网站数据表 自定义分类 列的唯一值

    数据的自定义分类就是网站的大类，一个网站只能有一个；正常数据
    全表一致。多值残留（历史英文透传）时取行数最多者；计数 dict
    仍返回完整分布供告警。统计表/信息表不参与聚合。
    """
    from qmds.modules.web.services.site_info_generator import (
        resolve_site_major_category)

    site = workdir / "Site_A"
    site.mkdir()
    _make_site_table(site, "mainFaucets.xlsx", [("动物", 10)])
    _make_site_table(site, "Faucets_supp_part1.xlsx",
                     [("动物", 6), ("艺术与娱乐", 3)])
    # 分类统计/网站信息表不应参与聚合（也不应导致读取失败）
    pd.DataFrame([{"指标": "x"}]).to_excel(site / "分类统计.xlsx", index=False)
    pd.DataFrame([{"网站（文件夹）": "Site_A"}]).to_excel(
        site / "网站信息.xlsx", index=False)

    # 单一值（全表一致）
    single = workdir / "Site_Single"
    single.mkdir()
    _make_site_table(single, "mainX.xlsx", [("动物", 12)])
    assert resolve_site_major_category(single) == ("动物", {"动物": 12})

    # 多值残留：取行数最多者（唯一值返回，计数保留完整分布）
    value, counts = resolve_site_major_category(site)
    assert value == "动物"
    assert counts == {"动物": 16, "艺术与娱乐": 3}

    # 英文残留（旧版透传）映射为中文后与中文值合并计数，不分裂多值
    mixed = workdir / "Site_Mixed"
    mixed.mkdir()
    _make_site_table(mixed, "mainM.xlsx",
                     [("animals pet supplies", 7), ("动物", 3)])
    assert resolve_site_major_category(mixed) == ("动物", {"动物": 10})


def test_resolve_site_major_category_empty(workdir):
    """自定义分类 列全空（旧数据）或没有数据表 -> ("", {})"""
    from qmds.modules.web.services.site_info_generator import (
        resolve_site_major_category)

    site = workdir / "Site_B"
    site.mkdir()
    make_df([("Cat", 3)]).to_excel(site / "mainB.xlsx", index=False,
                                   engine="openpyxl")  # 自定义分类 全 ""
    assert resolve_site_major_category(site) == ("", {})

    empty = workdir / "Site_C"
    empty.mkdir()
    assert resolve_site_major_category(empty) == ("", {})


def test_repair_row_major_category(workdir):
    """历史行（网站大类为空/过期混合值）从数据表刷新为唯一值"""
    from qmds.modules.web.services.site_info_generator import (
        repair_row_major_category)

    site = workdir / "Site_D"
    site.mkdir()
    _make_site_table(site, "mainD.xlsx", [("五金", 8)])

    # 空值：补写
    row = {"网站（文件夹）": "Site_D", "主类目": "Faucets", "网站大类": ""}
    assert repair_row_major_category(site, row) is True
    assert row["网站大类"] == "五金"

    # 与聚合结果一致的值：不修改
    row2 = {"网站（文件夹）": "Site_D", "网站大类": "五金"}
    assert repair_row_major_category(site, row2) is False
    assert row2["网站大类"] == "五金"

    # 过期值（早期回填的混合值等）：刷新为当前唯一值
    row3 = {"网站（文件夹）": "Site_D", "网站大类": "动物, 五金"}
    assert repair_row_major_category(site, row3) is True
    assert row3["网站大类"] == "五金"

    # 无数据表的文件夹：不修改
    empty = workdir / "Site_E"
    empty.mkdir()
    row4 = {"网站（文件夹）": "Site_E", "网站大类": ""}
    assert repair_row_major_category(empty, row4) is False


def test_info_excel_roundtrip_keeps_major_category(workdir):
    """INFO_COLUMNS 含 网站大类（主类目之后），读写往返保留该列"""
    from qmds.modules.web.services.site_info_generator import (
        INFO_COLUMNS, _write_info_excel)

    assert INFO_COLUMNS.index("网站大类") == INFO_COLUMNS.index("主类目") + 1

    rows = [{"网站（文件夹）": "Site_A", "主类目": "Faucets",
             "网站大类": "动物", "域名": "a.com", "备注": ""}]
    _write_info_excel(workdir / INFO_FILE_NAME, rows)
    back = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert back[0]["网站大类"] == "动物"


def test_batch_task_fills_major_category(workdir, monkeypatch):
    """批量生成：网站大类 列随行写入（数据表 自定义分类 聚合）"""
    site = workdir / "Faucets"
    site.mkdir()
    _make_site_table(site, "mainFaucets.xlsx", [("动物", 5)])

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        return {"domain": "faucet-works.com", "theme": "T", "title": "Title",
                "description": "Desc",
                "address": "1 Main St, Fort Collins, CO 80521",
                "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_major_cat"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert rows[0]["网站大类"] == "动物"
    # 日志中体现聚合结果
    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("网站大类" in m and "动物" in m for m in logs)
