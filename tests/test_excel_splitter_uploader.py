# -*- coding: utf-8 -*-
"""批量拆表（excel_splitter）与站群上传（site_uploader）单元测试"""

import shutil
import sys
import uuid
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services.excel_splitter import (
    SUFFIX_CUSTOM, SUFFIX_NONE, SUFFIX_PART, split_excel_file)
from qmds.modules.web.services import site_uploader

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录（默认权限 mkdir）"""
    d = Path(".tmp") / f"split_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_file(path, rows, domain="example.com"):
    df = pd.DataFrame([{
        "SKU": f"S{i}", "Name": f"P{i}", "Description": "d",
        "Regular price": 9.9, "Categories": "Cat", "Images": "",
        "cf_opingts": "", "自定义分类": "", "原站域名": domain,
        "分布网站识别": 0, "语言": "en",
    } for i in range(rows)], columns=EXPORT_COLUMNS)
    df.to_excel(path, index=False, engine="openpyxl")
    return path


# ── excel_splitter ─────────────────────────────

def test_split_by_rows(workdir):
    """补充数据按行数拆分：12000 行 / 5000 -> 3 份（5000+5000+2000）"""
    fp = make_file(workdir / "supp.xlsx", 12000)
    result = split_excel_file(fp, rows_per_file=5000)

    assert result["rows"] == 12000
    assert len(result["parts"]) == 3
    sizes = [len(pd.read_excel(p, engine="openpyxl")) for p in result["parts"]]
    assert sorted(sizes, reverse=True) == [5000, 5000, 2000]
    # 输出文件夹: 源文件旁 {名称}_split
    assert result["output_folder"] == workdir / "supp_split"
    # 文件名格式: supp_part{N}_{2字母}{时间戳}.xlsx
    names = [p.name for p in result["parts"]]
    for n in names:
        assert n.startswith("supp_part") and n.endswith(".xlsx")
    part_nums = sorted(int(n.split("_part")[1].split("_")[0]) for n in names)
    assert part_nums == [1, 2, 3]
    # 数据不重不漏
    all_df = pd.concat([pd.read_excel(p, engine="openpyxl") for p in result["parts"]])
    assert sorted(all_df["SKU"]) == sorted(f"S{i}" for i in range(12000))
    # 源文件默认保留
    assert fp.exists()


def test_split_unlimited_main_data(workdir):
    """主数据不设上限（rows_per_file=None）：整表一份"""
    fp = make_file(workdir / "main.xlsx", 8000)
    result = split_excel_file(fp, rows_per_file=None)

    assert len(result["parts"]) == 1
    assert result["rows"] == 8000
    df = pd.read_excel(result["parts"][0], engine="openpyxl")
    assert len(df) == 8000
    # rows_per_file=0 同样表示不限制
    result0 = split_excel_file(fp, rows_per_file=0)
    assert len(result0["parts"]) == 1


def test_split_suffix_modes(workdir):
    """原站域名后缀模式：none 不处理 / custom 自定义 / part 按分卷"""
    fp = make_file(workdir / "suffix.xlsx", 250)

    r1 = split_excel_file(fp, rows_per_file=100, suffix_mode=SUFFIX_NONE)
    for p in r1["parts"]:
        assert (pd.read_excel(p, engine="openpyxl")["原站域名"] == "example.com").all()

    r2 = split_excel_file(fp, rows_per_file=100, suffix_mode=SUFFIX_CUSTOM,
                          custom_suffix="us-01")
    dfs = [pd.read_excel(p, engine="openpyxl") for p in r2["parts"]]
    for df in dfs:
        assert (df["原站域名"] == "example.com_us-01").all()

    r3 = split_excel_file(fp, rows_per_file=100, suffix_mode=SUFFIX_PART)
    for i, p in enumerate(r3["parts"], start=1):
        df = pd.read_excel(p, engine="openpyxl")
        assert (df["原站域名"] == f"example.com_part{i}").all()

    # 各模式行数守恒
    for r in (r1, r2, r3):
        assert sum(len(pd.read_excel(p, engine="openpyxl")) for p in r["parts"]) == 250


def test_split_remove_source(workdir):
    """拆分成功后删除源文件"""
    fp = make_file(workdir / "gone.xlsx", 300)
    result = split_excel_file(fp, rows_per_file=200, remove_source=True)
    assert not fp.exists()
    assert len(result["parts"]) == 2


def test_split_preserves_all_columns(workdir):
    """拆分保留全部导出列与表头"""
    fp = make_file(workdir / "cols.xlsx", 150)
    result = split_excel_file(fp, rows_per_file=100)
    df = pd.read_excel(result["parts"][0], engine="openpyxl")
    assert list(df.columns) == EXPORT_COLUMNS


def test_split_skips_empty_rows(workdir):
    """整行空行不计入拆分（表尾空行常见）"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(EXPORT_COLUMNS)
    for i in range(10):
        ws.append([f"S{i}", f"P{i}", "d", 9.9, "Cat", "", "", "", "x.com", 0, "en"])
    ws.append([None] * len(EXPORT_COLUMNS))
    ws.append([None] * len(EXPORT_COLUMNS))
    fp = workdir / "empty_tail.xlsx"
    wb.save(fp)

    result = split_excel_file(fp, rows_per_file=4)
    assert result["rows"] == 10
    assert len(result["parts"]) == 3


