# -*- coding: utf-8 -*-
"""数据分配预测接口（/product-data/allocate-preview）测试

预测接口按当前参数返回每个网站将拿到的数据量，用于提交前确认是否会产生
额外补充（extra）。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.engine import create_app
from qmds.modules.web.routes import product_data as product_data_routes
from qmds.modules.web.services.data_allocator import count_excel_categories_with_domains

EXPORT_COLUMNS = ["SKU", "Name", "Categories", "原站域名"]


@pytest.fixture
def workdir():
    d = Path(".tmp") / f"preview_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_xlsx(path, items, with_domain=True):
    """items: [(分类, 数量, 域名)]"""
    rows = []
    for i, (cat, count, dom) in enumerate(items):
        for j in range(count):
            row = {"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}", "Categories": cat}
            if with_domain:
                row["原站域名"] = dom
            rows.append(row)
    columns = EXPORT_COLUMNS if with_domain else ["SKU", "Name", "Categories"]
    pd.DataFrame(rows, columns=columns).to_excel(path, index=False, engine="openpyxl")


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def post_preview(client, monkeypatch, file_path, **form):
    monkeypatch.setattr(product_data_routes, "resolve_export_file",
                        lambda folder, name: Path(file_path))
    data = {"folder": "uploads", "file": Path(file_path).name}
    data.update(form)
    return client.post("/product-data/allocate-preview", data=data)


def test_count_excel_categories_with_domains(workdir):
    fp = workdir / "t.xlsx"
    make_xlsx(fp, [("Cat A", 5, "a.com"), ("Cat A", 3, "b.com"), ("Cat B", 2, "a.com")])
    col, total, cats, domains = count_excel_categories_with_domains(fp)
    assert col == "Categories"
    assert total == 10
    assert cats == [("Cat A", 8), ("Cat B", 2)]
    assert domains["Cat A"] == {"a.com": 5, "b.com": 3}
    assert domains["Cat B"] == {"a.com": 2}

    # 无「原站域名」列 -> None
    fp2 = workdir / "t2.xlsx"
    make_xlsx(fp2, [("Cat A", 5, "a.com")], with_domain=False)
    assert count_excel_categories_with_domains(fp2)[3] is None


def test_preview_distribute_no_extra(client, workdir, monkeypatch):
    """均分模式预测：每站数据量可见、无额外补充、上限装不下时给出抬高说明"""
    fp = workdir / "preview.xlsx"
    make_xlsx(fp, [("Main A", 100, "main-a.com"),
                   ("Main B", 100, "main-b.com"),
                   ("small", 300, "example.com"),
                   ("big", 12000, "example.com")])

    resp = post_preview(client, monkeypatch, fp,
                        **{"main_categories": ["Main A", "Main B"],
                           "min_size": "40000", "max_size": "50000",
                           "split_threshold": "1000", "max_domain_count": "5000",
                           "distribute_sites": "on"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    d = resp.get_json()["data"]
    assert d["distribute"] is True
    assert d["total_rows"] == 12500
    assert d["main_category_count"] == 2
    assert d["remaining_total"] == 12300
    assert d["extra_portions"] == 0
    assert d["domain_column"] is True
    assert d["site_total_rows"] == d["total_rows"]

    # example.com 共 12300 条 / 2 个网站 -> 上限自动抬到 6150
    assert d["domain_limit"]["enabled"] is True
    assert d["domain_limit"]["requested"] == 5000
    assert d["domain_limit"]["raised"] == [{"domain": "example.com",
                                            "requested": 5000,
                                            "effective": 6150,
                                            "total": 12300}]
    assert any("自动抬到 6150 条" in w for w in d["warnings"])

    sites = {s["category"]: s for s in d["sites"]}
    assert set(sites) == {"Main A", "Main B"}
    for s in sites.values():
        assert s["total_rows"] == s["main_rows"] + s["supp_rows"]
        # 该站累计 example.com 不超过抬高后的每站上限
        assert s["domain_max_rows"].get("example.com", 0) <= 6150
    assert sum(s["supp_rows"] for s in d["sites"]) == d["remaining_total"]


def test_preview_legacy_mode_reports_extra(client, workdir, monkeypatch):
    """未勾选均分：预测会提示额外补充份数（老行为）"""
    fp = workdir / "legacy.xlsx"
    make_xlsx(fp, [("Main A", 100, "main-a.com"),
                   ("Main B", 100, "main-b.com"),
                   ("s1", 4000, "example.com"),
                   ("s2", 4000, "example.com"),
                   ("s3", 4000, "example.com")])

    resp = post_preview(client, monkeypatch, fp,
                        **{"main_categories": ["Main A", "Main B"],
                           "min_size": "100", "max_size": "5000",
                           "split_threshold": "1000", "max_domain_count": "0",
                           "distribute_sites": "off"})  # 取消勾选 -> 旧模式
    assert resp.status_code == 200, resp.get_data(as_text=True)
    d = resp.get_json()["data"]
    assert d["distribute"] is False
    assert d["domain_limit"]["enabled"] is False
    assert d["file_count"] == 3            # 3 个分类各一份
    assert d["extra_portions"] == 1        # 2 个主分类 -> 多出 1 份额外补充
    assert len(d["sites"]) == 2


def test_preview_validation_errors(client, workdir, monkeypatch):
    fp = workdir / "errors.xlsx"
    make_xlsx(fp, [("Main A", 10, "a.com")])

    r = post_preview(client, monkeypatch, fp, **{"min_size": "40000"})
    assert r.status_code == 400
    assert "至少勾选一个主分类" in r.get_json()["error"]

    r = post_preview(client, monkeypatch, fp,
                     **{"main_categories": ["Main A"], "min_size": "50000", "max_size": "40000"})
    assert r.status_code == 400
    assert "最多条数必须大于最少条数" in r.get_json()["error"]

    r = post_preview(client, monkeypatch, fp,
                     **{"main_categories": ["不存在的分类"], "min_size": "40000"})
    assert r.status_code == 400
    assert "没有有效的主分类" in r.get_json()["error"]

    monkeypatch.setattr(product_data_routes, "resolve_export_file",
                        lambda folder, name: (_ for _ in ()).throw(FileNotFoundError("缺失")))
    r = client.post("/product-data/allocate-preview",
                    data={"folder": "uploads", "file": "x.xlsx",
                          "main_categories": ["Main A"]})
    assert r.status_code == 404

# ── 数据分配表单：复选框解析（浏览器会同时提交隐藏的 off 与勾选的 on） ──

def test_allocate_route_checkbox_flags(client, workdir, monkeypatch):
    """勾选/取消勾选/字段缺失三种情况都按预期解析（隐藏 off 不能覆盖勾选的 on）"""
    fp = workdir / "route_flags.xlsx"
    make_xlsx(fp, [("Main A", 100, "a.com"), ("other", 50, "a.com")])
    monkeypatch.setattr(product_data_routes, "resolve_export_file",
                        lambda folder, name: Path(fp))

    captured: dict = {}

    class _FakeTaskManager:
        def create(self, *a, **k):
            pass

        def start_task_thread(self, task_id, fn, *a, **k):
            captured["fn"] = fn

    monkeypatch.setattr(product_data_routes, "task_manager", _FakeTaskManager())

    def _fake_run(*args, **kwargs):
        captured["args"] = args

    monkeypatch.setattr(product_data_routes, "run_allocation_task", _fake_run)

    base = {"folder": "uploads", "file": fp.name,
            "main_categories": "Main A", "min_size": "40000",
            "max_size": "50000", "split_threshold": "3000",
            "max_domain_count": "5000"}

    def _post(extra):
        payload = dict(base)
        payload.update(extra)  # 值为列表 -> 同名多值（隐藏 off 在前、勾选的 on 在后）
        r = client.post("/product-data/allocate", data=payload)
        assert r.status_code in (200, 302), r.status_code
        captured["fn"]()
        args = captured["args"]
        return args[6], args[8]  # split_options, distribute_to_sites

    # 1) 全部勾选：浏览器提交顺序 = 隐藏 off 在前、勾选的 on 在后
    checked = {"distribute_sites": ["off", "on"],
               "split_enabled": ["off", "on"],
               "split_remove_source": ["off", "on"]}
    split_opts, distribute = _post(checked)
    assert distribute is True                       # 均分模式仍默认开启
    assert split_opts["enabled"] is True            # 拆表开启
    assert split_opts["remove_source"] is True      # 拆分后删除 main/supp 表

    # 2) 全部取消勾选：只提交隐藏的 off
    unchecked = {"distribute_sites": "off", "split_enabled": "off",
                 "split_remove_source": "off"}
    split_opts, distribute = _post(unchecked)
    assert distribute is False
    assert split_opts["enabled"] is False
    assert split_opts["remove_source"] is False

    # 3) 字段缺失（老调用方）：distribute/remove_source 默认开启，拆表默认关闭
    split_opts, distribute = _post({})
    assert distribute is True
    assert split_opts["enabled"] is False
    assert split_opts["remove_source"] is True
