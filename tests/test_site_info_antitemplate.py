# -*- coding: utf-8 -*-
"""网站信息生成反模板化测试（避免被识别为批量建站）

覆盖：
- 创意方向抽取：同 key 稳定（重试幂等）、不同 key 互不相同（批量差异化）；
- 提示词注入 CREATIVE DIRECTION（品牌声线/命名风格/城市）与反指纹规则
  （禁用套路化域名后缀/标题句式/描述开头/陈词滥调），且保留原有结构段落；
- LLM 调用温度随方向抖动（0.7-0.95），不再固定 0.4；
- 批量任务域名去重：冲突时换创意方向重试一次，仍冲突则明确失败。
"""

import re
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from qmds.modules.web.services import site_info_generator
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    _ADDRESS_CITIES,
    _DESC_CHAR_CAP,
    _DESC_LENGTHS,
    _QUALITY_RETRY_HINT,
    _cap_description_length,
    _find_person_pronouns,
    _find_quality_issues,
    _find_template_opening,
    _regenerate_on_quality_issues,
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


def test_creative_direction_city_pool_major_economies():
    """城市池是美国经济靠前的城市 + 各州经济中心（用户要求），且覆盖多州"""
    cities = {city for city, _state in _ADDRESS_CITIES}
    # 经济规模靠前的主要都市必须在池内（旧版刻意排除它们，与要求相反）
    for city in ("New York", "Los Angeles", "Chicago", "Houston", "Dallas",
                 "San Francisco", "Boston", "Seattle", "Atlanta", "Miami",
                 "Phoenix", "Philadelphia", "San Jose", "Minneapolis"):
        assert city in cities, city
    assert len(_ADDRESS_CITIES) >= 60, "城市池过小，批量生成容易重复"
    # 仍保持地理分散：避免整批站点地址挤在同几个州
    assert len({state for _city, state in _ADDRESS_CITIES}) >= 45


def test_street_is_residential_and_differs_per_site():
    """街道行由代码生成：住宅街道、无大马路、各站不重复"""
    banned = re.compile(
        r"\b(main|highway|hwy|blvd|boulevard|parkway|pkwy|road|rd|route|"
        r"commerce|industrial|corporate|business)\b", re.I)
    streets = []
    for i in range(60):
        d = _site_creative_direction(f"addr-test|{i}|v0")
        street = d["street"]
        streets.append(street)
        assert banned.search(street) is None, street          # 不含大马路/商业路型
        assert re.match(r"^\d{1,4} [A-Z]", street), street    # 门牌号 + 街道名
        assert ", Suite" not in street, street                # 住宅地址不用 Suite
    assert len(set(streets)) >= 50, "街道应随网站变化，避免整批雷同"


def test_prompt_requires_copying_the_street_line(monkeypatch):
    """地址库不可用时的回退文案：原样照抄代码生成的街道行"""
    monkeypatch.setattr(site_info_generator, "_address_pool_cache", {})
    prompt = build_site_info_prompt(STATS, "Faucets")
    assert "copy that street line EXACTLY as given" in prompt
    assert "private residential home address" in prompt
    assert "must not look like a business park, a warehouse or a highway address" in prompt


# ── 提示词：创意方向 + 反指纹规则 ─────────────────────

