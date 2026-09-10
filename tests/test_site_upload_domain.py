# -*- coding: utf-8 -*-
"""按网站（域名文件夹）上传站群系统 — 单元与路由测试"""

import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services import site_uploader
from qmds.modules.web.services.site_uploader import (
    collect_domain_sites, collect_site_tables, run_domain_upload_task,
    save_upload_ids)

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"upload_domain_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_data_table(path, rows=3, domain="example.com"):
    """生成带 data_ 前缀命名的网站数据表"""
    df = pd.DataFrame([{
        "SKU": f"S{i}", "Name": f"P{i}", "Description": "d",
        "Regular price": 9.9, "Categories": "Cat", "Images": "",
        "cf_opingts": "", "自定义分类": "", "原站域名": domain,
        "分布网站识别": 0, "语言": "en",
    } for i in range(rows)], columns=EXPORT_COLUMNS)
    df.to_excel(path, index=False, engine="openpyxl")
    return path


def make_site(root, domain, mains=1, supps=2, cat="Cat"):
    """在 root 下生成一个域名网站文件夹（data_ 数据表，cat 用于区分文件名）"""
    site = root / domain
    site.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(1, mains + 1):
        paths.append(make_data_table(
            site / f"data_main{cat}_part{i}_AB{i}.xlsx", domain=domain))
    for i in range(1, supps + 1):
        paths.append(make_data_table(
            site / f"data_{cat}_supp_part{i}_CD{i}.xlsx", domain=domain))
    return site, paths


# ── collect_domain_sites / collect_site_tables ─────────────

def test_collect_domain_sites_nested(workdir):
    """递归发现域名文件夹：嵌套层级、非域名排除、域名内不向下、无表也列出"""
    # alloc/date 双层嵌套下的两个网站
    alloc = workdir / "20260907" / "hardware" / "alloc_out"
    s1, _ = make_site(alloc, "tanklidpro.com", mains=1, supps=2)
    s2, _ = make_site(alloc, "amstdtoilets.com", mains=1, supps=1)
    # 非域名文件夹（分类名带下划线/大写）不识别
    (workdir / "Toilet_Tank").mkdir()
    make_data_table(workdir / "Toilet_Tank" / "data_mainX.xlsx")
    # 域名文件夹内的子文件夹不再向下查找（子目录名恰好是域名也不算）
    inner = s1 / "shop.tanklidpro.com"
    inner.mkdir()
    make_data_table(inner / "data_inner.xlsx")
    # 空 data_ 表的域名文件夹也列出（tables=0）
    (workdir / "empty.com").mkdir()
    # 非域名文件夹里的域名文件夹（未审核的网站同名目录）也应能找到
    s3, _ = make_site(workdir / "Toilet_Tank", "toilettank.example.org",
                      mains=0, supps=1)

    sites = collect_domain_sites(workdir)
    by_name = {s["name"]: s for s in sites}
    assert set(by_name) == {"tanklidpro.com", "amstdtoilets.com",
                            "empty.com", "toilettank.example.org"}
    assert by_name["tanklidpro.com"]["tables"] == 3
    assert by_name["tanklidpro.com"]["path"] == \
        "20260907/hardware/alloc_out/tanklidpro.com"
    assert by_name["amstdtoilets.com"]["tables"] == 2
    assert by_name["empty.com"]["tables"] == 0
    # 域名内嵌套目录的表不计入
    assert by_name["toilettank.example.org"]["tables"] == 1


def test_collect_site_tables(workdir):
    """只收集 data_ 前缀 .xlsx，自然排序（part2 < part10），跳过结果文件"""
    site = workdir / "a.com"
    site.mkdir()
    p10 = make_data_table(site / "data_x_supp_part10_ZZ.xlsx")
    p2 = make_data_table(site / "data_x_supp_part2_ZZ.xlsx")
    p1 = make_data_table(site / "data_mainX_part1_ZZ.xlsx")
    # 非 data_ 文件不上传
    make_data_table(site / "mainX_part1.xlsx")
    make_data_table(site / "分类统计.xlsx")
    (site / "数据ID.txt").write_text("1", encoding="utf-8")

    tables = collect_site_tables(site)
    assert tables == [p1, p2, p10]
    assert collect_site_tables(workdir / "not_exist") == []


# ── save_upload_ids: data_ 前缀主数据识别 ─────────────

