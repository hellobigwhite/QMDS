# -*- coding: utf-8 -*-
"""upload_data 批量下图（图片处理阶段）测试

背景：one_dimg.php 返回「成功-N 失败-M」（带空格），旧实现按无空格的
「成功-0失败-0」子串判断完成，永远匹配不上；轮询固定 400 轮上限，跑满
或连续请求失败后直接按成功返回 —— 站点图片没下图完毕就显示
「上传成功/已上传」（下图不完全 + 中途提前显示完成）。

覆盖：
- 完成判定兼容两种格式（带空格 / 无空格）；
- 超过 400 轮仍继续下图直到完成（40k 商品的站点约需 800+ 轮）；
- 连续无进展（剩余图片反复失败）如实返回失败，不再假成功；
- 连续请求失败 / 无法解析进度时如实返回失败；
- 异常终止保留数据断点（final_cs），重跑可续传；
- 数据上传轮询达上限不再按成功返回且清除断点。
"""

import requests

import qmds.utils.site_operator as site_operator
from qmds.utils.site_operator import (
    DATA_MAX_ROUNDS,
    IMG_MAX_ROUNDS,
    IMG_STALL_ROUNDS,
    IMG_UNKNOWN_ROUNDS,
    SiteOperator,
    get_operator,
)

DOMAIN = "example.com"


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def json_resp(payload):
    return FakeResponse(200, payload)


class FakeSession:
    """按 URL 子串路由到脚本化响应的假会话

    pages: {url子串: [响应或异常, ...]}，列表耗尽后再请求即抛 AssertionError。
    """

    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for key, script in self.pages.items():
            if key in url:
                if not isinstance(script, list):
                    return script
                if not script:
                    raise AssertionError(f"脚本响应耗尽: {url}")
                item = script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"未预期的请求 URL: {url}")

    def close(self):
        pass


def patch_env(monkeypatch, pages):
    """替换 requests.Session 为 FakeSession 并去掉 sleep"""
    monkeypatch.setattr(site_operator.requests, "Session", lambda: FakeSession(pages))
    monkeypatch.setattr(site_operator.time, "sleep", lambda *_: None)


def data_script(*msgs):
    """数据上传阶段脚本：先返回若干进度响应，最后返回「完成」"""
    out = []
    for i, m in enumerate(msgs):
        n = (i + 1) * 100
        out.append(json_resp({"msg": m.format(n=n), "code": str(n)}))
    out.append(json_resp({"msg": "完成"}))
    return out


def run_upload(pages, **kwargs):
    op = get_operator()
    logs = []
    breakpoints = []
    result = op.upload_data(
        DOMAIN, "123",
        progress_callback=logs.append,
        start_cs=kwargs.pop("start_cs", "0"),
        breakpoint_callback=breakpoints.append,
        stop_callback=kwargs.pop("stop_callback", None),
    )
    return result, logs, breakpoints


def test_images_complete_with_space_format(monkeypatch):
    """one_dimg.php 带空格格式「成功-0 失败-0」能正确判定完成（旧实现漏判）"""
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": [
            json_resp({"msg": "成功-50 失败-0 执行时间:12秒"}),
            json_resp({"msg": "成功-50 失败-0 执行时间:11秒"}),
            json_resp({"msg": "成功-0 失败-0 执行时间:2秒"}),
        ],
    }
    patch_env(monkeypatch, pages)
    result, logs, _ = run_upload(pages)

    assert result["success"] is True
    assert "图片成功100张" in result["message"]
    assert any("图片处理完成" in m for m in logs)
    assert result["final_cs"] == "0"


def test_images_complete_legacy_no_space_format(monkeypatch):
    """旧 dimg.php 无空格格式「成功-0失败-0」仍能判定完成（回归保护）"""
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": [
            json_resp({"msg": "成功-200失败-0 执行时间:5秒"}),
            json_resp({"msg": "成功-0失败-0 执行时间:1秒"}),
        ],
    }
    patch_env(monkeypatch, pages)
    result, logs, _ = run_upload(pages)

    assert result["success"] is True
    assert "图片成功200张" in result["message"]
    assert any("图片处理完成" in m for m in logs)


def test_images_continue_past_400_rounds(monkeypatch):
    """超过旧 400 轮上限后继续下图直到完成（修复「下图不完全」）"""
    rounds = 500  # 500轮 x 50张 = 25000张，旧实现 400 轮即假成功
    img_script = [json_resp({"msg": "成功-50 失败-0 执行时间:12秒"}) for _ in range(rounds)]
    img_script.append(json_resp({"msg": "成功-0 失败-0 执行时间:1秒"}))
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": img_script,
    }
    patch_env(monkeypatch, pages)
    result, logs, _ = run_upload(pages)

    assert result["success"] is True
    assert "图片成功25000张" in result["message"]
    # 全部响应都应被消费（500 轮进度 + 1 轮完成）
    assert not pages["one_dimg.php"]