def test_prompt_contains_direction_and_rules():
    """提示词注入该站专属创意方向与反指纹规则，且保留原有结构段落"""
    direction = {
        "voice": "family-run shop, second generation, plain-spoken",
        "tone": "warm and conversational",
        "domain_style": "a two-word compound of two real English words",
        "title_style": "brand word first, then a plain descriptor after a colon",
        "title_len": "40-60", "desc_angle": "open with the concrete product range",
        "desc_opening": "start with the material or construction that defines the specialty",
        "desc_len": "100-150", "keyword_recipe": "10-14 keywords, head terms first",
        "city": "Fort Collins", "state": "CO",
        "street": "412 Oak Street",
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
    assert any("重复" in m and "重试" in m for m in logs)


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


def test_batch_only_generates_sites_with_empty_info(workdir, monkeypatch):
    """增量模式：已有网站信息的网站跳过不动，只生成信息为空的网站"""
    _make_site(workdir, "Faucets", "Bathroom|||Faucets")
    _make_site(workdir, "Door_Hardware", "Hardware|||Door Hardware")
    # 预置结果表：Faucets 已有信息（要保留），Door_Hardware 上次失败（要重生成）
    site_info_generator._write_info_excel(workdir / INFO_FILE_NAME, [
        {"网站（文件夹）": "Faucets", "域名": "keep-me.com", "标题": "Old title"},
        {"网站（文件夹）": "Door_Hardware", "备注": "生成失败: boom"},
    ])

    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        return {"domain": "new-door.com", "theme": "T", "title": "Title",
                "description": "Cork mats anchor the studio range.",
                "address": "1 Main St, Fort Collins, CO 80521", "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_only_empty"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert len(calls) == 1, "只应为信息为空的网站调用模型"

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert len(rows) == 2, "不应为跳过的网站新增行"
    by_site = {r["网站（文件夹）"]: r for r in rows}
    assert by_site["Faucets"]["域名"] == "keep-me.com"       # 已有信息未被覆盖
    assert by_site["Door_Hardware"]["域名"] == "new-door.com"  # 空的重生成了
    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("跳过（已有网站信息）" in m for m in logs)


def test_batch_retries_failed_site_then_succeeds(workdir, monkeypatch):
    """单个网站生成失败会自动重试（换创意方向），重试成功则照常写入"""
    _make_site(workdir, "Faucets", "Bathroom|||Faucets")

    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("api 502 bad gateway")
        return {"domain": "retry-ok.com", "theme": "T", "title": "Title",
                "description": "Brass faucets anchor the bath range.",
                "address": "1 Main St, Fort Collins, CO 80521", "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)
    monkeypatch.setattr(site_info_generator.time, "sleep", lambda _s: None)  # 免等待

    task_id = "test_retry_site"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert len(calls) == 2, "第一次失败后应自动重试一次"
    assert "批量生成 1/1" in task["message"]
    assert "失败" not in task["message"]

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert len(rows) == 1 and rows[0]["域名"] == "retry-ok.com"
    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("自动重试" in m for m in logs)


def test_batch_spreads_same_category_across_cities(workdir, monkeypatch):
    """同一类目的多个网站要分到不同城市"""
    for i in (1, 2, 3):
        _make_site(workdir, f"Faucets_{i}", "Bathroom|||Faucets")

    # 三个城市的真实地址库
    pool = {f"City{c}|CO": [[f"{100 + c} Oak St", f"City{c}", f"8020{c}"]] * 12
            for c in (1, 2, 3)}
    monkeypatch.setattr(site_info_generator, "_load_address_pool", lambda: pool)

    calls = []

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        calls.append(prompt)
        m = re.search(r'residential address: "([^"]+)"', prompt)
        return {"domain": f"site{len(calls)}.com", "theme": "T", "title": "Title",
                "description": "Brass faucets anchor the bath range.",
                "address": m.group(1) if m else "", "keywords": ["k"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_city_spread"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert len(rows) == 3
    cities = [site_info_generator._city_of(r.get("地址")) for r in rows]
    assert len(set(cities)) == 3, f"同类目网站应分散到不同城市，实际: {cities}"


def test_store_row_replaces_in_place():
    """同一网站重跑时原地替换该行，不会堆出多行"""
    rows = [{"网站（文件夹）": "A", "备注": "生成失败: x"}]
    index = {"A": 0}
    site_info_generator._store_row(rows, index, "A",
                                   {"网站（文件夹）": "A", "域名": "a.com"})
    assert len(rows) == 1 and rows[0]["域名"] == "a.com"
    site_info_generator._store_row(rows, index, "B", {"网站（文件夹）": "B"})
    assert len(rows) == 2 and index["B"] == 1


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

# ── 描述长度：按「字符」计（约 300 字符的 SEO meta 描述）──────

def test_desc_lengths_are_characters_within_cap():
    """长度档位按字符计，且上限不超过兜底阈值"""
    for spec in _DESC_LENGTHS:
        assert "characters" in spec, spec
        assert "words" not in spec, spec
        upper = int(spec.split()[0].split("-")[1])
        assert upper <= _DESC_CHAR_CAP, spec
        assert 250 <= upper <= 340, spec          # 贴着「约 300 字符」


def test_prompt_asks_for_characters_not_words():
    """提示词明确按字符、单段 2-3 句，并给出硬上限"""
    prompt = build_site_info_prompt(STATS, "Toilets_Toilet_Tank_Lids")
    assert "300 CHARACTERS (characters, NOT words)" in prompt
    assert "never exceed 330 characters" in prompt
    assert "ONE short paragraph of 2-3 sentences" in prompt
    # 不能再出现按词计的长文要求
    assert "about 300 words" not in prompt
    assert "flowing paragraphs" not in prompt


def test_cap_description_keeps_short_text_and_flattens_newlines():
    text = "Short meta description for the store."
    assert _cap_description_length(text) == text
    assert _cap_description_length("Line one.\n\nLine two.") == "Line one. Line two."
    assert _cap_description_length("") == ""
    assert _cap_description_length(None) is None


def test_cap_description_truncates_to_complete_sentence():
    """超长描述截断到完整句子，且不超过上限"""
    long_desc = ("First sentence about the specialty and its main product types. "
                 + "Second sentence padding " + "pad " * 80
                 + ". Third sentence that should never survive the cap.")
    assert len(long_desc) > _DESC_CHAR_CAP

    logs = []
    result = _cap_description_length(long_desc, log_fn=lambda m, lvl="info": logs.append(m),
                                     folder_name="Faucets")
    assert len(result) <= _DESC_CHAR_CAP
    assert result.endswith(".")
    assert "Third sentence" not in result
    assert result.startswith("First sentence about the specialty")
    assert any("描述超长" in m for m in logs)


def test_cap_description_handles_single_overlong_sentence():
    """极端情况：第一句就超长时按字符截断到词边界"""
    one_long = "word " * 200
    result = _cap_description_length(one_long)
    assert len(result) <= _DESC_CHAR_CAP + 1
    assert result.endswith(".")
    assert "wordword" not in result            # 没有切断单词

# ── 第三人称：提示词规则 + 生成后人称校验 ───────────────

def test_prompt_forbids_first_and_second_person():
    """提示词明确第三人称，并给出正反例、禁祈使句、禁残句"""
    prompt = build_site_info_prompt(STATS, "Toilets_Toilet_Tank_Lids")
    assert "WRITING VOICE - applies to EVERY field" in prompt
    assert "Write in the THIRD PERSON only" in prompt
    assert "Never address the reader" in prompt
    assert '"Order today", "Browse the range", "Shop now", "Get yours"' in prompt
    assert 'BAD:  "We carry yoga mats' in prompt          # 反例
    assert 'GOOD: "Cork, jute and natural rubber yoga mats' in prompt  # 正例
    assert "Every sentence must contain a verb and stand on its own" in prompt
    assert "Never write noun fragments" in prompt
    assert "conversational address to the reader" in prompt


def test_find_person_pronouns():
    assert _find_person_pronouns("We carry yoga mats and our prices are low.") == ["our", "we"]
    assert _find_person_pronouns("You'll find the perfect gift for your home.") == ["you", "your"]
    assert _find_person_pronouns("Order today and we'll ship fast.") == ["we"]
    assert _find_person_pronouns("Give us a call.") == ["us"]
    # 第三人称写法不应命中
    assert _find_person_pronouns("The catalog spans cork and rubber mats.") == []
    # US 是国家缩写，不是人称代词
    assert _find_person_pronouns("Orders ship from the US within two days.") == []
    assert _find_person_pronouns("") == []
    assert _find_person_pronouns(None) == []


def _info(**kw):
    base = {"domain": "example.com", "theme": "plumbing parts", "title": "Plumbing Supplies",
            "description": "Faucets and shut-off valves make up the plumbing core range.",
            "address": "1 Main St, Norman, OK 73069", "keywords": ["plumbing supplies"]}
    base.update(kw)
    return base


def test_quality_retry_skipped_when_clean(monkeypatch):
    """文案合格（无人称、无模板开头）时不额外调用 LLM"""
    calls = []
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm",
                        lambda *a, **k: calls.append(1) or _info())
    logs = []
    info = _info()
    out = _regenerate_on_quality_issues(
        info, {}, "k", "prompt", {"temperature": 0.8},
        lambda m, lvl="info": logs.append(m), "Site")
    assert out is info and calls == [] and logs == []


def test_quality_retry_replaces_bad_copy(monkeypatch):
    """命中原人称时重写一次，并采用重写后的第三人称结果"""
    good = _info(description="Rosary beads and brass censers fill the devotional range.")
    def fake_llm(config, api_key, prompt, log_fn=None, **kw):
        assert _QUALITY_RETRY_HINT in prompt           # 带上针对性提示
        return good
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)
    logs = []
    bad = _info(description="We carry rosary beads and you'll love our brass censers.")
    out = _regenerate_on_quality_issues(
        bad, {}, "k", "prompt", {"temperature": 0.8},
        lambda m, lvl="info": logs.append(m), "Site")
    assert out is good
    assert any("未通过校验" in m for m in logs)
    assert any("文案已修正" in m for m in logs)


def test_template_opening_triggers_retry(monkeypatch):
    """描述以「The catalog ...」等商店词开头时判为模板化并重写"""
    good = _info(description="Cork and rubber yoga mats anchor the studio range.")
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm",
                        lambda *a, **k: good)
    logs = []
    bad = _info(description="The catalog supplies yoga mats, blocks and straps.")
    out = _regenerate_on_quality_issues(
        bad, {}, "k", "prompt", {"temperature": 0.8},
        lambda m, lvl="info": logs.append(m), "Site")
    assert out is good
    assert any("模板化开头" in m for m in logs)


def test_find_template_opening():
    """模板化开头的识别规则"""
    assert _find_template_opening("The catalog supplies yoga mats.") == "The catalog"
    assert _find_template_opening("This store stocks faucets.") == "This store"
    assert _find_template_opening("The plumbing catalog spans valves.") == "The plumbing catalog"
    assert _find_template_opening("Our selection covers brass fittings.") == "Our selection"
    # 以商品/材质/用途开头都合格
    assert _find_template_opening("Cork yoga mats anchor the range.") == ""
    assert _find_template_opening("Brass fittings and PEX tubing cover repairs.") == ""
    assert _find_template_opening("") == ""


def test_find_quality_issues_reports_both_kinds():
    issues = _find_quality_issues(_info(description="We supply mats."))
    assert len(issues) == 1 and "第一/第二人称" in issues[0]
    issues = _find_quality_issues(_info(description="The catalog supplies mats."))
    assert len(issues) == 1 and "模板化开头" in issues[0]
    assert _find_quality_issues(_info()) == []


def test_quality_retry_keeps_original_when_still_bad(monkeypatch):
    """重试后仍含人称时沿用原结果（不无限重试）"""
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm",
                        lambda *a, **k: _info(description="We still write like this."))
    logs = []
    bad = _info(description="We carry rosary beads.")
    out = _regenerate_on_quality_issues(
        bad, {}, "k", "prompt", {"temperature": 0.8},
        lambda m, lvl="info": logs.append(m), "Site")
    assert out is bad
    assert any("重试后仍未通过" in m for m in logs)


def test_quality_retry_survives_llm_failure(monkeypatch):
    """重试调用抛异常时沿用原结果，不中断整个生成流程"""
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", boom)
    logs = []
    bad = _info(description="We carry rosary beads.")
    out = _regenerate_on_quality_issues(
        bad, {}, "k", "prompt", {"temperature": 0.8},
        lambda m, lvl="info": logs.append(m), "Site")
    assert out is bad
    assert any("质量重试失败" in m for m in logs)
