# -*- coding: utf-8 -*-
"""网站信息审核应用（site_review）测试

覆盖：数据表收集/分类、域名标记写入（整列原站域名）、data_ 前缀重命名、
幂等重应用、无 _partN 表名的序号回退、缺列容错、批量任务与路由。
"""

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest

from qmds.modules.web.services.category_stats import STATS_FILE_NAME
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    _write_info_excel,
    read_site_info_excel,
)
from qmds.modules.web.services.site_review import (
    DATA_PREFIX,
    ORIGIN_COLUMN,
    _part_number,
    _table_kind,
    apply_domain_to_site,
    apply_site_review_task,
    collect_data_tables,
    is_site_applied,
    locate_site_folder,
)
from qmds.modules.web.task_manager import task_manager

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"site_review_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_df(categories, origin="old-site.com", n=8):
    """生成测试数据表 DataFrame（每分类 n 行，前几行用于标记验证）"""
    rows = []
    for i, cat in enumerate(categories):
        for j in range(n):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": cat, "Images": "", "cf_opingts": "",
                         "自定义分类": "", "原站域名": origin,
                         "分布网站识别": 0, "语言": "en"})
    return pd.DataFrame(rows, columns=EXPORT_COLUMNS)


def write_table(folder, name, categories=("Cat",), origin="old-site.com", n=8):
    """在 folder 下写一个数据表并返回路径"""
    path = Path(folder) / name
    make_df(list(categories), origin=origin, n=n).to_excel(
        path, index=False, engine="openpyxl")
    return path


# ── 表名识别 / 分卷号 ─────────────────────────────

def test_table_kind_and_part_number():
    assert _table_kind("mainToilet_Tank_Lid_part1_AK123.xlsx") == "main"
    assert _table_kind("Toilet_Tank_Lid_supp_part10_CW456.xlsx") == "supp"
    # 无分卷标记时回退排序序号
    assert _part_number("mainCat.xlsx", 1) == 1
    assert _part_number("mainCat.xlsx", 3) == 3
    assert _part_number("Cat_supp_part10_X.xlsx", 1) == 10


def test_collect_data_tables(workdir):
    """收集数据表：主/补充分组、自然排序、跳过结果文件"""
    site = workdir / "Site_A"
    site.mkdir()
    write_table(site, "mainCat_part1_X1.xlsx")
    write_table(site, "Cat_supp_part10_X2.xlsx")
    write_table(site, "Cat_supp_part2_X3.xlsx")
    write_table(site, "already.xlsx")  # 无 part 标记的补充表
    # 结果文件与临时文件不参与
    (site / STATS_FILE_NAME).write_bytes(b"x")
    (site / INFO_FILE_NAME).write_bytes(b"x")
    (site / "~$mainCat.xlsx").write_bytes(b"x")
    (site / "note.txt").write_bytes(b"x")
    # 已加 data_ 前缀的表按原始名参与排序
    write_table(site, "data_mainCat_part2_X4.xlsx")

    tables = collect_data_tables(site)
    assert [p.name for p in tables["main"]] == [
        "mainCat_part1_X1.xlsx", "data_mainCat_part2_X4.xlsx"]
    # 自然排序：part2 < part10
    assert [p.name for p in tables["supp"]] == [
        "already.xlsx", "Cat_supp_part2_X3.xlsx", "Cat_supp_part10_X2.xlsx"]


# ── 单网站应用 ────────────────────────────────────

def test_apply_domain_to_site(workdir):
    """整列原站域名标记 + data_ 前缀重命名（ERP 按整列一致标记识别）"""
    site = workdir / "Toilet_Tank_Lid"
    site.mkdir()
    main1 = write_table(site, "mainToilet_Tank_Lid_part1_AK1.xlsx")
    supp1 = write_table(site, "Toilet_Tank_Lid_supp_part1_TK1.xlsx")
    supp2 = write_table(site, "Toilet_Tank_Lid_supp_part2_OK1.xlsx")
    write_table(site, STATS_FILE_NAME, n=1)   # 结果文件（内容随意）
    write_table(site, INFO_FILE_NAME, n=1)

    logs = []
    st = apply_domain_to_site(site, "TankLidPro.com",
                              log_fn=lambda m, l="info": logs.append(m))

    # 标记统计：主 1 + 补 2，全部重命名
    assert st["main"] == 1 and st["supp"] == 2 and st["renamed"] == 3
    assert set(st["markers"]) == {"tanklidpro.com_main_part1",
                                  "tanklidpro.com_part1",
                                  "tanklidpro.com_part2"}
    # 网站文件夹已改名为域名
    new_site = workdir / "tanklidpro.com"
    assert st["folder"] == new_site
    assert new_site.is_dir() and not site.exists()

    # 主数据表：整列 -> 域名_main_part1
    df = pd.read_excel(new_site / (DATA_PREFIX + main1.name), engine="openpyxl")
    assert list(df[ORIGIN_COLUMN]) == ["tanklidpro.com_main_part1"] * len(df)
    assert "old-site.com" not in set(df[ORIGIN_COLUMN])
    # 补充表 1/2 -> 整列 域名_part1 / 域名_part2
    df1 = pd.read_excel(new_site / (DATA_PREFIX + supp1.name), engine="openpyxl")
    assert list(df1[ORIGIN_COLUMN]) == ["tanklidpro.com_part1"] * len(df1)
    df2 = pd.read_excel(new_site / (DATA_PREFIX + supp2.name), engine="openpyxl")
    assert list(df2[ORIGIN_COLUMN]) == ["tanklidpro.com_part2"] * len(df2)
    # 结果文件不重命名（跟随文件夹一起改名）
    assert (new_site / STATS_FILE_NAME).is_file()
    assert (new_site / INFO_FILE_NAME).is_file()
    assert not (new_site / (DATA_PREFIX + STATS_FILE_NAME)).exists()
    # 已应用状态
    assert is_site_applied(new_site)
    # 日志包含标记说明与文件夹改名
    assert any("整列" in m for m in logs)
    assert any("改名为域名" in m for m in logs)


