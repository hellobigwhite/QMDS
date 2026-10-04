# -*- coding: utf-8 -*-
"""「复制勾选站点到粘贴板」测试

覆盖：导出数据构建（字段/时间格式化）、制表符文本生成（可粘贴进 Excel）、
copy_selected 接口（与导出 Excel 同源、无勾选时的报错、内容过大时的提示）。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.engine import create_app
from qmds.modules.web.routes import site_management as site_routes
from qmds.modules.web.routes.site_management import (
    build_site_export_rows,
    site_export_tsv,
)


class FakeSiteDB:
    def __init__(self, sites):
        self.sites = sites

    def get_site_by_id(self, sid):
        return self.sites.get(sid)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_build_site_export_rows_labels_and_time():
    """字段 -> 表头映射；时间字段统一格式化为 年/月/日；缺失站点跳过"""
    db = FakeSiteDB({
        "1": {"domain": "a.com", "build_time": "2026-09-01T12:30:00",
              "server": "s1", "template": "t1", "description": "desc"},
        "2": {"domain": "b.com", "build_time": "2026-09-02",
              "server": "s2", "template": "t2"},
    })
    rows, columns = build_site_export_rows(
        db, ["1", "2", "missing"], ["build_time", "domain", "server"])
    assert columns == ["建站时间", "域名", "服务器"]
    assert rows == [
        {"建站时间": "2026/09/01", "域名": "a.com", "服务器": "s1"},
        {"建站时间": "2026/09/02", "域名": "b.com", "服务器": "s2"},
    ]


def test_site_export_tsv_paste_safe():
    """制表符文本：首行表头、CRLF 换行；单元格内的制表符/换行被替换，不会串列"""
    rows = [
        {"域名": "a.com", "描述": "第一行\n第二行\t带制表符"},
        {"域名": "b.com", "描述": None},
    ]
    columns = ["域名", "描述"]
    text = site_export_tsv(rows, columns)
    lines = text.split("\r\n")
    assert lines[0] == "域名\t描述"
    assert lines[1] == "a.com\t第一行 第二行 带制表符"
    assert lines[2] == "b.com\t"
    assert len(lines) == 3
    assert "\n" not in text.replace("\r\n", "")  # 单元格里没有裸换行
    # 列顺序按 columns 决定
    assert site_export_tsv(rows, ["描述", "域名"]).split("\r\n")[0] == "描述\t域名"


def test_copy_selected_route_returns_tsv(client, monkeypatch):
    """copy_selected：返回与导出相同的制表符文本（含表头）"""
    db = FakeSiteDB({
        "1": {"domain": "a.com", "build_time": "2026-09-01T00:00:00"},
        "2": {"domain": "b.com", "build_time": "2026-09-02T00:00:00"},
    })
    monkeypatch.setattr(site_routes, "get_site_db", lambda: db)

    resp = client.post("/site-management/built", data={
        "action": "copy_selected",
        "selected_ids": ["1", "2"],
        "export_fields": ["build_time", "domain"],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    data = body["data"]
    assert data["count"] == 2
    assert data["columns"] == ["建站时间", "域名"]
    assert data["text"] == "建站时间\t域名\r\n2026/09/01\ta.com\r\n2026/09/02\tb.com"


def test_copy_selected_route_defaults_and_errors(client, monkeypatch):
    """未勾选字段时用默认字段；站点全部查不到时报错"""
    db = FakeSiteDB({"1": {"domain": "a.com", "server": "s1", "template": "t1"}})
    monkeypatch.setattr(site_routes, "get_site_db", lambda: db)

    # 不传 export_fields -> 默认 建站时间/域名/服务器/模板底板
    resp = client.post("/site-management/built", data={
        "action": "copy_selected", "selected_ids": ["1"]})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["columns"] == ["建站时间", "域名", "服务器", "模板底板"]

    # 勾选的站点都查不到 -> 400
    resp = client.post("/site-management/built", data={
        "action": "copy_selected", "selected_ids": ["nope"],
        "export_fields": ["domain"]})
    assert resp.status_code == 400
    assert "没有可复制的站点数据" in resp.get_json()["error"]


def test_copy_selected_route_too_large(client, monkeypatch):
    """内容超过上限时返回 413 并提示改用 Excel 导出"""
    db = FakeSiteDB({"1": {"domain": "a.com"}})
    monkeypatch.setattr(site_routes, "get_site_db", lambda: db)
    monkeypatch.setattr(site_routes, "MAX_CLIPBOARD_CHARS", 5)

    resp = client.post("/site-management/built", data={
        "action": "copy_selected", "selected_ids": ["1"], "export_fields": ["domain"]})
    assert resp.status_code == 413
    err = resp.get_json()["error"]
    assert "内容过大" in err and "导出勾选到Excel" in err
