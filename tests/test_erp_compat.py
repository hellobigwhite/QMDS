# -*- coding: utf-8 -*-
"""ERP 兼容格式转换（inlineStr -> sharedStrings）单元测试"""

import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services.site_uploader import (
    _REL_ATTR_RE, make_erp_compatible)


@pytest.fixture
def workdir():
    d = Path(".tmp") / f"erp_compat_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def write_table(path, df):
    df.to_excel(path, index=False, engine="openpyxl")
    return path


def inspect(path):
    """返回 (有无 sharedStrings, 有无 inlineStr, 工作表名)"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        has_ss = "xl/sharedStrings.xml" in names
        sheet = next(n for n in names
                     if n.startswith("xl/worksheets/sheet"))
        xml = z.read(sheet).decode("utf-8")
        import re
        m = re.search(r'<sheet [^>]*name="([^"]+)"',
                      z.read("xl/workbook.xml").decode("utf-8"))
        return has_ss, "inlineStr" in xml, m.group(1)


def test_convert_basic(workdir):
    """内联字符串 -> 共享字符串；工作表名 -> Sheet；数据逐值一致"""
    df = pd.DataFrame([
        {"SKU": "S1", "Name": "American Standard 智能马桶", "价格": 1028.35,
         "描述": "<p>The <b>Toilet</b> & Tank</p>", "备注": "  前后空格  "},
        {"SKU": "S2", "Name": "两截式马桶水箱", "价格": 838,
         "描述": "line1\nline2", "备注": ""},
        {"SKU": "S3", "Name": None, "价格": None,
         "描述": "特殊字符 <>&'\"", "备注": "0"},
    ])
    fp = write_table(workdir / "t.xlsx", df)
    has_ss, inline, sheet = inspect(fp)
    assert not has_ss and inline and sheet == "Sheet1"  # 转换前: openpyxl 内联
    before = pd.read_excel(fp, engine="openpyxl")  # "" 在 xlsx 中读出即为 NaN

    assert make_erp_compatible(fp) is True

    has_ss, inline, sheet = inspect(fp)
    assert has_ss, "应有 sharedStrings.xml"
    assert not inline, "不应再有 inlineStr"
    assert sheet == "Sheet"

    # 数据逐值一致（含 None/空串/空格/换行/实体字符）
    after = pd.read_excel(fp, engine="openpyxl")
    assert after.equals(before)
    assert list(after.columns) == list(df.columns)
    # 前后空格保留
    assert after["备注"].iloc[0] == "  前后空格  "
    # None / 空串 / "0" 均按 xlsx 语义读出
    assert pd.isna(after["Name"].iloc[2])
    assert after["备注"].iloc[1] == "" or pd.isna(after["备注"].iloc[1])
    assert after["备注"].iloc[2] == "0"

    # 注册部件完整（openpyxl 能读 = rels/content-types 正确）
    with zipfile.ZipFile(fp) as z:
        ct = z.read("[Content_Types].xml").decode("utf-8")
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        assert "/xl/sharedStrings.xml" in ct
        assert "sharedStrings.xml" in rels


def test_convert_idempotent(workdir):
    """二次转换不再修改（返回 False，字节不变）"""
    fp = write_table(workdir / "t.xlsx",
                     pd.DataFrame([{"A": "x", "B": 1}, {"A": "y", "B": 2}]))
    assert make_erp_compatible(fp) is True
    data = fp.read_bytes()
    assert make_erp_compatible(fp) is False
    assert fp.read_bytes() == data


def test_convert_all_types(workdir):
    """混合类型列（数字/字符串/NaN）转换后一致"""
    df = pd.DataFrame({
        "int": [1, 2, 3],
        "float": [1.5, None, 3.25],
        "str": ["a", None, "中文"],
        "mixed": [1, "b", None],
        "lang": ["en"] * 3,
    })
    fp = write_table(workdir / "t2.xlsx", df)
    assert make_erp_compatible(fp) is True
    after = pd.read_excel(fp, engine="openpyxl")
    assert after.equals(df)


def test_convert_real_shape(workdir):
    """真实结构（导出 11 列 + 域名标记）转换后一致"""
    cols = ["SKU", "Name", "Description", "Regular price", "Categories",
            "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]
    df = pd.DataFrame([{
        "SKU": f"M5000A{i}", "Name": f"Product {i}",
        "Description": "<h2>Desc</h2>", "Regular price": 9.99 + i,
        "Categories": "Cat", "Images": "https://cdn.x.com/1.jpg",
        "cf_opingts": "Toilet shape^El" if i % 2 else None,
        "自定义分类": "五金", "原站域名": "site.com_main_part1" if i < 5 else "old.com",
        "分布网站识别": 0, "语言": "en",
    } for i in range(30)], columns=cols)
    fp = write_table(workdir / "t3.xlsx", df)
    before = pd.read_excel(fp, engine="openpyxl")
    assert make_erp_compatible(fp) is True
    has_ss, inline, sheet = inspect(fp)
    assert has_ss and not inline and sheet == "Sheet"
    after = pd.read_excel(fp, engine="openpyxl")
    assert after.equals(before)
    # 域名标记与关键列保持原值
    assert list(after["原站域名"][:5]) == ["site.com_main_part1"] * 5
    assert list(after["原站域名"][5:]) == ["old.com"] * 25
    assert after["自定义分类"].eq("五金").all()


def _reencode_ss_refs(path):
    """模拟旧版转换残留：把 sharedStrings 中的非 ASCII 重编码为数字字符引用"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    ss = contents["xl/sharedStrings.xml"].decode("utf-8")
    ss = re.sub(r"[^\x00-\x7f]", lambda m: f"&#{ord(m.group())};", ss)
    contents["xl/sharedStrings.xml"] = ss.encode("utf-8")
    with zipfile.ZipFile(path, "w") as zout:
        for n, data in contents.items():
            zout.writestr(n, data)
    return path