def test_apply_domain_to_site_fallback_numbering(workdir):
    """表名无 _partN 时按排序序号编号（mainCat -> main_part1 等）"""
    site = workdir / "Site_B"
    site.mkdir()
    write_table(site, "mainCat.xlsx")
    write_table(site, "alpha_supp.xlsx")
    write_table(site, "beta_supp.xlsx")

    apply_domain_to_site(site, "abc.com")
    new_site = workdir / "abc.com"
    df_main = pd.read_excel(new_site / (DATA_PREFIX + "mainCat.xlsx"),
                            engine="openpyxl")
    df_s1 = pd.read_excel(new_site / (DATA_PREFIX + "alpha_supp.xlsx"),
                          engine="openpyxl")
    df_s2 = pd.read_excel(new_site / (DATA_PREFIX + "beta_supp.xlsx"),
                          engine="openpyxl")
    assert list(df_main[ORIGIN_COLUMN]) == ["abc.com_main_part1"] * len(df_main)
    assert list(df_s1[ORIGIN_COLUMN]) == ["abc.com_part1"] * len(df_s1)
    assert list(df_s2[ORIGIN_COLUMN]) == ["abc.com_part2"] * len(df_s2)


def test_apply_domain_to_site_idempotent(workdir):
    """重复应用（改域名）：标记更新、前缀不叠加、文件夹改到新域名"""
    site = workdir / "Site_C"
    site.mkdir()
    write_table(site, "mainCat_part1_X.xlsx")

    apply_domain_to_site(site, "first.com")
    first_dir = workdir / "first.com"
    renamed = first_dir / (DATA_PREFIX + "mainCat_part1_X.xlsx")
    assert renamed.is_file()

    # 换域名重新应用（文件夹已改名为 first.com）：不加第二次前缀，
    # 标记更新为新域名，文件夹再改到新域名
    apply_domain_to_site(first_dir, "second.com")
    second_dir = workdir / "second.com"
    assert second_dir.is_dir() and not first_dir.exists()
    assert not (second_dir / (DATA_PREFIX + DATA_PREFIX + "mainCat_part1_X.xlsx")).exists()
    df = pd.read_excel(second_dir / (DATA_PREFIX + "mainCat_part1_X.xlsx"),
                       engine="openpyxl")
    assert list(df[ORIGIN_COLUMN]) == ["second.com_main_part1"] * len(df)

    # 域名与文件夹名相同：改名跳过（幂等）
    apply_domain_to_site(second_dir, "second.com")
    assert second_dir.is_dir()


def test_apply_domain_to_site_missing_column(workdir):
    """缺「原站域名」列：警告并跳过标记，仍重命名"""
    site = workdir / "Site_D"
    site.mkdir()
    path = site / "mainCat_part1_X.xlsx"
    pd.DataFrame({"SKU": [f"S{i}" for i in range(8)]}).to_excel(
        path, index=False, engine="openpyxl")

    logs = []
    apply_domain_to_site(site, "abc.com", log_fn=lambda m, l="info": logs.append(m))
    new_site = workdir / "abc.com"
    assert (new_site / (DATA_PREFIX + "mainCat_part1_X.xlsx")).is_file()
    assert any("缺少" in m and ORIGIN_COLUMN in m for m in logs)


def test_apply_domain_to_site_errors(workdir):
    """域名为空 / 无数据表 -> 报错"""
    site = workdir / "Site_E"
    site.mkdir()
    with pytest.raises(ValueError, match="域名为空"):
        apply_domain_to_site(site, "  ")
    with pytest.raises(ValueError, match="没有数据表"):
        apply_domain_to_site(site, "abc.com")


