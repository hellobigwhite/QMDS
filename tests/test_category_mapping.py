# -*- coding: utf-8 -*-
"""简化分类名 -> 中文一级分类名映射测试

回归背景：历史数据 source_category 为空格形式（"animals pet supplies"，
清洗流程曾把下划线替换为空格），导出时映射表（下划线键名）查不到，
自定义分类 列原样输出了英文串。ERP 站群按站点中文分类树校验该列，
未映射的英文值导致整表上传失败（分类错了）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.config.categories import (
    SHOPIFY_TO_CN_CATEGORY, get_cn_category_name)


def test_standard_underscore_names_map():
    """标准下划线简化名（映射表的键）正常映射"""
    assert get_cn_category_name("hardware") == "五金"
    assert get_cn_category_name("electronics") == "电子产品"
    assert get_cn_category_name("animals_pet_supplies") == "动物"


def test_spaced_names_map():
    """空格形式（历史清洗数据）同样映射成功 — 本次分类错了的根因"""
    assert get_cn_category_name("animals pet supplies") == "动物"
    assert get_cn_category_name("home  garden") == "家居与园艺"
    assert get_cn_category_name("food beverages tobacco") == "饮食"


def test_case_and_hyphen_variants_map():
    """大小写/连字符差异规范化后映射"""
    assert get_cn_category_name("Animals Pet Supplies") == "动物"
    assert get_cn_category_name("ANIMALS_PET_SUPPLIES") == "动物"
    assert get_cn_category_name("animals-pet supplies") == "动物"
    assert get_cn_category_name("  animals pet supplies  ") == "动物"


def test_unknown_returns_original():
    """未知分类返回传入值原样（保持原契约，不做部分改写）"""
    assert get_cn_category_name("foo bar") == "foo bar"
    assert get_cn_category_name("mystery_category") == "mystery_category"


def test_empty_input():
    """空值/空白原样返回（导出行构建按空值输出空串）"""
    assert get_cn_category_name("") == ""
    assert get_cn_category_name(None) is None
    assert get_cn_category_name("   ") == "   "


def test_all_mapping_keys_roundtrip():
    """映射表全部键的空格形式也能映射到相同中文值"""
    for key, cn in SHOPIFY_TO_CN_CATEGORY.items():
        assert get_cn_category_name(key) == cn
        assert get_cn_category_name(key.replace("_", " ")) == cn


def test_export_row_builds_cn_custom_category():
    """导出行构建：source_category 空格形式 -> 自定义分类 输出中文（端到端）"""
    from qmds.db.product_db import _build_export_row

    doc = {
        "SKU": "S1", "标题": "Product", "描述": "d", "原价": 9.9, "折扣价": 0,
        "分类": "Pet Supplies|||Dog Food", "图片": "", "变体": "",
        "source_category": "animals pet supplies",
        "source_domain": "example.com",
    }
    row = _build_export_row(doc)
    assert row["自定义分类"] == "动物"

    doc["source_category"] = "animals_pet_supplies"
    assert _build_export_row(doc)["自定义分类"] == "动物"

    # 未传 fallback_category 时保持旧契约（空值输出空串）
    doc["source_category"] = ""
    assert _build_export_row(doc)["自定义分类"] == ""


def test_export_row_uniform_collection_category():
    """导出行构建：自定义分类 = 集合大类（全表一致，一个网站只能有一个）

    自定义分类的语义是网站大类（ERP 站群分类树的中文名）。同一份导出
    数据分配出的网站，自定义分类 必须全表一致——一律取集合的一级分类，
    不按各行 source_category 分别映射（否则脏值/跨类值会混进网站数据，
    ERP 校验按站点分类树进行，混合值会导致整表上传失败）。
    """
    from qmds.db.product_db import _build_export_row

    doc = {
        "SKU": "S1", "标题": "Product", "描述": "d", "原价": 9.9, "折扣价": 0,
        "分类": "Pet Supplies|||Dog Food", "图片": "", "变体": "",
        "source_domain": "example.com",
    }

    # 无论 source_category 是什么（标准/未知/空/其他大类），集合大类说了算
    for src in ("animals pet supplies", "animals_pet_supplies", "mystery category",
                "", "hardware", "arts entertainment"):
        doc["source_category"] = src
        row = _build_export_row(doc, collection_category="animals_pet_supplies")
        assert row["自定义分类"] == "动物", src

    # 其他集合大类同样全表一致
    for src in ("hardware", "whatever", ""):
        doc["source_category"] = src
        assert _build_export_row(
            doc, collection_category="hardware")["自定义分类"] == "五金"
        assert _build_export_row(
            doc, collection_category="arts_entertainment")["自定义分类"] == "艺术与娱乐"