def test_repair_ss_char_refs(workdir):
    """修复路径：已转换文件的 sharedStrings 残留数字字符引用 -> 解码为原始字符

    早期版本转换未解码 &#NNNN; 引用，且幂等检查跳过这些文件导致坏格式
    永久残留（ERP 字符串比较列名失败）。修复后应自动检出并解码。
    """
    df = pd.DataFrame([
        {"SKU": "S1", "自定义分类": "五金", "原站域名": "a.com_main_part1",
         "描述": "A&B <tag> 'q'"},
        {"SKU": "S2", "自定义分类": "工具", "原站域名": "b.com_part1",
         "描述": "中文&符号"},
    ])
    fp = write_table(workdir / "t.xlsx", df)
    assert make_erp_compatible(fp) is True  # 首次转换（新逻辑已解码）
    _reencode_ss_refs(fp)  # 模拟旧版坏输出
    with zipfile.ZipFile(fp) as z:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8")
    assert re.search(r"&#\d+;", ss), "模拟失败：应有数字字符引用"
    before = pd.read_excel(fp, engine="openpyxl")

    assert make_erp_compatible(fp) is True  # 修复路径触发

    with zipfile.ZipFile(fp) as z:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8")
        sheet = next(n for n in z.namelist()
                     if n.startswith("xl/worksheets/sheet"))
        sheet_xml = z.read(sheet).decode("utf-8")
    assert not re.search(r"&#\d+;", ss), "数字字符引用应已解码"
    assert "自定义分类" in ss and "原站域名" in ss  # 中文以原始字符存在
    assert "&amp;" in ss  # 标准 XML 转义保留不解码
    assert not re.search(r"&#\d+;", sheet_xml)
    after = pd.read_excel(fp, engine="openpyxl")
    assert after.equals(before)  # 数据逐值一致
    assert list(after.columns) == list(df.columns)
    assert after["自定义分类"].tolist() == ["五金", "工具"]

    # 幂等：修复后不再修改
    data = fp.read_bytes()
    assert make_erp_compatible(fp) is False
    assert fp.read_bytes() == data