def test_apply_domain_to_site_short_table(workdir):
    """不足五行的表：全部行标记"""
    site = workdir / "Site_F"
    site.mkdir()
    write_table(site, "mainCat_part1_X.xlsx", n=3)
    apply_domain_to_site(site, "abc.com")
    df = pd.read_excel(workdir / "abc.com" / (DATA_PREFIX + "mainCat_part1_X.xlsx"),
                       engine="openpyxl")
    assert len(df) == 3
    assert list(df[ORIGIN_COLUMN]) == ["abc.com_main_part1"] * 3


def test_locate_site_folder(workdir):
    """定位网站文件夹：直接子文件夹 / root 自身 / 深层嵌套（上级目录）"""
    (workdir / "Site_A").mkdir()
    assert locate_site_folder(workdir, "Site_A") == workdir / "Site_A"
    assert locate_site_folder(workdir, "Missing") is None
    assert locate_site_folder(workdir / "Site_A", "Site_A") == workdir / "Site_A"
    assert locate_site_folder(workdir, "") is None
    # 含路径分隔符的名称拒绝（防止越出 root）
    assert locate_site_folder(workdir, "../Site_A") is None
    assert locate_site_folder(workdir, "sub/Site_A") is None

    # 深层嵌套：所选文件夹是网站文件夹的上级（如分配文件夹的父目录）
    nested = workdir / "alloc_out" / "Site_B"
    nested.mkdir(parents=True)
    assert locate_site_folder(workdir, "Site_B") == nested
    # 同名时取路径最浅的一个
    deeper = workdir / "x" / "y" / "Site_B"
    deeper.mkdir(parents=True)
    assert locate_site_folder(workdir, "Site_B") == nested
    # 不存在的目录
    assert locate_site_folder(workdir / "not_exist", "Site_A") is None


# ── 批量任务 ──────────────────────────────────────

def _make_review_root(workdir):
    """构造审核场景：两个网站文件夹 + 网站信息.xlsx"""
    for name, main_cat in (("Site_A", "Faucets"), ("Site_B", "Door Hardware")):
        site = workdir / name
        site.mkdir()
        write_table(site, f"main{main_cat}_part1_X.xlsx")
        write_table(site, f"{main_cat}_supp_part1_Y.xlsx")
        write_table(site, f"{main_cat}_supp_part2_Z.xlsx")
    rows = [
        {"网站（文件夹）": "Site_A", "主类目": "Faucets", "域名": "faucetpro.com",
         "标题": "Faucet Pro", "描述": "d", "主题": "t", "地址": "a",
         "关键词": "k", "产品数": 16, "分类数": 2, "模型": "m",
         "生成时间": "2026-09-10 10:00:00", "备注": ""},
        {"网站（文件夹）": "Site_B", "主类目": "Door Hardware",
         "域名": "door-store.com", "标题": "Door Store", "描述": "d",
         "主题": "t", "地址": "a", "关键词": "k", "产品数": 16, "分类数": 2,
         "模型": "m", "生成时间": "2026-09-10 10:00:00", "备注": ""},
    ]
    _write_info_excel(workdir / INFO_FILE_NAME, rows)
    return rows


def test_apply_site_review_task(workdir):
    """批量应用：数据表标记 + 重命名 + 审核结果回写 网站信息.xlsx"""
    _make_review_root(workdir)

    task_id = "test_review_apply"
    task_manager.create(task_id, "site_review", "test")
    apply_site_review_task(task_id, workdir, [
        {"folder": "Site_A", "domain": "faucet-pro-fixed.com",
         "title": "Faucet Pro Fixed", "description": "", "theme": "",
         "address": "", "keywords": ""},
        {"folder": "Site_B", "domain": "doorstore.com",
         "title": "", "description": "", "theme": "",
         "address": "", "keywords": ""},
    ])

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "已应用 2/2" in task["message"]
    assert "6 个数据表" in task["message"]

    # 网站文件夹已改名为域名
    assert (workdir / "faucet-pro-fixed.com").is_dir()
    assert (workdir / "doorstore.com").is_dir()
    assert not (workdir / "Site_A").exists()
    assert not (workdir / "Site_B").exists()

    # Site_A 主表整列 -> 修改后的域名标记
    df = pd.read_excel(workdir / "faucet-pro-fixed.com"
                       / (DATA_PREFIX + "mainFaucets_part1_X.xlsx"),
                       engine="openpyxl")
    assert list(df[ORIGIN_COLUMN]) == ["faucet-pro-fixed.com_main_part1"] * len(df)
    # Site_B 补充表 part2 整列
    df = pd.read_excel(workdir / "doorstore.com"
                       / (DATA_PREFIX + "Door Hardware_supp_part2_Z.xlsx"),
                       engine="openpyxl")
    assert list(df[ORIGIN_COLUMN]) == ["doorstore.com_part2"] * len(df)

    # 网站信息.xlsx 回写：域名/标题更新 + 审核通过备注 + 文件夹名同步为域名；
    # 未审核字段保留
    rows = read_site_info_excel(workdir / INFO_FILE_NAME)
    by_site = {r["网站（文件夹）"]: r for r in rows}
    assert by_site["faucet-pro-fixed.com"]["域名"] == "faucet-pro-fixed.com"
    assert by_site["faucet-pro-fixed.com"]["标题"] == "Faucet Pro Fixed"
    assert by_site["faucet-pro-fixed.com"]["描述"] == "d"  # 空值不覆盖
    assert "审核通过" in by_site["faucet-pro-fixed.com"]["备注"]
    assert by_site["doorstore.com"]["域名"] == "doorstore.com"
    assert "审核通过" in by_site["doorstore.com"]["备注"]


