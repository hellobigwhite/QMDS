# -*- coding: utf-8 -*-
"""原站域名统计（域名统计.xlsx）单元测试

覆盖：单表域名流式统计、跨表聚合（主数据/补充数据拆分）、
写出 域名统计.xlsx（两个 Sheet）、无「原站域名」列时跳过、
统计结果表不会被再次当成数据表统计。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services.category_stats import (
    DOMAIN_STATS_FILE_NAME,
    aggregate_folder_domains,
    collect_stats_files,
    count_file_domains,
    generate_domain_stats,
)


@pytest.fixture
def workdir():
    d = Path(".tmp") / f"domain_stats_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def write_table(path, rows, with_domain=True):
    """rows: [(分类, 原站域名, 条数)]"""
    data = []
    for i, (cat, dom, n) in enumerate(rows):
        for j in range(n):
            row = {"SKU": f"S{i}-{j}", "Categories": cat}
            if with_domain:
                row["原站域名"] = dom
            data.append(row)
    columns = ["SKU", "Categories"] + (["原站域名"] if with_domain else [])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(data, columns=columns).to_excel(path, index=False, engine="openpyxl")


def test_count_file_domains(workdir):
    fp = workdir / "t.xlsx"
    write_table(fp, [("Cat A", "a.com", 3), ("Cat A", "b.com", 2), ("Cat B", "a.com", 1)])
    counts, total, empty, dom_cats = count_file_domains(fp)
    assert total == 6
    assert empty == 0
    assert counts == {"a.com": 4, "b.com": 2}
    assert dom_cats["a.com"] == {"Cat A", "Cat B"}
    assert dom_cats["b.com"] == {"Cat A"}

    # 空域名行单独计数
    fp2 = workdir / "t2.xlsx"
    write_table(fp2, [("Cat A", "", 2), ("Cat A", "a.com", 1)])
    counts2, total2, empty2, _ = count_file_domains(fp2)
    assert (total2, empty2, dict(counts2)) == (3, 2, {"a.com": 1})

    # 无「原站域名」列 -> ValueError
    fp3 = workdir / "t3.xlsx"
    write_table(fp3, [("Cat A", "a.com", 1)], with_domain=False)
    with pytest.raises(ValueError):
        count_file_domains(fp3)


def test_generate_domain_stats_writes_both_sheets(workdir):
    site = workdir / "Main_One"
    write_table(site / "mainMain_One.xlsx",
                [("Main One", "main.com", 100), ("Main One", "shared.com", 50)])
    write_table(site / "Main_One_supp.xlsx",
                [("cat A", "shared.com", 200), ("cat B", "other.com", 300)])
    write_table(site / "Main_One_supp2.xlsx", [("cat C", "other.com", 50)])

    info = generate_domain_stats(site)
    assert info is not None
    assert info["domains"] == 3
    assert info["rows"] == 700
    assert (site / DOMAIN_STATS_FILE_NAME).exists()

    df = pd.read_excel(site / DOMAIN_STATS_FILE_NAME, sheet_name="域名统计",
                       engine="openpyxl")
    assert list(df.columns) == ["原站域名", "商品数", "占比(%)", "主数据条数",
                                "补充数据条数", "涉及分类数"]
    assert df["商品数"].sum() == 700
    by_dom = {r["原站域名"]: r for _, r in df.iterrows()}
    assert by_dom["other.com"]["商品数"] == 350
    assert by_dom["other.com"]["补充数据条数"] == 350   # 全部来自补充表
    assert by_dom["other.com"]["主数据条数"] == 0
    assert by_dom["main.com"]["主数据条数"] == 100
    assert by_dom["shared.com"]["商品数"] == 250        # 主 50 + 补充 200
    assert by_dom["shared.com"]["涉及分类数"] == 2
    # 按商品数降序
    assert list(df["原站域名"]) == ["other.com", "shared.com", "main.com"]

    summary = pd.read_excel(site / DOMAIN_STATS_FILE_NAME, sheet_name="汇总",
                            engine="openpyxl")
    kv = {str(r["指标"]): r["值"] for _, r in summary.iterrows()}
    assert kv["数据文件夹"] == "Main_One"
    assert int(kv["原站域名总数"]) == 3
    assert int(kv["商品总数"]) == 700
    assert "other.com" in str(kv["商品数最多的域名"])


def test_generate_domain_stats_skips_without_domain_column(workdir):
    site = workdir / "Main_Two"
    write_table(site / "mainMain_Two.xlsx", [("Main Two", "x.com", 10)],
                with_domain=False)
    assert generate_domain_stats(site) is None
    assert not (site / DOMAIN_STATS_FILE_NAME).exists()

    # 空文件夹同样跳过
    empty = workdir / "Empty"
    empty.mkdir(parents=True, exist_ok=True)
    assert generate_domain_stats(empty) is None


def test_stats_files_are_not_rescanned(workdir):
    """域名统计.xlsx / 分类统计.xlsx 不会被当成数据表再次统计"""
    site = workdir / "Site"
    write_table(site / "mainSite.xlsx", [("Main", "a.com", 5)])
    generate_domain_stats(site)
    assert (site / DOMAIN_STATS_FILE_NAME).exists()

    files = [p.name for p in collect_stats_files(site)]
    assert files == ["mainSite.xlsx"], files

    # 再跑一次结果不变（不会把上一次的统计表当数据）
    info = generate_domain_stats(site)
    assert info["rows"] == 5
    assert info["domains"] == 1

    agg = aggregate_folder_domains([site / "mainSite.xlsx"])
    assert sum(agg["counts"].values()) == 5