def test_split_invalid_file(workdir):
    """空表格报错"""
    from openpyxl import Workbook
    wb = Workbook()
    fp = workdir / "blank.xlsx"
    wb.save(fp)
    with pytest.raises(ValueError):
        split_excel_file(fp, rows_per_file=100)


# ── site_uploader ─────────────────────────────

def test_parse_fields():
    """类 JSON 响应解析（移植原工具逻辑，含值截断行为）"""
    d = site_uploader._parse_fields('{"code":"0","msg":"500","yz":"123","cat":"abc"}')
    assert d["code"] == "0"
    assert d["msg"] == "500"
    assert d["yz"] == "123"
    assert d["cat"] == "abc"
    # URL 值被冒号截断为 https（原工具依赖该行为）
    d2 = site_uploader._parse_fields('{"code":"2","msg":"https://erp.xx.com/a"}')
    assert d2["msg"] == "https"
    assert d2["code"] == "2"


def test_collect_xlsx_files(workdir):
    """收集文件夹全部 .xlsx（含子文件夹，跳过 ~$ 临时文件与非 xlsx）"""
    sub = workdir / "sub"
    sub.mkdir()
    make_file(workdir / "a.xlsx", 3)
    make_file(sub / "b.xlsx", 3)
    (workdir / "c.txt").write_text("x", encoding="utf-8")
    (workdir / "~$d.xlsx").write_bytes(b"fake")

    files = site_uploader.collect_xlsx_files(workdir)
    assert files == [workdir / "a.xlsx", sub / "b.xlsx"]
    # 单文件目标
    assert site_uploader.collect_xlsx_files(workdir / "a.xlsx") == [workdir / "a.xlsx"]
    # 不存在的路径
    with pytest.raises(FileNotFoundError):
        site_uploader.collect_xlsx_files(workdir / "nope")


def test_config_roundtrip(workdir, tmp_path=None):
    """配置保存/读取/校验"""
    with mock.patch.object(site_uploader, "_config_path",
                           return_value=workdir / "site_upload_config.json"):
        site_uploader.save_upload_config({
            "login_url": "https://x.com/login", "upload_page_url": "https://x.com/up",
            "username": "user", "password": "pw"})
        cfg = site_uploader.load_upload_config()
        assert cfg["username"] == "user"
        assert cfg["password"] == "pw"

        # 缺少必填项 -> ValueError
        with pytest.raises(ValueError):
            site_uploader.validate_config({"login_url": "", "upload_page_url": "u",
                                           "username": "u", "password": "p"})


