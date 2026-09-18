# -*- coding: utf-8 -*-
"""成人类目清洗时跳过成人违禁词 — 回归测试

回归背景：清洗流程的违禁词表把色情类关键词（adult / lingerie / vibrator /
sex toy 等）与武器、药品等一起做无类目区分的子串匹配，
成人类目（mature / 成人）的商品标题天然包含这些词，
接入真实成人数据后会成片被误判为"违禁词"而 failed。
而站点级 AI 分类明确允许成人站（ai_classifier.py: "adult products (Mature
category) are NOT black-five"），即上游放行、下游全砍，前后矛盾。

修复：成人关键词单独成表 ADULT_KEYWORDS，清洗按一级分类取表
（get_prohibited_keywords），成人类目跳过成人词，其余违禁词照常生效。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.config.categories import is_adult_category
from qmds.modules.data_scraper.models.schemas import Product
from qmds.modules.data_scraper.pipeline.filters import (
    ADULT_KEYWORDS, NON_ADULT_KEYWORDS, PROHIBITED_KEYWORDS,
    ProductFilter, get_prohibited_keywords,
)


def _product(title: str, body_html: str = "", tags=None) -> Product:
    return Product(source_url="https://example.com/p/1", title=title,
                   price=29.99, body_html=body_html, tags=tags or [])


# ── is_adult_category ──────────────────────────────────

def test_adult_category_aliases():
    """标准名/旧别名/中文名/分类号都识别为成人类目"""
    for name in ["mature", "Mature", "MATURE", " adult ", "adult", "成人", "772", "13"]:
        assert is_adult_category(name), name


def test_non_adult_category_not_matched():
    """其他类目、空值不识别为成人类目"""
    for name in ["health_beauty", "apparel_accessories", "animals pet supplies",
                 "electronics", "", "   ", None, "maturity"]:
        assert not is_adult_category(name), name


# ── get_prohibited_keywords ────────────────────────────

def test_full_keyword_table_unchanged():
    """全量违禁词表仍包含成人词且顺序与拆分前一致（向后兼容）"""
    assert "adult" in PROHIBITED_KEYWORDS
    assert "lingerie" in PROHIBITED_KEYWORDS
    assert PROHIBITED_KEYWORDS[:6] == [
        "weapon", "weapons", "gun", "guns", "firearm", "firearms"]
    assert PROHIBITED_KEYWORDS[-1] == "2,4-dinitrophenol"
    assert len(PROHIBITED_KEYWORDS) == len(NON_ADULT_KEYWORDS) + len(ADULT_KEYWORDS)


def test_adult_category_skips_adult_keywords():
    """成人类目取表时剔除全部成人关键词，非成人关键词保留"""
    keywords = get_prohibited_keywords("mature")
    assert keywords == NON_ADULT_KEYWORDS
    for kw in ADULT_KEYWORDS:
        assert kw not in keywords, kw
    for kw in ["weapon", "cocaine", "casino", "counterfeit", "tobacco"]:
        assert kw in keywords, kw


def test_other_category_keeps_adult_keywords():
    """非成人类目仍按全量违禁词表判定"""
    assert get_prohibited_keywords("health_beauty") == PROHIBITED_KEYWORDS
    assert get_prohibited_keywords("") == PROHIBITED_KEYWORDS
    assert "vibrator" in get_prohibited_keywords("apparel_accessories")


# ── ProductFilter.has_prohibited_content ───────────────

def test_adult_product_passes_in_adult_category():
    """成人类目下成人商品不再判违禁"""
    p = _product("Silicone Rechargeable Vibrator for Women")
    assert ProductFilter.has_prohibited_content(p) is True          # 默认（无法判定类目）
    assert ProductFilter.has_prohibited_content(p, "mature") is False
    assert ProductFilter.has_prohibited_content(p, "成人") is False


def test_adult_keywords_in_description_and_tags_also_skipped():
    """描述/标签中的成人词在成人类目下同样跳过"""
    p = _product("Personal Massager Wand", body_html="<p>Best lingerie gift set</p>",
                 tags=["adult toys", "nsfw"])
    assert ProductFilter.has_prohibited_content(p) is True
    assert ProductFilter.has_prohibited_content(p, "mature") is False


def test_other_prohibited_words_still_blocked_for_adult_category():
    """成人类目仍过滤武器/药品/赌博等其他违禁词"""
    p = _product("Tactical Crossbow Kit")
    assert ProductFilter.has_prohibited_content(p, "mature") is True
    p2 = _product("Herbal Cannabis Gift Box")
    assert ProductFilter.has_prohibited_content(p2, "mature") is True


def test_non_adult_category_behavior_unchanged():
    """非成人类目判定逻辑与改动前一致"""
    assert ProductFilter.has_prohibited_content(_product("weapon for sale"))
    assert not ProductFilter.has_prohibited_content(_product("Normal product"))
    assert ProductFilter.has_prohibited_content(_product("Normal product", tags=["lingerie"]))


# ── 清洗入口集成（假 Collection，不连数据库）──────────────

class _FakeUpdateResult:
    def __init__(self, n):
        self.modified_count = n


class _FakeCollection:
    """最小 Collection 替身：只支持 clean_category 用到的读/写方法"""

    def __init__(self, docs):
        self.docs = docs
        self.updates = []

    def find(self, query, projection=None):
        self._cursor = list(self.docs)
        return self

    def batch_size(self, n):
        return self

    def __iter__(self):
        return iter(getattr(self, "_cursor", []))

    def bulk_write(self, ops, ordered=False):
        self.updates.append(("bulk", len(ops)))
        return _FakeUpdateResult(len(ops))

    def update_many(self, flt, update):
        keys = flt.get("unique_key", {}).get("$in", [])
        self.updates.append(("update_many", keys, update))
        return _FakeUpdateResult(len(keys))

    def final_status(self) -> dict:
        status = {}
        for op in self.updates:
            if op[0] != "update_many":
                continue
            for key in op[1]:
                status[key] = op[2]["$set"].get("clean_status")
        return status


def _doc(key, title):
    return {
        "unique_key": key, "标题": title,
        "描述": "A fine product description long enough to pass the checks.",
        "子描述": "", "图片": "https://example.com/img.jpg",
        "分类": "Test|||Item", "变体": "", "source_domain": "example.com",
        "source_category": "mature adult_toys", "原价": "29.99", "折扣价": "29.99",
    }


def _run_clean_category(category, docs, monkeypatch):
    """用假 Collection 执行 clean_category，返回 (结果, 假集合)

    site_name_from_domain 依赖 tldextract（会联网拉取公共后缀表），
    这里替换为纯字符串实现，保证测试离线且快速。
    """
    from qmds.db import product_db as product_db_module
    from qmds.utils import text_cleaner

    monkeypatch.setattr(text_cleaner, "site_name_from_domain",
                        lambda domain: str(domain or "").split(".")[0])

    client = product_db_module.ProductDBClient.__new__(product_db_module.ProductDBClient)
    fake = _FakeCollection(docs)
    client.collection = lambda c, s="": fake
    client._rebuild_single_product_counter = lambda *a, **k: None

    class _Cache:
        def invalidate(self, key=None):
            pass

    client._stats_cache = _Cache()
    result = product_db_module.ProductDBClient.clean_category(
        client, category, "other", force=True, regenerate_sku=False)
    return result, fake


def test_clean_category_keeps_adult_products_in_adult_category(monkeypatch):
    """成人类目清洗：成人商品通过，武器/药品仍被拦下"""
    docs = [
        _doc("k1", "Silicone Rechargeable Vibrator for Women"),
        _doc("k2", "Women's Lingerie Set Lace Babydoll"),
        _doc("k3", "Tactical Crossbow Kit Full Set"),
        _doc("k4", "Ceramic Coffee Mug 350ml"),
    ]
    result, fake = _run_clean_category("mature", docs, monkeypatch)
    status = fake.final_status()
    assert status["k1"] == "cleaned"
    assert status["k2"] == "cleaned"
    assert status["k3"] == "failed"
    assert status["k4"] == "cleaned"
    assert result["stats"]["违禁词"] == 1
    assert result["cleaned"] == 3


def test_clean_category_blocks_adult_products_in_other_category(monkeypatch):
    """非成人类目清洗：行为与改动前一致，成人词照常判违禁"""
    docs = [
        _doc("k1", "Silicone Rechargeable Vibrator for Women"),
        _doc("k2", "Ceramic Coffee Mug 350ml"),
    ]
    result, fake = _run_clean_category("health_beauty", docs, monkeypatch)
    status = fake.final_status()
    assert status["k1"] == "failed"
    assert status["k2"] == "cleaned"
    assert result["stats"]["违禁词"] == 1
