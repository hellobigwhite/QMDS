# -*- coding: utf-8 -*-
"""数据分配服务（data_allocator）单元测试

覆盖：大分类（> 拆分阈值）按比例拆分到各补充表、小分类整类保留、
方案计算与文件写出（行级不重不漏）。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.modules.web.services.data_allocator import (
    count_excel_categories,
    detect_category_column,
    plan_allocation,
    run_allocation_task,
    sanitize_filename,
)

EXPORT_COLUMNS = ["SKU", "Name", "Description", "Regular price", "Categories",
                  "Images", "cf_opingts", "自定义分类", "原站域名", "分布网站识别", "语言"]


@pytest.fixture
def workdir():
    """临时工作目录（默认权限 mkdir，避免受限环境下目录不可访问）"""
    d = Path(".tmp") / f"alloc_test_{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_df(categories):
    """按 [分类名 x 数量] 生成测试数据"""
    rows = []
    for i, cat in enumerate(categories):
        for j in range(cat[1]):
            rows.append({"SKU": f"S{i}-{j}", "Name": f"P{i}-{j}",
                         "Description": "d", "Regular price": 9.9,
                         "Categories": cat[0], "Images": "", "cf_opingts": "",
                         "自定义分类": "", "原站域名": "example.com",
                         "分布网站识别": 0, "语言": "en"})
    return pd.DataFrame(rows, columns=EXPORT_COLUMNS)


# ── 文件名清理 ─────────────────────────────

def test_sanitize_filename():
    assert sanitize_filename("Toilet Tank") == "Toilet_Tank"
    assert sanitize_filename("Hardware|||Plumbing & Fittings") == "Hardware_Plumbing_&_Fittings"
    assert sanitize_filename('a?*<>:"/\\|b') == "a_b"
    assert sanitize_filename("  spaces   inside  ") == "spaces_inside"
    assert sanitize_filename("") == "Unnamed"
    assert sanitize_filename("2-piece Toilets - Toilet Tanks") == "2-piece_Toilets_-_Toilet_Tanks"


# ── 分类列检测 ─────────────────────────────

def test_detect_category_column():
    assert detect_category_column(EXPORT_COLUMNS) == "Categories"
    assert detect_category_column(["SKU", "分类"]) == "分类"
    assert detect_category_column(["SKU", "category"]) == "category"
    assert detect_category_column(["a", "CATEGORIES"]) == "CATEGORIES"
    assert detect_category_column(["SKU", "Name"]) is None


# ── 分配方案计算 ─────────────────────────────

def test_plan_allocation_basic():
    """实际场景：约 25 万行剩余，5 个主分类，大分类拆分 + 小分类整类"""
    counts = {"Hardware": 43486, "bathroom supplies": 17496,
              "Hardware|||Plumbing & Fittings": 11429,
              "Plumbing|||Sanitary Ware & Fittings": 5846}
    # 2000 个小分类填充剩余数据（共 17 万，剩余约 24.8 万 -> 5 份 x ~4.97 万）
    for i in range(2000):
        counts[f"small {i}"] = 85
    mains = ["Toilet Tank", "Toilet Tank Lid", "2-piece Toilets - Toilet Tanks",
             "Toilet Tank Lid - Reproduction", "American Standard Toilet Tank"]
    for m in mains:
        counts[m] = 500

    plan = plan_allocation(counts, mains, 40000, 50000)

    assert len(plan["main_tables"]) == 5
    assert sum(t["count"] for t in plan["main_tables"]) == 2500
    assert plan["remaining_total"] == sum(counts.values()) - 2500
    # 剩余约 24.8 万 -> 5 份，每份约 4.97 万
    assert plan["portion_count"] == 5
    assert 40000 <= plan["target_size"] <= 50000
    for p in plan["portions"]:
        assert 40000 <= p["total"] <= 50000

    # 大分类（> 3000）必须拆分到每一份，分片之和等于原数量
    split_cats = {"Hardware": 43486, "bathroom supplies": 17496,
                  "Hardware|||Plumbing & Fittings": 11429,
                  "Plumbing|||Sanitary Ware & Fittings": 5846}
    assert {s["category"] for s in plan["split_categories"]} == set(split_cats)
    for cat, cnt in split_cats.items():
        frags = [p["categories"][cat] for p in plan["portions"] if cat in p["categories"]]
        assert len(frags) == 5, f"{cat} 应拆分到全部 5 份"
        assert sum(frags) == cnt
        # 分片尽量均匀：任意两份相差不超过 1
        assert max(frags) - min(frags) <= 1

    # 小分类（<= 3000）整类保留，只出现在一份中
    small_assigned = [c for p in plan["portions"] for c in p["categories"]
                      if c.startswith("small ")]
    assert len(small_assigned) == 2000
    assert len(set(small_assigned)) == 2000

    # 总量守恒
    assert sum(p["total"] for p in plan["portions"]) == plan["remaining_total"]
    assert plan["warnings"] == []


def test_plan_allocation_extra_portions_when_too_much_data():
    """剩余数据多于 主分类数 x 最大值 时，增加额外补充份数；全部大分类拆分"""
    counts = {f"cat{i}": 10000 for i in range(30)}  # 30 万剩余（全部 > 3000，拆分）
    counts["Main A"] = 100
    counts["Main B"] = 200
    plan = plan_allocation(counts, ["Main A", "Main B"], 40000, 50000)

    # 300000 / 50000 = 6 份 > 2 个主分类
    assert plan["portion_count"] == 6
    for p in plan["portions"]:
        assert 49990 <= p["total"] <= 50010
    # 每个大分类拆分到全部 6 份
    assert len(plan["split_categories"]) == 30
    for i in range(30):
        frags = [p["categories"][f"cat{i}"] for p in plan["portions"]]
        assert len(frags) == 6
        assert sum(frags) == 10000
    assert sum(p["total"] for p in plan["portions"]) == 300000
    assert not any("超过" in w for w in plan["warnings"])


def test_plan_allocation_insufficient_data():
    """剩余数据不足时仍保证每个主分类一份补充，并给出告警"""
    counts = {"Main A": 100, "Main B": 200, "Main C": 300, "other": 90000}
    plan = plan_allocation(counts, ["Main A", "Main B", "Main C"], 40000, 50000)

    assert plan["portion_count"] == 3
    assert plan["target_size"] == 30000
    # other 90000 拆分为 3 份 x 30000
    frags = [p["categories"]["other"] for p in plan["portions"]]
    assert frags == [30000, 30000, 30000]
    assert any("低于最少目标" in w for w in plan["warnings"])


def test_plan_allocation_splits_oversize_category():
    """单个分类超过每份上限时：拆分到多份，不产生超限份"""
    counts = {"Main A": 100, "Huge Cat": 60000, "small": 1000}
    plan = plan_allocation(counts, ["Main A"], 40000, 50000)

    # 61000 / 50000 -> 2 份
    assert plan["portion_count"] == 2
    # Huge Cat 拆分到 2 份（30000 + 30000），任何一份都不超 50000
    frags = [p["categories"]["Huge Cat"] for p in plan["portions"]]
    assert len(frags) == 2
    assert sum(frags) == 60000
    for p in plan["portions"]:
        assert p["total"] <= 50000
    assert not any("超过" in w for w in plan["warnings"])
    # 每份约 30500 < 40000 -> 提示数据偏少
    assert any("低于最少目标" in w for w in plan["warnings"])


def test_plan_allocation_split_disabled_by_large_threshold():
    """拆分阈值极大时退化为整类装箱：大分类整类保留并触发超限告警"""
    counts = {"Main A": 100, "Huge Cat": 60000, "small": 1000}
    plan = plan_allocation(counts, ["Main A"], 40000, 50000, split_threshold=100000)

    assert plan["split_categories"] == []
    whole = [p for p in plan["portions"] if "Huge Cat" in p["categories"]]
    assert len(whole) == 1
    assert whole[0]["categories"]["Huge Cat"] == 60000
    assert any("超过" in w for w in plan["warnings"])


def test_plan_allocation_split_threshold_boundary():
    """阈值边界：= 阈值整类保留，> 阈值拆分"""
    counts = {"M": 100, "A": 3000, "B": 3001, "S": 1000}
    plan = plan_allocation(counts, ["M"], 1000, 4500)

    # A(3000) == 阈值 -> 整类；B(3001) > 阈值 -> 拆分为 2 份
    a_in = [p for p in plan["portions"] if "A" in p["categories"]]
    assert len(a_in) == 1
    assert a_in[0]["categories"]["A"] == 3000
    b_frags = [p["categories"]["B"] for p in plan["portions"]]
    assert len(b_frags) == 2
    assert sum(b_frags) == 3001
    assert {s["category"] for s in plan["split_categories"]} == {"B"}
    # S 整类保留
    s_in = [p for p in plan["portions"] if "S" in p["categories"]]
    assert len(s_in) == 1
    assert sum(p["total"] for p in plan["portions"]) == 7001


def test_plan_allocation_zero_threshold():
    """阈值为 0：所有分类都拆分"""
    counts = {"M": 100, "A": 500, "B": 700}
    plan = plan_allocation(counts, ["M"], 400, 700, split_threshold=0)

    assert plan["portion_count"] == 2
    for cat, cnt in (("A", 500), ("B", 700)):
        frags = [p["categories"][cat] for p in plan["portions"]]
        assert len(frags) == 2
        assert sum(frags) == cnt
    assert {s["category"] for s in plan["split_categories"]} == {"B", "A"}


def test_plan_allocation_no_remaining():
    """全部数据都属于主分类时，不生成补充表"""
    counts = {"Main A": 100, "Main B": 200}
    plan = plan_allocation(counts, ["Main A", "Main B"], 40000, 50000)

    assert plan["portion_count"] == 0
    assert plan["portions"] == []
    assert any("没有剩余数据" in w for w in plan["warnings"])


def test_plan_allocation_invalid_mains():
    counts = {"A": 100}
    with pytest.raises(ValueError):
        plan_allocation(counts, ["不存在的分类"], 40000, 50000)


def test_plan_allocation_skips_unknown_main_with_warning():
    counts = {"A": 100, "B": 50000}
    plan = plan_allocation(counts, ["A", "不存在"], 40000, 50000)
    assert len(plan["main_tables"]) == 1
    assert any("不存在" in w for w in plan["warnings"])


def test_plan_allocation_empty_category_in_remaining():
    """空分类（分类列为空的行）参与补充分配，不能作为主分类"""
    counts = {"Main A": 100, "": 45000, "B": 5000}
    plan = plan_allocation(counts, ["Main A", ""], 40000, 50000)
    assert len(plan["main_tables"]) == 1
    assert "" in plan["portions"][0]["categories"]


def test_plan_allocation_empty_portions_filtered():
    """剩余数据极少时过滤空份，不生成空补充表"""
    counts = {"Main A": 100, "Main B": 200, "Main C": 300, "only": 500}
    plan = plan_allocation(counts, ["Main A", "Main B", "Main C"], 40000, 50000)

    # 3 个主分类 -> 3 份，但只有 1 个分类 500 条 -> 仅 1 份非空
    assert plan["portion_count"] == 1
    assert plan["portions"][0]["total"] == 500
    assert any("低于最少目标" in w for w in plan["warnings"])


# ── Excel 读取与完整任务 ─────────────────────────────

def test_count_excel_categories(workdir):
    df = make_df([("Cat A", 5), ("Cat B", 3), ("", 2)])
    fp = workdir / "t.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    col, total, cats = count_excel_categories(fp)
    assert col == "Categories"
    assert total == 10
    assert cats == [("Cat A", 5), ("Cat B", 3)]  # 空分类不进列表，按数量降序


def test_run_allocation_task(workdir):
    """端到端：2 个主分类 + 1 个大分类（拆分）+ 30 个小分类（整类）

    min/max 缩小为 1500~2000，拆分阈值 1000 模拟真实比例。
    """
    from qmds.modules.web.task_manager import task_manager

    cats = [("Main One", 120), ("Main|||Two", 80), ("Big Cat", 3400)]
    cats += [(f"cat{i}", 100) for i in range(30)]  # 3000 条小分类
    df = make_df(cats)
    fp = workdir / "export_test.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["Main One", "Main|||Two"], 1500, 2000, 1000)

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)

    out_dirs = [d for d in workdir.iterdir() if d.is_dir()]
    assert len(out_dirs) == 1
    out_dir = out_dirs[0]
    assert out_dir.name.startswith("export_test_分配_")

    # 结构: 每个主分类一个数据文件夹（主数据 + 补充数据同文件夹），额外补充 extra{N}
    folders = {d.name for d in out_dir.iterdir() if d.is_dir()}
    assert folders == {"Main_One", "Main_Two", "extra1", "extra2"}, folders
    assert list(out_dir.glob("*.xlsx")) == []

    # 主数据表: main 前缀命名，只含对应分类
    main1 = pd.read_excel(out_dir / "Main_One" / "mainMain_One.xlsx", engine="openpyxl")
    assert len(main1) == 120
    assert (main1["Categories"] == "Main One").all()
    main2 = pd.read_excel(out_dir / "Main_Two" / "mainMain_Two.xlsx", engine="openpyxl")
    assert len(main2) == 80
    assert (main2["Categories"] == "Main|||Two").all()

    # 补充数据表: 与主数据同文件夹，命名不含中文
    supp_files = [out_dir / "Main_One" / "Main_One_supp.xlsx",
                  out_dir / "Main_Two" / "Main_Two_supp.xlsx",
                  out_dir / "extra1" / "extra1.xlsx",
                  out_dir / "extra2" / "extra2.xlsx"]
    supp_sizes = []
    total_rows = len(main1) + len(main2)
    seen = set()
    for sub in (main1, main2):
        for sku in sub["SKU"]:
            assert sku not in seen
            seen.add(sku)
    for p in supp_files:
        assert p.exists(), p
        # 命名不含中文
        assert not any("一" <= ch <= "鿿" for ch in p.stem), p.stem
        sub = pd.read_excel(p, engine="openpyxl")
        total_rows += len(sub)
        for sku in sub["SKU"]:
            assert sku not in seen  # 数据不重复
            seen.add(sku)
        supp_sizes.append(len(sub))
        # 补充表不含主分类
        assert not sub["Categories"].isin(["Main One", "Main|||Two"]).any()
        # Big Cat(3400 > 1000) 拆分：每份恰好 850 条
        assert int((sub["Categories"] == "Big Cat").sum()) == 850
        # 每份在 1500~2000 范围内
        assert 1500 <= len(sub) <= 2000
    assert total_rows == len(df)  # 数据不丢失
    assert sum(supp_sizes) == 3400 + 3000
    assert sorted(supp_sizes) == [1550, 1550, 1650, 1650]


def test_run_allocation_task_with_split(workdir):
    """分配后批量拆表：主数据不设上限（整表一份），补充数据按行数拆分"""
    from qmds.modules.web.task_manager import task_manager

    cats = [("Main One", 120), ("Main|||Two", 80), ("Big Cat", 3400)]
    cats += [(f"cat{i}", 100) for i in range(30)]  # 3000 条小分类
    df = make_df(cats)
    fp = workdir / "export_split.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc_split"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["Main One", "Main|||Two"], 1500, 2000, 1000,
                        split_options={"enabled": True,
                                       "supp_rows_per_file": 700,
                                       "suffix_mode": "part",
                                       "remove_source": True})

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)

    out_dir = next(d for d in workdir.iterdir() if d.is_dir()
                   and d.name.startswith("export_split_分配_"))

    # 拆分后源表格被删除；主数据与补充数据的分卷都在同一分类文件夹内
    xlsx_left = list(out_dir.glob("*.xlsx"))
    assert xlsx_left == [], xlsx_left
    folders = {d.name for d in out_dir.iterdir() if d.is_dir()}
    assert folders == {"Main_One", "Main_Two", "extra1", "extra2"}, folders

    import re as _re

    # Main_One 文件夹: 主数据 1 份（不设上限）+ 补充数据 3 份
    main_one_files = list((out_dir / "Main_One").glob("*.xlsx"))
    main_parts = [p for p in main_one_files if p.name.startswith("main")]
    assert len(main_parts) == 1
    assert main_parts[0].name.startswith("mainMain_One_part")
    df1 = pd.read_excel(main_parts[0], engine="openpyxl")
    assert len(df1) == 120
    # 后缀模式 part: 数据行添加 _part1 后缀
    assert (df1["原站域名"] == "example.com_part1").all()

    supp_parts = [p for p in main_one_files if "_supp_" in p.name]
    assert len(supp_parts) == 3
    sizes = [len(pd.read_excel(p, engine="openpyxl")) for p in supp_parts]
    assert sorted(sizes, reverse=True) == [700, 700, 250]
    # 后缀 part1..part3 的原站域名
    for p in supp_parts:
        m = _re.search(r"_part(\d+)_", p.name)
        dfp = pd.read_excel(p, engine="openpyxl")
        expected_suffix = f"example.com_part{m.group(1)}"
        assert (dfp["原站域名"] == expected_suffix).all()

    # 额外补充文件夹: extra{N}_part{M}，命名不含中文
    extra_files = list((out_dir / "extra1").glob("*.xlsx"))
    assert len(extra_files) == 3
    assert all(p.name.startswith("extra1_part") for p in extra_files)
    for p in extra_files:
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in p.stem), p.stem

    # 全部数据不重不漏（分类统计.xlsx 是统计文件，非数据表，跳过）
    all_skus = []
    for d in out_dir.iterdir():
        if d.is_dir():
            for p in d.glob("*.xlsx"):
                if p.name == "分类统计.xlsx":
                    continue
                all_skus.extend(pd.read_excel(p, engine="openpyxl")["SKU"])
    assert len(all_skus) == len(df)
    assert len(set(all_skus)) == len(df)


def test_run_allocation_task_split_disabled(workdir):
    """未启用拆表（enabled=False）：主数据与补充数据同文件夹，无分卷"""
    from qmds.modules.web.task_manager import task_manager

    df = make_df([("Main One", 50), ("other", 100)])
    fp = workdir / "no_split.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc_nosplit"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["Main One"], 40, 100,
                        split_options={"enabled": False})

    task = task_manager.get(task_id)
    assert task["status"] == "completed"
    out_dir = next(d for d in workdir.iterdir() if d.is_dir())
    # 每个主分类一个文件夹: main 前缀主数据 + _supp 补充数据 + 分类统计
    folders = {d.name for d in out_dir.iterdir() if d.is_dir()}
    assert folders == {"Main_One"}, folders
    assert list(out_dir.glob("*.xlsx")) == []
    names = sorted(p.name for p in (out_dir / "Main_One").glob("*.xlsx"))
    # 分配完成后自动生成网站分类统计（默认开启）
    assert names == ["Main_One_supp.xlsx", "mainMain_One.xlsx", "分类统计.xlsx"], names
    assert not any(d.is_dir() for d in (out_dir / "Main_One").iterdir())


def test_run_allocation_task_all_main(workdir):
    """全部勾选为主分类：只生成主数据表"""
    from qmds.modules.web.task_manager import task_manager

    df = make_df([("Only A", 10), ("Only B", 20)])
    fp = workdir / "all_main.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc_all"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["Only A", "Only B"], 40000, 50000)

    task = task_manager.get(task_id)
    assert task["status"] == "completed"
    out_dir = next(d for d in workdir.iterdir() if d.is_dir())
    folders = {d.name for d in out_dir.iterdir() if d.is_dir()}
    assert folders == {"Only_A", "Only_B"}, folders
    assert (out_dir / "Only_A" / "mainOnly_A.xlsx").exists()
    assert (out_dir / "Only_B" / "mainOnly_B.xlsx").exists()


def test_run_allocation_task_invalid_file(workdir):
    """无分类列的表格 -> 任务失败"""
    from qmds.modules.web.task_manager import task_manager

    df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
    fp = workdir / "bad.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc_bad"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["A"], 40000, 50000)

    assert task_manager.get(task_id)["status"] == "failed"


# ── 分配后自动网站分类统计 ─────────────────────

def _read_stats(path):
    """读取分类统计.xlsx（复用 category_stats 的读取器），返回 {分类: 产品数}"""
    from qmds.modules.web.services.category_stats import read_stats_excel

    result = read_stats_excel(path)
    counts = {c["category"]: c["count"] for c in result["categories"]}
    return counts, result["summary"]


def test_run_allocation_task_generates_stats(workdir):
    """分配完成后自动生成各网站（主分类文件夹）的分类统计.xlsx

    每个主分类文件夹一份统计（主数据 + 补充数据合并计数），
    extra 文件夹不属于网站不生成统计。
    """
    from qmds.modules.web.task_manager import task_manager

    cats = [("Main One", 120), ("Main|||Two", 80)]
    cats += [(f"cat{i}", 100) for i in range(10)]  # 1000 条剩余
    df = make_df(cats)
    fp = workdir / "export_stats.xlsx"
    df.to_excel(fp, index=False, engine="openpyxl")

    task_id = "test_alloc_stats"
    task_manager.create(task_id, "data_allocate", "test")
    run_allocation_task(task_id, fp, ["Main One", "Main|||Two"], 500, 600, 1000)

    task = task_manager.get(task_id)
    assert task["status"] == "completed", task_manager.get_logs(task_id)
    assert "网站分类统计" in task["message"], task["message"]

    out_dir = next(d for d in workdir.iterdir() if d.is_dir())
    # 各网站文件夹（主分类）都有统计文件
    stats1 = out_dir / "Main_One" / "分类统计.xlsx"
    stats2 = out_dir / "Main_Two" / "分类统计.xlsx"
    assert stats1.exists(), list((out_dir / "Main_One").iterdir())
    assert stats2.exists(), list((out_dir / "Main_Two").iterdir())

    # Main_One = 主数据 120 + 补充 500（10 个小分类整类分配，不拆散）
    counts1, summary1 = _read_stats(stats1)
    assert sum(counts1.values()) == 120 + 500, sum(counts1.values())
    assert counts1.get("Main One") == 120
    small = {c: n for c, n in counts1.items() if c.startswith("cat")}
    assert sum(small.values()) == 500
    assert all(n == 100 for n in small.values())
    # 汇总指向正确的文件夹
    assert summary1["数据文件夹"] == "Main_One"

    # Main_Two = 主数据 80 + 剩余 500
    counts2, summary2 = _read_stats(stats2)
    assert sum(counts2.values()) == 80 + 500, sum(counts2.values())
    assert counts2.get("Main|||Two") == 80
    assert summary2["数据文件夹"] == "Main_Two"

    # extra 文件夹（未绑定主分类，不属于网站）不生成统计
    extra_dirs = [d for d in out_dir.iterdir()
                  if d.is_dir() and d.name.startswith("extra")]
    for d in extra_dirs:
        assert not (d / "分类统计.xlsx").exists()

    # 日志包含统计完成记录
    logs = [e.get("message", "") for e in task_manager.get_logs(task_id)]
    assert any("网站分类统计完成: Main_One" in m for m in logs)