def test_images_stall_aborts_honestly(monkeypatch):
    """连续无进展（成功0张反复失败）如实返回失败，不再提前显示完成"""
    stall = IMG_STALL_ROUNDS + 10
    img_script = [json_resp({"msg": "成功-0 失败-50 执行时间:3秒"}) for _ in range(stall)]
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script("成功:100失败:0-重复0-名牌0已上传-{n}执行时间5秒"),
        "one_dimg.php": img_script,
    }
    patch_env(monkeypatch, pages)
    result, logs, breakpoints = run_upload(pages)

    assert result["success"] is False
    assert "图片处理未完成" in result["message"]
    assert "无进展" in result["message"]
    assert "下图成功0张" in result["message"]
    # 数据断点保留（final_cs 为数据阶段最后的 code），重跑可续传下图
    assert result["final_cs"] == "100"
    assert breakpoints[-1] == "100"
    assert not any("上传完成" in m for m in logs)


def test_images_request_errors_abort_honestly(monkeypatch):
    """连续请求失败超过上限：如实返回失败并说明原因"""
    img_script = [requests.exceptions.ConnectionError("boom") for _ in range(IMG_MAX_RETRIES + 2)]
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": img_script,
    }
    patch_env(monkeypatch, pages)
    result, logs, _ = run_upload(pages)

    assert result["success"] is False
    assert "图片处理未完成" in result["message"]
    assert "请求失败" in result["message"]
    assert "boom" in result["message"]


def test_images_unparsable_message_aborts(monkeypatch):
    """连续无法解析进度的消息：终止并如实报告，避免死循环"""
    img_script = [json_resp({"msg": "数据库连接失败"}) for _ in range(IMG_UNKNOWN_ROUNDS + 2)]
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": img_script,
    }
    patch_env(monkeypatch, pages)
    result, logs, _ = run_upload(pages)

    assert result["success"] is False
    assert "图片处理未完成" in result["message"]
    assert "无法解析" in result["message"]
    assert "数据库连接失败" in result["message"]


def test_images_stop_callback(monkeypatch):
    """图片处理阶段收到停止信号：返回用户停止"""
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": data_script(),
        "one_dimg.php": [json_resp({"msg": "成功-50 失败-0 执行时间:12秒"})] * 50,
    }

    def make_session():
        s = FakeSession(pages)
        sessions.append(s)
        return s

    sessions = []

    def stop():
        return any("one_dimg.php" in u for s in sessions for u in s.requested)

    monkeypatch.setattr(site_operator.requests, "Session", make_session)
    monkeypatch.setattr(site_operator.time, "sleep", lambda *_: None)

    result, logs, _ = run_upload(pages, stop_callback=stop)

    assert result["success"] is False
    assert result["message"] == "用户停止"
    assert any("中止图片处理" in m for m in logs)


def test_data_rounds_cap_returns_failure_with_breakpoint(monkeypatch):
    """数据上传轮询达上限：不再按成功返回，断点保留可续传"""
    script = []
    for i in range(DATA_MAX_ROUNDS):
        n = (i + 1) * 100
        script.append(json_resp({
            "msg": f"成功:100失败:0-重复0-名牌0已上传-{n}执行时间1秒",
            "code": str(n),
        }))
    pages = {
        "options-general.php": FakeResponse(404, ""),
        "dan_duopsot.php": script,
    }
    patch_env(monkeypatch, pages)
    result, logs, breakpoints = run_upload(pages)

    assert result["success"] is False
    assert "轮询达" in result["message"] and "上限" in result["message"]
    assert result["final_cs"] == str(DATA_MAX_ROUNDS * 100)
    assert breakpoints[-1] == str(DATA_MAX_ROUNDS * 100)
    assert not any("上传完成" in m for m in logs)


def test_img_progress_regex_formats():
    """IMG_PROGRESS_RE 兼容带空格/无空格两种格式且不误判"""
    re_ = site_operator.IMG_PROGRESS_RE
    assert re_.search("成功-50 失败-0 执行时间:18秒").groups() == ("50", "0")
    assert re_.search("成功-171失败-0 执行时间:5秒").groups() == ("171", "0")
    assert re_.search("成功-0 失败-0 执行时间:2秒").groups() == ("0", "0")
    assert re_.search("成功-0失败-0").groups() == ("0", "0")
    # 「成功-100 失败-0」不应被误判为完成信号
    m = re_.search("成功-100 失败-0 执行时间:3秒")
    assert (int(m.group(1)), int(m.group(2))) == (100, 0)
    assert re_.search("数据库连接失败") is None


def test_watchdog_constants_sane():
    """守护阈值合理性：上限远高于实际需要，防死循环又不过早终止"""
    assert IMG_MAX_ROUNDS > 4000      # 40k商品/50张每轮 ≈ 800轮，上限留足余量
    assert IMG_STALL_ROUNDS >= 30
    assert IMG_UNKNOWN_ROUNDS >= 5
    assert DATA_MAX_ROUNDS >= 800