def test_repair_mixed_format_keeps_indexes(workdir):
    """混合格式（已有 sharedStrings + inlineStr）不转字符串，但引用仍解码"""
    fp = write_table(workdir / "t.xlsx",
                     pd.DataFrame([{"A": "中文x", "B": 1}]))
    assert make_erp_compatible(fp) is True
    _reencode_ss_refs(fp)
    # 注入一个 inlineStr 单元格（B 列改写为内联字符串）构成混合格式
    with zipfile.ZipFile(fp) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    sheet = next(n for n in names if n.startswith("xl/worksheets/sheet"))
    xml = contents[sheet].decode("utf-8")
    assert '<c r="B2" t="n"><v>1</v></c>' in xml, "注入目标单元格形态不符"
    xml = xml.replace(
        '<c r="B2" t="n"><v>1</v></c>',
        '<c r="B2" t="inlineStr"><is><t>1</t></is></c>')
    contents[sheet] = xml.encode("utf-8")
    with zipfile.ZipFile(fp, "w") as zout:
        for n, data in contents.items():
            zout.writestr(n, data)

    msgs = []
    changed = make_erp_compatible(fp, log_fn=lambda m, lv="info": msgs.append(m))
    assert changed is True
    assert any("跳过格式转换" in m for m in msgs)
    # 引用被解码
    with zipfile.ZipFile(fp) as z:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8")
    assert not re.search(r"&#\d+;", ss)
    # inlineStr 保留（未做字符串转换）
    with zipfile.ZipFile(fp) as z:
        sheet_xml = z.read(
            next(n for n in z.namelist()
                 if n.startswith("xl/worksheets/sheet"))).decode("utf-8")
    assert 't="inlineStr"' in sheet_xml
    # 数据仍可读且一致
    after = pd.read_excel(fp, engine="openpyxl")
    assert after["A"].tolist() == ["中文x"]


def test_normalize_rels_targets(workdir):
    """rels 规范化：绝对 Target -> 相对；属性顺序 -> Id/Type/Target

    魔改 openpyxl 把 worksheet 关系写成 Target="/xl/worksheets/sheet1.xml"
    且 Type/Target/Id 顺序；ERP 的 PHP 解析器按目录拼接解析路径、按属性
    顺序做正则匹配，两种偏差都会找不到 worksheet（表错了）。
    """
    fp = write_table(workdir / "t.xlsx",
                     pd.DataFrame([{"A": "中文x", "B": 1}]))
    with zipfile.ZipFile(fp) as z:
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    # 魔改 openpyxl 形态: 绝对 Target + Type 在前
    assert 'Target="/xl/worksheets/sheet1.xml"' in rels, rels
    pos_type = rels.index('Type=')
    pos_id = rels.index('Id=')
    assert pos_type < pos_id

    assert make_erp_compatible(fp) is True

    with zipfile.ZipFile(fp) as z:
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        root = z.read("_rels/.rels").decode("utf-8")
    assert 'Target="worksheets/sheet1.xml"' in rels
    assert 'Target="/xl/' not in rels
    assert 'Target="/xl/' not in root
    # 全部 Relationship 属性顺序 = Id, Type, Target
    for tag in re.findall(r"<Relationship\b[^>]*/>", rels + root):
        attrs = [a for a, _ in _REL_ATTR_RE.findall(tag)]
        assert attrs[:3] == ["Id", "Type", "Target"], tag
    # 数据不变
    after = pd.read_excel(fp, engine="openpyxl")
    assert after["A"].tolist() == ["中文x"]
    assert after["B"].tolist() == [1]
    # 幂等
    data = fp.read_bytes()
    assert make_erp_compatible(fp) is False
    assert fp.read_bytes() == data


def test_upload_task_converts_before_post(workdir):
    """按网站上传任务：POST 到服务器的字节已是 sharedStrings 格式"""
    from unittest import mock

    from qmds.modules.web.services import site_uploader
    from qmds.modules.web.services.site_uploader import run_domain_upload_task
    from qmds.modules.web.task_manager import task_manager

    cols = ["SKU", "Name", "原站域名"]
    (workdir / "a.com").mkdir()
    pd.DataFrame([{"SKU": "S1", "Name": "x", "原站域名": "a.com_part1"}],
                 columns=cols).to_excel(
        workdir / "a.com" / "data_mainX_part1_A.xlsx",
        index=False, engine="openpyxl")

    uploaded_bytes = {}

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200

    session = mock.MagicMock()

    def fake_post(url, **kw):
        if url == "https://x.com/login":
            return FakeResponse("<html>ok</html>")
        name, fobj = kw["files"]["file"]
        uploaded_bytes[name] = fobj.read()
        return FakeResponse('{"code":"0","msg":"500","yz":"77","cat":"c"}')

    session.post.side_effect = fake_post
    session.get.side_effect = lambda u, **k: FakeResponse('{"msg":"完成"}')

    task_id = "test_compat_upload"
    task_manager.create(task_id, "site_upload_huisheng", "test")
    with mock.patch("requests.Session", return_value=session):
        run_domain_upload_task(task_id, workdir, ["a.com"],
                               {"login_url": "https://x.com/login",
                                "upload_page_url": "https://x.com/up",
                                "username": "u", "password": "p"})
    assert task_manager.get(task_id)["status"] == "completed"

    # POST 出去的文件内容已是共享字符串格式
    name, blob = next(iter(uploaded_bytes.items()))
    import io
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert "xl/sharedStrings.xml" in z.namelist()
        sheet = next(n for n in z.namelist()
                     if n.startswith("xl/worksheets/sheet"))
        assert "inlineStr" not in z.read(sheet).decode("utf-8")
    # 上传后本地文件也已转换（幂等：下次直接上传）
    has_ss, inline, _ = inspect(workdir / "a.com" / name)
    assert has_ss and not inline
    # 转换日志
    logs = [e["message"] for e in task_manager.get_logs(task_id)]
    assert any("已转为 ERP 兼容格式" in m for m in logs)