def test_upload_one_table_polling(workdir):
    """上传 + 轮询流程（mock HTTP 会话）"""
    fp = make_file(workdir / "up.xlsx", 5)

    class FakeResponse:
        def __init__(self, text, status=200):
            self.text = text
            self.status_code = status

    responses = [
        FakeResponse('{"code":"0","msg":"500","yz":"77","cat":"c1"}'),  # 初始上传响应
        FakeResponse('{"code":"0","msg":"1000","yz":"77","cat":"c1"}'),  # 第 1 次轮询
        FakeResponse('{"msg":"完成","yz":"77"}'),                        # 第 2 次轮询 -> 完成
    ]
    session = mock.MagicMock()
    session.post.return_value = responses[0]
    state = {"get_i": 1}  # GET 轮询从 responses[1] 开始

    def fake_get(url, **kw):
        r = responses[state["get_i"]]
        state["get_i"] += 1
        return r

    session.get.side_effect = fake_get

    pid = site_uploader.upload_one_table(session, fp,
                                         "https://x.com/index.php?dongzuo=add_cp_pl",
                                         "https://x.com/login")
    assert pid == "77"
    # 上传 POST 带文件
    _, post_kwargs = session.post.call_args
    assert "file" in post_kwargs["files"]
    # 轮询 URL 拼接正确
    first_get = session.get.call_args_list[0][0][0]
    assert "cs=2" in first_get and "w=500" in first_get and "yz=77" in first_get
    second_get = session.get.call_args_list[1][0][0]
    assert "w=1000" in second_get


def test_upload_one_table_server_error(workdir):
    """服务器返回错误信息 -> 抛 RuntimeError"""
    fp = make_file(workdir / "err.xlsx", 3)

    class FakeResponse:
        text = '{"code":"1","msg":"文件格式不对","yz":"0","cat":""}'
        status_code = 200

    session = mock.MagicMock()
    session.post.return_value = FakeResponse()
    with pytest.raises(RuntimeError, match="文件格式不对"):
        site_uploader.upload_one_table(session, fp, "https://x.com/up",
                                       "https://x.com/login")


def test_login_failure():
    """登录失败（返回 msg）-> RuntimeError"""
    class FakeResponse:
        text = '{"msg":"密码错误"}'
        status_code = 200

    session = mock.MagicMock()
    session.post.return_value = FakeResponse()
    with pytest.raises(RuntimeError, match="密码错误"):
        site_uploader.login(session, {"login_url": "https://x.com/login",
                                      "username": "u", "password": "bad"})


def test_save_upload_ids_category_folder(workdir):
    """新结构：主数据与补充数据同文件夹，main 前缀的主数据 ID 在最前"""
    cat_dir = workdir / "CatA"
    extra_dir = workdir / "extra1"
    cat_dir.mkdir()
    extra_dir.mkdir()

    main_f = cat_dir / "mainCatA_part1_AB123.xlsx"
    supp1 = cat_dir / "CatA_supp_part1_CD456.xlsx"
    supp2 = cat_dir / "CatA_supp_part2_EF789.xlsx"
    extra1 = extra_dir / "extra1_part1_GH012.xlsx"
    for f in (main_f, supp1, supp2, extra1):
        make_file(f, 2)

    # 上传顺序打乱：补充先上传，主数据 ID 仍排最前
    uploaded = [(supp1, "102"), (main_f, "101"), (supp2, "103"), (extra1, "104")]
    written = site_uploader.save_upload_ids(workdir, uploaded)

    assert set(written) == {cat_dir / "数据ID.txt", extra_dir / "数据ID.txt"}
    assert (cat_dir / "数据ID.txt").read_text(encoding="utf-8").splitlines() \
        == ["101", "102", "103"]
    assert (extra_dir / "数据ID.txt").read_text(encoding="utf-8").splitlines() == ["104"]


def test_save_upload_ids_old_structure(workdir):
    """旧版结构兼容：X_split 与 X_补充_split 的 ID 合并到主数据文件夹，主数据在前"""
    main_dir = workdir / "CatB_split"
    supp_dir = workdir / "CatB_补充_split"
    main_dir.mkdir()
    supp_dir.mkdir()
    main_f = make_file(main_dir / "CatB_part1_X.xlsx", 2)
    supp_f = make_file(supp_dir / "CatB_补充_part1_Y.xlsx", 2)

    uploaded = [(supp_f, "202"), (main_f, "201")]
    written = site_uploader.save_upload_ids(workdir, uploaded)
    assert (main_dir / "数据ID.txt").read_text(encoding="utf-8").splitlines() \
        == ["201", "202"]
    assert not (supp_dir / "数据ID.txt").exists()


