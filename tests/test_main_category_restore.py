# -*- coding: utf-8 -*-
"""网站信息「主类目」还原为表格原始分类值（含 ||| 层级分隔符）测试

背景：数据分配用 sanitize_filename(分类) 命名文件夹与主数据表（||| 与
Windows 非法字符统一转下划线——文件名不能含 |），网站信息的「主类目」
此前取清洗后的文件夹名，||| 层级分隔符丢失（"Toilets|||Toilet Tank Lids"
显示为 "Toilets Toilet Tank Lids"）。要求：审核表中的主类目与导出表格
「分类」列完全一致（含 |||）。
"""

import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest

from qmds.modules.web.services.category_stats import (
    STATS_FILE_NAME,
    aggregate_folder_categories,
    collect_stats_files,
    write_stats_excel,
)
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    _write_info_excel,
    build_site_info_prompt,
    read_site_info_excel,
    resolve_main_category,
)
from qmds.modules.web.services.site_review import repair_row_main_category
from qmds.modules.web.task_manager import task_manager

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"maincat_test_{uuid.uuid4().hex[:10]}"
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


def _cats(pairs):
    """构造分类统计形态的分类列表（产品数降序由调用方给定顺序）"""
    return [{"category": c, "count": n, "level1": c.split("|||")[0],
             "level2": "", "level3": ""} for c, n in pairs]


# ── resolve_main_category：文件夹/表名 -> 原始分类值 ─────────

CATS = _cats([("Toilets|||Toilet Tank Lids", 320),
              ("Hardware|||Plumbing & Fittings", 120),
              ("Toilet Tank Lid", 30)])


def test_resolve_by_folder_name():
    """文件夹名（sanitize 后的分类）-> 原始分类值，||| 完整保留"""
    assert (resolve_main_category("Toilets_Toilet_Tank_Lids", CATS)
            == "Toilets|||Toilet Tank Lids")
    # 单级分类（无 |||）同样精确还原
    assert resolve_main_category("Toilet_Tank_Lid", CATS) == "Toilet Tank Lid"
    assert (resolve_main_category("Hardware_Plumbing_&_Fittings", CATS)
            == "Hardware|||Plumbing & Fittings")


def test_resolve_collision_suffix():
    """数据分配防重名后缀 _{N}：去掉后缀仍可还原"""
    assert (resolve_main_category("Toilets_Toilet_Tank_Lids_2", CATS)
            == "Toilets|||Toilet Tank Lids")


def test_resolve_exact_folder_name_beats_base():
    """精确文件夹名优先于去后缀基名：避免 "Widgets" 抢占 "Widgets 2" 的文件夹"""
    cats = _cats([("Widgets", 900), ("Widgets 2", 50)])
    # 文件夹 Widgets_2 属于分类 "Widgets 2"（精确匹配），而非高数量的 "Widgets"
    assert resolve_main_category("Widgets_2", cats) == "Widgets 2"
    assert resolve_main_category("Widgets", cats) == "Widgets"


def test_resolve_by_main_table_name(workdir):
    """审核应用后文件夹已改名为域名：靠主数据表名（main 前缀）还原"""
    applied = workdir / "tanklidpro.com"
    applied.mkdir()
    (applied / "data_mainToilets_Toilet_Tank_Lids_part1_AB1234.xlsx").touch()
    assert (resolve_main_category(applied, CATS)
            == "Toilets|||Toilet Tank Lids")

    # 未拆分/未应用的主表名同样匹配
    plain = workdir / "faucetpro.com"
    plain.mkdir()
    (plain / "mainHardware_Plumbing_&_Fittings.xlsx").touch()
    assert (resolve_main_category(plain, CATS)
            == "Hardware|||Plumbing & Fittings")

    # 补充数据表（_supp，非 main 前缀）不参与匹配
    supp_only = workdir / "supponly.com"
    supp_only.mkdir()
    (supp_only / "data_Toilets_Toilet_Tank_Lids_supp_part1_X.xlsx").touch()
    assert resolve_main_category(supp_only, CATS) == ""


def test_resolve_no_match_returns_empty():
    assert resolve_main_category("Completely_Unrelated", CATS) == ""
    assert resolve_main_category("", CATS) == ""
    assert resolve_main_category("Toilets_Toilet_Tank_Lids", []) == ""
    assert resolve_main_category(None, CATS) == ""


# ── 提示词与批量生成 ─────────────────────────────

def test_prompt_uses_original_category():
    """提示词的 STORE SPECIALTY 使用原始分类值（含 |||）；显式传入优先"""
    stats = {"categories": CATS, "summary": {"产品总数": 470}}
    prompt = build_site_info_prompt(stats, "Toilets_Toilet_Tank_Lids")
    assert "STORE SPECIALTY (main category): Toilets|||Toilet Tank Lids" in prompt

    # 显式传入的 main_category 优先（生成流程已还原后的值直传）
    prompt2 = build_site_info_prompt(stats, "whatever_folder",
                                     main_category="Faucets|||Bathroom Sinks")
    assert "STORE SPECIALTY (main category): Faucets|||Bathroom Sinks" in prompt2

    # 无 ||| 的分类不受影响（回退清洗后的文件夹名）
    stats3 = {"categories": _cats([("Toilet Tank Lid", 320)]),
              "summary": {"产品总数": 320}}
    prompt3 = build_site_info_prompt(stats3, "Toilet_Tank_Lid")
    assert "STORE SPECIALTY (main category): Toilet Tank Lid" in prompt3


