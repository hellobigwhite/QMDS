# -*- coding: utf-8 -*-
"""超长路径（>260 字符）支持测试 — winpath 工具与数据分配/拆表/收集链路

复刻线上故障：数据分配输出的分卷文件路径超过 Windows MAX_PATH(260) 时
open()/openpyxl 报 [Errno 2] No such file or directory（数据分配任务失败），
Path.is_file/rglob 也会静默漏掉这些文件。所有文件 I/O 必须走
qmds.utils.winpath 的扩展长度前缀（\\?\）。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services.data_allocator import run_allocation_task
from qmds.modules.web.services.excel_splitter import split_excel_file
from qmds.modules.web.services.site_uploader import (
    collect_site_tables,
    collect_xlsx_files,
)
from qmds.utils import winpath

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]

# 线上实际出问题的分类名（多级类目拼接，120+ 字符）
LONG_CATEGORY = ("Pet_Bowls_&_Feeders_Pet_Bowls,_Feeders_&_Waterers"
                 "_Extended_Test_Category_Name_Padding_Padding_Padding")

WIN_ONLY = pytest.mark.skipif(winpath.os.name != "nt",
                              reason="长路径前缀仅 Windows 需要")


@pytest.fixture
def workdir():
    """临时工作目录"""
    d = Path(".tmp") / f"longpath_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_df(categories):
    rows = []
    for i, cat in enumerate(categories):
        for j in range(cat[1]):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": cat[0], "Images": "", "cf_opingts": "",
                         "自定义分类": "", "原站域名": "example.com",
                         "分布网站识别": 0, "语言": "en"})
    return pd.DataFrame(rows, columns=EXPORT_COLUMNS)


def deep_dataset_dir(workdir) -> tuple[Path, Path]:
    """构造与线上同构的深层导出目录

    exports/{日期}/{数据集}/{数据集}_{id}_分配_{时间戳}/{长分类名}/
    返回 (数据集目录, 分配输出目录)。分类名保证分卷文件路径 >260 字符。
    """
    ds_dir = workdir / "exports" / "20260911" / "animals_pet_supplies__pet_food"
    ds_dir.mkdir(parents=True, exist_ok=True)
    alloc_dir = ds_dir / "animals_pet_supplies__pet_food_330148_分配_20260911_110754"
    return ds_dir, alloc_dir


# ── winpath 工具 ─────────────────────────────

@WIN_ONLY
def test_long_path_prefix_conversion(workdir):
    """绝对路径加扩展长度前缀；UNC 转UNC 前缀；已带前缀不重复加"""
    p = workdir / "a.xlsx"
    lp = winpath.long_path(p)
    assert lp.startswith("\\\\?\\")
    assert winpath.long_path(lp) == lp  # 幂等
    # normal_path 去前缀还原
    assert Path(winpath.normal_path(lp)) == Path(p).resolve()
    # UNC
    unc = winpath.long_path("\\\\server\\share\\f.xlsx")
    assert unc == "\\\\?\\UNC\\server\\share\\f.xlsx"


@WIN_ONLY
def test_winpath_file_ops_long_path(workdir):
    """超长路径下的 makedirs / is_file / remove 往返"""
    ds_dir, alloc_dir = deep_dataset_dir(workdir)
    folder = alloc_dir / LONG_CATEGORY
    target = folder / f"main{LONG_CATEGORY}_part1_MG1789096144.xlsx"
    assert len(str(target.resolve())) > 260  # 测试前提：确实超长

    winpath.makedirs(folder)
    pd.DataFrame({"a": [1, 2]}).to_excel(winpath.long_path(target),
                                         index=False, engine="openpyxl")
    # Path.is_file 对超长路径静默返回 False —— 必须用 winpath
    assert not target.is_file()
    assert winpath.is_file(target)

    df = pd.read_excel(winpath.long_path(target))
    assert df["a"].tolist() == [1, 2]

    winpath.remove(target)
    assert not winpath.exists(target)


# ── 拆表（线上故障的直接位置） ─────────────────────

@WIN_ONLY
def test_split_excel_file_long_output_path(workdir):
    """拆表输出到超长路径分类文件夹：分卷保存成功且可读回

    线上现象：main{分类}_part1_XX{时间戳}.xlsx 路径 269 字符时
    wb.save 抛 [Errno 2] No such file or directory。
    """
    ds_dir, alloc_dir = deep_dataset_dir(workdir)
    src = ds_dir / "animals_pet_supplies__pet_food_330148.xlsx"
    df = make_df([(LONG_CATEGORY, 30), ("Other Cat", 10)])
    df.to_excel(src, index=False, engine="openpyxl")

    result = split_excel_file(src, rows_per_file=None,
                              output_folder=alloc_dir / LONG_CATEGORY,
                              remove_source=False)
    assert len(result["parts"]) == 1
    part = result["parts"][0]
    assert len(str(part.resolve())) > 260
    assert winpath.is_file(part)
    back = pd.read_excel(winpath.long_path(part))
    assert len(back) == 40  # 30 + 10


# ── 数据分配任务端到端（线上故障的完整链路） ─────────────────

@WIN_ONLY
def test_run_allocation_task_long_category(workdir):
    """长分类名 + 深层目录 + 启用拆表：分配任务成功完成

    覆盖主/补充表写出、批量拆表、分类统计 —— 全部在 >260 路径下执行。
    """
    from qmds.modules.web.task_manager import task_manager

    ds_dir, _ = deep_dataset_dir(workdir)
    src = ds_dir / "animals_pet_supplies__pet_food_330148.xlsx"
    df = make_df([(LONG_CATEGORY, 40), ("Other Cat", 20)])
    df.to_excel(src, index=False, engine="openpyxl")

    task_id = f"longpath_alloc_{uuid.uuid4().hex[:6]}"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(
        task_id, src, [LONG_CATEGORY, "Other Cat"], 10, 20, 1000,
        {"enabled": True, "supp_rows_per_file": 10,
         "suffix_mode": "none", "remove_source": True})

    task = task_manager.get(task_id)
    assert task["status"] == "completed", [
        e.get("message") for e in task_manager.get_logs(task_id)]

    # 分配输出目录：主分类文件夹 + Other_Cat 文件夹
    alloc_dir = ds_dir / [d.name for d in ds_dir.iterdir()
                          if d.is_dir() and "分配" in d.name][0]
    cat_folder = alloc_dir / LONG_CATEGORY
    assert winpath.is_dir(cat_folder)

    # 主数据分卷（main 前缀）存在且可读
    parts = [p for p in collect_xlsx_files(alloc_dir)
             if p.parent == cat_folder and "main" in p.name]
    assert parts, f"未找到主数据分卷: {list(collect_xlsx_files(alloc_dir))}"
    part = parts[0]
    assert len(str(part.resolve())) > 260
    assert winpath.is_file(part)
    main_df = pd.read_excel(winpath.long_path(part))
    assert len(main_df) == 40

    # 分类统计已生成（同样在超长路径下写出）
    stats_file = cat_folder / "分类统计.xlsx"
    assert winpath.is_file(stats_file)

    # 拆表后中间表（main{分类}.xlsx）按配置删除，只留 _partN_ 分卷
    assert not winpath.exists(cat_folder / f"main{LONG_CATEGORY}.xlsx")
    # 源导出文件保留（remove_source 只删拆表中间表）
    assert winpath.exists(src)


@WIN_ONLY
def test_collect_site_tables_long_path(workdir):
    """collect_site_tables 能枚举超长路径下的 data_ 前缀数据表"""
    ds_dir, alloc_dir = deep_dataset_dir(workdir)
    folder = alloc_dir / LONG_CATEGORY
    winpath.makedirs(folder)
    for n in ("data_mainX_part1_AB1.xlsx", "data_X_supp_part1_AB2.xlsx"):
        pd.DataFrame({"a": [1]}).to_excel(
            winpath.long_path(folder / n), index=False, engine="openpyxl")
    # 非 data_ 前缀文件不上传
    pd.DataFrame({"a": [1]}).to_excel(
        winpath.long_path(folder / "分类统计.xlsx"), index=False, engine="openpyxl")

    tables = collect_site_tables(folder)
    names = [p.name for p in tables]
    assert names == ["data_mainX_part1_AB1.xlsx", "data_X_supp_part1_AB2.xlsx"]
    for p in tables:
        assert len(str(p.resolve())) > 260