def test_save_upload_ids_flat_folder(workdir):
    """普通文件夹（散表）：main 前缀的主数据 ID 在前"""
    m1 = make_file(workdir / "mainCatA.xlsx", 2)
    m2 = make_file(workdir / "mainCatB.xlsx", 2)
    s1 = make_file(workdir / "CatA_supp.xlsx", 2)
    uploaded = [(s1, "301"), (m1, "302"), (m2, "303")]
    written = site_uploader.save_upload_ids(workdir, uploaded)
    assert written == [workdir / "数据ID.txt"]
    lines = (workdir / "数据ID.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["302", "303", "301"]


def test_save_upload_ids_single_file(workdir):
    """单文件目标：ID txt 保存在文件所在文件夹"""
    sub = workdir / "CatC"
    sub.mkdir()
    f = make_file(sub / "mainCatC_part1_X.xlsx", 2)
    written = site_uploader.save_upload_ids(f, [(f, "401")])
    assert written == [sub / "数据ID.txt"]
    assert (sub / "数据ID.txt").read_text(encoding="utf-8").splitlines() == ["401"]


def test_collect_xlsx_files_natural_order(workdir):
    """分卷自然排序：part1, part2, ..., part10（而非字典序 part1, part10, part2）"""
    d = workdir / "CatA"
    d.mkdir()
    for n in (1, 2, 10):
        make_file(d / f"CatA_supp_part{n}_AB{n}.xlsx", 1)
    files = site_uploader.collect_xlsx_files(workdir)
    names = [p.name for p in files]
    assert names == ["CatA_supp_part1_AB1.xlsx", "CatA_supp_part2_AB2.xlsx",
                     "CatA_supp_part10_AB10.xlsx"], names


def test_run_upload_task_flow(workdir):
    """完整上传任务（mock requests.Session）：登录 -> 上传 -> 数据ID保存到txt"""

    class FakeResponse:
        def __init__(self, text, status=200):
            self.text = text
            self.status_code = status

    # 模拟分配+拆表输出结构: CatA 文件夹内主数据 1 份 + 补充 2 份
    main_dir = workdir / "CatA"
    main_dir.mkdir()
    files = [
        make_file(main_dir / "mainCatA_part1_AB1.xlsx", 4),
        make_file(main_dir / "CatA_supp_part1_CD2.xlsx", 4),
        make_file(main_dir / "CatA_supp_part2_EF3.xlsx", 4),
    ]

    state = {"upload_i": 0, "get_urls": []}

    session = mock.MagicMock()

    def fake_post(url, **kw):
        if url == "https://x.com/login":
            return FakeResponse("<html>ok</html>")  # 登录成功（无 msg）
        state["upload_i"] += 1
        return FakeResponse('{"code":"0","msg":"500","yz":"%d","cat":"c"}'
                            % (100 + state["upload_i"]))

    def fake_get(url, **kw):
        state["get_urls"].append(url)
        return FakeResponse('{"msg":"完成","yz":"0"}')

    session.post.side_effect = fake_post
    session.get.side_effect = fake_get

    from qmds.modules.web.task_manager import task_manager
    task_id = "test_upload_flow"
    task_manager.create(task_id, "site_upload_huisheng", "test")
    cfg = {"login_url": "https://x.com/login",
           "upload_page_url": "https://x.com/up?dongzuo=add_cp_pl",
           "username": "u", "password": "p"}

    with mock.patch("requests.Session", return_value=session):
        site_uploader.run_upload_task(task_id, workdir, cfg)

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "上传 3/3" in task["message"]
    assert "数据ID已保存到 1 个 txt" in task["message"]

    # 轮询只发生上传状态查询，不再有触发下载原图请求
    assert all("dz=xztp" not in u for u in state["get_urls"])

    # 数据ID txt: 主数据与补充数据同文件夹，主数据 ID 在最前
    # （自然排序上传顺序为 CatA_supp_part1/2 -> mainCatA_part1，即主数据 ID 为 103，
    #   排序后主数据 ID 仍排在补充数据 ID 之前）
    txt = main_dir / "数据ID.txt"
    assert txt.exists()
    assert txt.read_text(encoding="utf-8").splitlines() == ["103", "101", "102"]