def test_batch_generation_main_category(workdir, monkeypatch):
    """批量 AI 生成：网站信息.xlsx 的主类目列 == 表格原始分类（含 |||）"""
    from qmds.modules.web.services import site_info_generator

    site = workdir / "Toilets_Toilet_Tank_Lids"
    site.mkdir()
    make_df([("Toilets|||Toilet Tank Lids", 5)]).to_excel(
        site / "mainToilets_Toilet_Tank_Lids.xlsx", index=False, engine="openpyxl")
    make_df([("Hardware|||Plumbing", 3)]).to_excel(
        site / "Toilets_Toilet_Tank_Lids_supp.xlsx", index=False, engine="openpyxl")

    def fake_llm(config, api_key, prompt, log_fn=None, **kwargs):
        assert "Toilets|||Toilet Tank Lids" in prompt  # 提示词同样使用原始值
        return {"domain": "tanklidpro.com", "theme": "Toilet Tank Lids",
                "title": "Toilet Tank Lids Pro",
                "description": "Replacement toilet tank lids.",
                "address": "1 Main St, Austin, TX 78701",
                "keywords": ["toilet tank lid"]}

    monkeypatch.setattr(site_info_generator, "_call_site_info_llm", fake_llm)

    task_id = "test_maincat_batch"
    task_manager.create(task_id, "site_info", "test")
    site_info_generator.run_batch_site_info_task(
        task_id, workdir, "agentrouter", "live-model-x",
        site_db=StubSiteDB({"agentrouter_api_key": "sk-ar-test"}))

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)

    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    assert len(rows) == 1
    # 主类目与导出表格「分类」列完全一致（含 |||），不再是清洗后的文件夹名
    assert rows[0]["主类目"] == "Toilets|||Toilet Tank Lids"
    assert rows[0]["主类目"] != "Toilets Toilet Tank Lids"


# ── 历史网站信息表的修复（审核表加载时） ───────────────

def _make_stale_site(root: Path):
    """构造旧版生成的网站场景：||| 丢失的统计正常网站文件夹 + 过期信息行"""
    site = root / "Toilets_Toilet_Tank_Lids"
    site.mkdir()
    make_df([("Toilets|||Toilet Tank Lids", 5)]).to_excel(
        site / "mainToilets_Toilet_Tank_Lids.xlsx", index=False, engine="openpyxl")
    make_df([("Hardware|||Plumbing", 3)]).to_excel(
        site / "Toilets_Toilet_Tank_Lids_supp.xlsx", index=False, engine="openpyxl")
    agg = aggregate_folder_categories(collect_stats_files(site))
    write_stats_excel(site / STATS_FILE_NAME, agg, folder_label=site.name)
    return site


def test_repair_row_main_category(workdir):
    """修复函数：过期主类目 -> 原始分类值；幂等；无统计表不动"""
    site = _make_stale_site(workdir)

    row = {"网站（文件夹）": "Toilets_Toilet_Tank_Lids",
           "主类目": "Toilets Toilet Tank Lids"}  # 旧版（||| 丢失）
    assert repair_row_main_category(site, row) is True
    assert row["主类目"] == "Toilets|||Toilet Tank Lids"
    # 已一致时幂等（不再修改）
    assert repair_row_main_category(site, row) is False

    # 无统计表的文件夹：保持原值
    other = workdir / "No_Stats_Here"
    other.mkdir()
    row2 = {"主类目": "keep me"}
    assert repair_row_main_category(other, row2) is False
    assert row2["主类目"] == "keep me"


def test_route_repairs_stale_main_category(workdir, monkeypatch):
    """GET 审核表路由：过期主类目即时修复（响应 + 回写 网站信息.xlsx）"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    monkeypatch.setattr(product_data, "fetch_agentrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("页面渲染不应连接平台")))
    _make_stale_site(workdir)
    # 旧版生成的网站信息（主类目丢失 |||）
    _write_info_excel(workdir / INFO_FILE_NAME, [{
        "网站（文件夹）": "Toilets_Toilet_Tank_Lids",
        "主类目": "Toilets Toilet Tank Lids",
        "域名": "tanklidpro.com", "标题": "t", "描述": "d", "主题": "th",
        "地址": "a", "关键词": "k", "产品数": 8, "分类数": 2, "模型": "m",
        "生成时间": "2026-09-10 10:00:00", "备注": "",
    }])

    export_dir = Path("data/exports") / "maincat_route_test"
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    for p in workdir.iterdir():
        if p.is_dir():
            shutil.copytree(p, export_dir / p.name)
        else:
            shutil.copy2(p, export_dir)

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/product-data/site-info/table",
                      query_string={"folder": "maincat_route_test"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # 响应中的主类目已还原为原始分类值（含 |||）
    assert data["rows"][0]["主类目"] == "Toilets|||Toilet Tank Lids"
    assert data["rows"][0]["applied"] is False

    # 修复结果已回写表格（下次加载/审核应用保持一致）
    rows = read_site_info_excel(export_dir / INFO_FILE_NAME)
    assert rows[0]["主类目"] == "Toilets|||Toilet Tank Lids"

    shutil.rmtree(export_dir, ignore_errors=True)