def test_save_upload_ids_data_prefix(workdir):
    """data_ 前缀表名剥离后再判断主数据：主数据 ID 在最前"""
    site = workdir / "a.com"
    site.mkdir()
    files = [
        make_data_table(site / "data_Cat_supp_part1_A.xlsx"),
        make_data_table(site / "data_Cat_supp_part2_B.xlsx"),
        make_data_table(site / "data_mainCat_part1_C.xlsx"),
    ]
    uploaded = [(files[0], "201"), (files[1], "202"), (files[2], "203")]
    written = save_upload_ids(site, uploaded)
    assert written == [site / "数据ID.txt"]
    ids = (site / "数据ID.txt").read_text(encoding="utf-8").splitlines()
    assert ids == ["203", "201", "202"]  # 主数据最前，其余按上传顺序


# ── run_domain_upload_task ─────────────

class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def _mock_session(upload_ok_names=None):
    """伪造 requests.Session：登录成功，上传按文件名返回递增 ID"""
    upload_ok_names = upload_ok_names or []
    state = {"i": 0, "uploaded_files": []}
    session = mock.MagicMock()

    def fake_post(url, **kw):
        if url == "https://x.com/login":
            return FakeResponse("<html>ok</html>")
        fname = kw["files"]["file"][0]
        if fname in upload_ok_names:
            state["i"] += 1
            state["uploaded_files"].append(fname)
            return FakeResponse('{"code":"0","msg":"500","yz":"%d","cat":"c"}'
                                % (300 + state["i"]))
        return FakeResponse('{"code":"1","msg":"boom"}')

    def fake_get(url, **kw):
        return FakeResponse('{"msg":"完成","yz":"0"}')

    session.post.side_effect = fake_post
    session.get.side_effect = fake_get
    return session, state


CFG = {"login_url": "https://x.com/login",
       "upload_page_url": "https://x.com/up?dongzuo=add_cp_pl",
       "username": "u", "password": "p"}


def test_run_domain_upload_task(workdir):
    """逐站顺序上传 data_ 表：分组输出数据ID + 各站 数据ID.txt + 汇总"""
    s1, s1_files = make_site(workdir, "a-site.com", mains=1, supps=2,
                              cat="CatA")
    s2, s2_files = make_site(workdir, "b-site.com", mains=1, supps=1,
                             cat="CatB")

    # b-site 的补充表上传失败（服务器返回错误）
    fail_name = "data_CatB_supp_part1_CD1.xlsx"
    session, state = _mock_session(
        upload_ok_names=[p.name for p in s1_files]
        + [p.name for p in s2_files if p.name != fail_name])

    from qmds.modules.web.task_manager import task_manager
    task_id = "test_domain_upload"
    task_manager.create(task_id, "site_upload_huisheng", "test")

    with mock.patch("requests.Session", return_value=session):
        run_domain_upload_task(task_id, workdir, ["a-site.com", "b-site.com"], CFG)

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    # 2 站全部有成功表；表格 4/5（b 站补充表失败）
    assert "上传 2/2 个网站" in task["message"]
    assert "4/5 个表格" in task["message"]

    logs = [e["message"] for e in task_manager.get_logs(task_id)]
    # 逐站顺序：a 站的表全部完成后才开始 b 站
    a_done = next(i for i, m in enumerate(logs)
                  if m.startswith("[1/2]") and "a-site.com 完成" in m)
    b_start = next(i for i, m in enumerate(logs) if "▶ 网站 b-site.com" in m)
    assert a_done < b_start
    # 分组汇总输出：按网站列出全部数据ID
    summary_idx = next(i for i, m in enumerate(logs) if "数据ID汇总" in m)
    a_line = next(m for m in logs[summary_idx:] if m.startswith("a-site.com"))
    b_line = next(m for m in logs[summary_idx:] if m.startswith("b-site.com"))
    assert "3 个" in a_line and a_line.count("3") >= 1  # 3 个 ID
    assert len(a_line.split(": ", 1)[1].split(", ")) == 3
    assert len(b_line.split(": ", 1)[1].split(", ")) == 1
    # 失败表有错误日志
    assert any("✗" in m and fail_name in m for m in logs)

    # 各站 数据ID.txt：主数据 ID 在最前
    a_ids = (s1 / "数据ID.txt").read_text(encoding="utf-8").splitlines()
    assert len(a_ids) == 3 and a_ids[0].startswith("3")
    b_ids = (s2 / "数据ID.txt").read_text(encoding="utf-8").splitlines()
    assert len(b_ids) == 1