# ── CRLF 回车行尾（真实数据回归：描述含 \r\n 导致校验不一致）──────

def test_decode_char_refs_safety():
    """解码安全边界：CR 保留引用；& < > 转命名实体；非法字符保留；其余解码"""
    from qmds.modules.web.services.site_uploader import _decode_char_refs

    # 常规字符（中文、• ’ 等）解码为原始字符（ERP 字符串比较列名需要）
    assert _decode_char_refs("&#20013;&#25991;") == "中文"
    assert _decode_char_refs("&#8226;&#8217;") == "•’"
    assert _decode_char_refs("&#x4E2D;") == "中"  # 十六进制形式
    # CR 保留引用形态（字面 CR 会被 XML 解析器归一化为 LF，数据失真）
    assert _decode_char_refs("a&#13;\nb") == "a&#13;\nb"
    assert _decode_char_refs("a&#x0D;b") == "a&#x0D;b"
    # & < > 转标准命名实体（写字面会破坏 XML 结构）
    assert _decode_char_refs("&#38;") == "&amp;"
    assert _decode_char_refs("&#60;") == "&lt;"
    assert _decode_char_refs("&#62;") == "&gt;"
    # XML 1.0 非法字符（控制字符）保留引用形态
    assert _decode_char_refs("&#1;") == "&#1;"
    # 命名实体不属于数字引用，原样保留
    assert _decode_char_refs("&amp;&#38;") == "&amp;&amp;"


def test_convert_crlf_text(workdir):
    r"""描述含 \r\n 行尾：转换成功且 CR 精确保留（真实数据回归）

    openpyxl 把单元格文本中的 CR 写成 &#13; 引用；旧逻辑把 &#13; 解码为
    字面 CR 写入 sharedStrings，被 XML 解析器归一化为 LF，转换前后数据
    不一致（描述 \r\n 变 \n），触发「转换后数据校验不一致」放弃替换，
    未转换的内联格式上传 ERP 即「表错了」。
    """
    desc = "Line1\r\nLine2\r\n\r\nBullet • item"
    df = pd.DataFrame([
        {"SKU": "S1", "描述": desc, "自定义分类": "五金"},
        {"SKU": "S2", "描述": "\r\n前导回车\r\n", "自定义分类": "五金"},
    ])
    fp = write_table(workdir / "t.xlsx", df)
    # 前置确认：openpyxl 写出的内联形态确实含 &#13; 引用
    with zipfile.ZipFile(fp) as z:
        sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "&#13;" in sheet_xml, "测试前置失败：应含 CR 字符引用"
    before = pd.read_excel(fp, engine="openpyxl")

    assert make_erp_compatible(fp) is True

    after = pd.read_excel(fp, engine="openpyxl")
    assert after.equals(before)
    # CR 精确保留（未被归一化为 LF），前导/末尾 CR 同样保留
    assert after["描述"].iloc[0] == desc
    assert after["描述"].iloc[1] == "\r\n前导回车\r\n"
    # sharedStrings 中 CR 以合法引用形态存在；中文列名为原始字符
    with zipfile.ZipFile(fp) as z:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8")
    assert "&#13;" in ss
    assert "自定义分类" in ss
    # 幂等：再次转换不再修改（仅含 &#13; 引用不触发重写）
    data = fp.read_bytes()
    assert make_erp_compatible(fp) is False
    assert fp.read_bytes() == data