def test_apply_site_review_task_partial_failure(workdir):
    """网站文件夹缺失：记录失败继续下一个"""
    _make_review_root(workdir)
    task_id = "test_review_partial"
    task_manager.create(task_id, "site_review", "test")
    apply_site_review_task(task_id, workdir, [
        {"folder": "Not_Exist", "domain": "x.com", "title": "", "description": "",
         "theme": "", "address": "", "keywords": ""},
        {"folder": "Site_A", "domain": "faucetpro.com", "title": "",
         "description": "", "theme": "", "address": "", "keywords": ""},
    ])

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "已应用 1/2" in task["message"]
    assert "Not_Exist" in task["message"]
    assert (workdir / "faucetpro.com"
            / (DATA_PREFIX + "mainFaucets_part1_X.xlsx")).is_file()


# ── 路由 ──────────────────────────────────────────

def test_site_info_table_route(workdir, monkeypatch):
    """GET 表格路由：返回行数据 + applied 状态"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    monkeypatch.setattr(product_data, "fetch_agentrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("页面渲染不应连接平台")))
    _make_review_root(workdir)
    # Site_A 先应用审核（直接调服务：文件夹改名但 xlsx 行仍是旧名 Site_A，
    # 路由需靠域名回退定位）-> applied 标记
    apply_domain_to_site(workdir / "Site_A", "faucetpro.com")

    export_dir = Path("data/exports") / "site_review_route_test"
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    # 复制整个审核场景到导出目录
    for p in workdir.iterdir():
        if p.is_dir():
            shutil.copytree(p, export_dir / p.name)
        else:
            shutil.copy2(p, export_dir)

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/product-data/site-info/table",
                      query_string={"folder": "site_review_route_test"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    by_site = {r["网站（文件夹）"]: r for r in data["rows"]}
    assert by_site["Site_A"]["applied"] is True
    assert by_site["Site_B"]["applied"] is False
    assert by_site["Site_A"]["域名"] == "faucetpro.com"

    # 无表格的文件夹 -> 明确错误
    resp2 = client.get("/product-data/site-info/table",
                       query_string={"folder": "not_exist_folder"})
    assert resp2.get_json()["ok"] is False

    shutil.rmtree(export_dir, ignore_errors=True)


def test_site_info_apply_route(workdir, monkeypatch):
    """POST 应用路由：解析 JSON 站点列表并启动任务线程"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    captured = {}

    def fake_task(task_id, folder, sites):
        captured["folder"] = folder
        captured["sites"] = sites

    monkeypatch.setattr(product_data, "apply_site_review_task", fake_task)
    monkeypatch.setattr(product_data, "fetch_agentrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("页面渲染不应连接平台")))

    _make_review_root(workdir)
    export_dir = Path("data/exports") / "site_review_apply_route"
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

    sites = [{"folder": "Site_A", "domain": "faucetpro.com", "title": "T",
              "description": "", "theme": "", "address": "", "keywords": ""}]
    resp = client.post("/product-data/site-info/apply", data={
        "folder": "site_review_apply_route",
        "sites": json.dumps(sites),
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "site_review_apply_route" in str(captured["folder"])
    assert captured["sites"][0]["folder"] == "Site_A"
    assert captured["sites"][0]["domain"] == "faucetpro.com"

    # 域名缺失 -> 拒绝启动
    resp2 = client.post("/product-data/site-info/apply", data={
        "folder": "site_review_apply_route",
        "sites": json.dumps([{"folder": "Site_B", "domain": ""}]),
    }, follow_redirects=True)
    assert "域名为空" in resp2.get_data(as_text=True)

    shutil.rmtree(export_dir, ignore_errors=True)