def test_run_domain_upload_task_skip_empty(workdir):
    """无 data_ 表的网站跳过并记录，不影响其他网站"""
    make_site(workdir, "a-site.com", mains=1, supps=0)
    (workdir / "empty.com").mkdir()

    session, _ = _mock_session(upload_ok_names=["data_mainCat_part1_AB1.xlsx"])
    from qmds.modules.web.task_manager import task_manager
    task_id = "test_domain_skip"
    task_manager.create(task_id, "site_upload_huisheng", "test")
    with mock.patch("requests.Session", return_value=session):
        run_domain_upload_task(task_id, workdir, ["empty.com", "a-site.com"], CFG)

    task = task_manager.get(task_id)
    assert task["status"] == "completed"
    assert "上传 1/2 个网站" in task["message"]
    logs = [e["message"] for e in task_manager.get_logs(task_id)]
    assert any("没有 data_ 数据表，跳过" in m and "empty.com" in m for m in logs)
    assert any("empty.com: 无上传成功的数据表" for m in logs)


def test_run_domain_upload_task_errors(workdir):
    """未知网站名 / 空选择 -> 任务失败"""
    make_site(workdir, "a-site.com", mains=1, supps=0)
    from qmds.modules.web.task_manager import task_manager

    task_id = "test_domain_err"
    task_manager.create(task_id, "site_upload_huisheng", "test")
    run_domain_upload_task(task_id, workdir, ["nope.com"], CFG)
    assert task_manager.get(task_id)["status"] == "failed"
    assert "未找到域名文件夹" in task_manager.get(task_id)["message"]

    task_id2 = "test_domain_err2"
    task_manager.create(task_id2, "site_upload_huisheng", "test")
    run_domain_upload_task(task_id2, workdir, [], CFG)
    assert task_manager.get(task_id2)["status"] == "failed"


# ── 路由 ─────────────

def _make_export_scene(name):
    """在 data/exports 下创建测试场景（域名网站文件夹），返回相对文件夹名"""
    export_dir = Path("data/exports") / name
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    make_site(export_dir, "a-site.com", mains=1, supps=1)
    make_site(export_dir, "b-site.com", mains=1, supps=0)
    return name


def test_site_upload_sites_route(monkeypatch):
    """GET 站点列表路由：返回域名文件夹与 data_ 表数"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    monkeypatch.setattr(product_data, "fetch_agentrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("页面渲染不应连接平台")))
    folder = _make_export_scene("site_upload_route_test")

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/product-data/site-upload/sites",
                      query_string={"folder": folder})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    by_name = {s["name"]: s for s in data["sites"]}
    assert set(by_name) == {"a-site.com", "b-site.com"}
    assert by_name["a-site.com"]["tables"] == 2
    assert by_name["b-site.com"]["tables"] == 1

    # 不存在的文件夹 -> 明确错误
    resp2 = client.get("/product-data/site-upload/sites",
                       query_string={"folder": "not_exist_folder"})
    assert resp2.get_json()["ok"] is False

    shutil.rmtree(Path("data/exports") / folder, ignore_errors=True)


def test_site_upload_domain_route(monkeypatch):
    """POST 按网站上传路由：解析 JSON 站点列表并启动任务线程"""
    from qmds.modules.web.engine import create_app
    from qmds.modules.web.routes import product_data

    captured = {}

    def fake_task(task_id, target, site_names, config=None):
        captured["target"] = target
        captured["sites"] = list(site_names)
        from qmds.modules.web.task_manager import task_manager
        task_manager.update(task_id, status="completed", message="fake done")

    monkeypatch.setattr(site_uploader, "run_domain_upload_task", fake_task)
    monkeypatch.setattr(product_data, "fetch_agentrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("页面渲染不应连接平台")))
    folder = _make_export_scene("site_upload_domain_route")

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post("/product-data/site-upload/domain", data={
        "upload_folder": folder,
        "sites": json.dumps(["a-site.com", "b-site.com"]),
    }, follow_redirects=True)
    assert resp.status_code == 200
    # 等待后台线程执行 fake 任务
    from qmds.modules.web.task_manager import task_manager
    for _ in range(100):
        if captured.get("sites"):
            break
        time.sleep(0.05)
    assert captured["sites"] == ["a-site.com", "b-site.com"]
    assert str(captured["target"]).replace("\\", "/").endswith(folder)

    # 未勾选网站 -> 拒绝启动
    resp2 = client.post("/product-data/site-upload/domain", data={
        "upload_folder": folder, "sites": json.dumps([]),
    }, follow_redirects=True)
    assert resp2.status_code == 200
    # 未知网站 -> 拒绝启动
    resp3 = client.post("/product-data/site-upload/domain", data={
        "upload_folder": folder, "sites": json.dumps(["nope.com"]),
    }, follow_redirects=True)
    assert resp3.status_code == 200

    shutil.rmtree(Path("data/exports") / folder, ignore_errors=True)
