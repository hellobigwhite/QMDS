# -*- coding: utf-8 -*-
"""类目补充关键词（宗教相关珠宝等）匹配测试

背景：Google Product Taxonomy 只收录到 Prayer Beads / Prayer Cards / Religious
Altars / Religious Veils / Tarot Cards，十字架项链、念珠手串、圣牌这类
「宗教 + 珠宝」商品完全没有收录，精准类目筛选时匹配不到宗教类目。

覆盖：
- match_category_extra_phrases：宗教珠宝/专有词命中，通用词不命中
- category_matcher.match_title：宗教珠宝可匹配宗教类目；
  裸项链、cross training、crossbody 等不得被判进宗教类目
- site_classifier 同步生效
"""

from qmds.config.categories import CATEGORY_EXTRA_PHRASES, match_category_extra_phrases
from qmds.modules.data_scraper.category_matcher import match_title
from qmds.utils.site_classifier import SiteClassifier


# ── 补充关键词短语匹配 ────────────────────────

def test_religious_jewelry_phrases_hit():
    """宗教相关珠宝应命中宗教类目"""
    for title in ("Cross Necklaces", "Cross Pendant", "Silver Cross Bracelet",
                  "Rosary Bracelets", "Rosary Beads Necklace", "Crucifix Pendant",
                  "Gold Crucifix Necklace", "Religious Jewelry",
                  "Saint Medal Necklace", "Bible Verse Bracelet",
                  "Faith Necklace", "Confirmation Gifts"):
        assert match_category_extra_phrases("religious_ceremonial", title), title


def test_religious_proper_nouns_hit():
    """宗教专有词单独出现即可判定"""
    for title in ("Rosary", "Rosaries", "Crucifixes", "Mezuzah Cases",
                  "Menorahs", "Kippahs", "Prayer Shawls", "Bible Covers",
                  "Christian Gifts", "Catholic Books", "Judaica", "First Communion",
                  "Incense Burners", "Chalices", "Communion Cups"):
        assert match_category_extra_phrases("religious_ceremonial", title), title


def test_generic_words_do_not_hit():
    """通用词不能单独把标题判进宗教类目（关键回归）"""
    for title in ("Gold Necklaces", "Sterling Silver Rings", "Diamond Earrings",
                  "Cross Training Shoes", "Crossbody Bags", "Wedding Bands",
                  "Yoga Mats", "Candle Holders", "Silver Charms", "Faith Hill CD"):
        assert not match_category_extra_phrases("religious_ceremonial", title), title


def test_singular_and_plural_forms():
    """单复数标题都能命中（词表只写一种形式）"""
    assert match_category_extra_phrases("religious_ceremonial", "Kippah")
    assert match_category_extra_phrases("religious_ceremonial", "Kippahs")
    assert match_category_extra_phrases("religious_ceremonial", "Rosary")
    assert match_category_extra_phrases("religious_ceremonial", "Rosaries")
    assert match_category_extra_phrases("religious_ceremonial", "Cross Necklace")
    assert match_category_extra_phrases("religious_ceremonial", "Crosses Necklaces")
    assert match_category_extra_phrases("religious_ceremonial", "Crucifix")
    assert match_category_extra_phrases("religious_ceremonial", "Crucifixes")


def test_bare_cross_is_excluded_by_design():
    """裸 cross 故意不收录：它同时是 cross training / cross stitch 等非宗教词

    宗教语境下的十字架商品基本都带首饰词（cross necklace / cross pendant /
    cross bracelet ...），这些短语都已收录；单独一个 "Crosses" 才判宗教会
    把运动/手工艺类店铺误判进来，因此这里固定该取舍行为。
    """
    for title in ("Crosses", "Cross", "Cross Training Shoes", "Cross Stitch Kits",
                  "Cross Country Skis", "CrossFit Gear"):
        assert not match_category_extra_phrases("religious_ceremonial", title), title


def test_case_punctuation_and_order_insensitive():
    assert match_category_extra_phrases("religious_ceremonial", "CROSS NECKLACE")
    assert match_category_extra_phrases("religious_ceremonial", "Cross & Rosary Necklaces")
    assert match_category_extra_phrases("religious_ceremonial", "Necklace with Cross Pendant")
    assert match_category_extra_phrases("religious_ceremonial", "cross-necklace")


def test_unknown_category_returns_false():
    assert not match_category_extra_phrases("electronics", "Cross Necklaces")
    assert not match_category_extra_phrases("", "Cross Necklaces")
    assert not match_category_extra_phrases("religious_ceremonial", "")


# ── 精准类目筛选 match_title 集成 ──────────────

def test_match_title_religious_jewelry():
    """宗教珠宝在精准类目筛选中匹配到宗教类目"""
    for title in ("Cross Necklaces", "Rosary Bracelets", "Crucifix Pendants",
                  "Bible Covers", "Christian Gifts", "Mezuzah Cases"):
        assert match_title("religious_ceremonial", title), title


def test_match_title_generic_jewelry_still_not_religious():
    """普通珠宝仍不匹配宗教类目（taxonomy 单词交集不受补充词表污染）"""
    for title in ("Gold Necklaces", "Sterling Silver Rings", "Diamond Earrings",
                  "Cross Training Shoes", "Crossbody Bags", "Gemstone Bracelets"):
        assert not match_title("religious_ceremonial", title), title


def test_match_title_taxonomy_behavior_unchanged():
    """原有 taxonomy 匹配行为保持不变"""
    assert match_title("religious_ceremonial", "Prayer Beads")       # taxonomy 收录
    assert match_title("religious_ceremonial", "Wedding Ceremony Supplies")
    assert not match_title("religious_ceremonial", "")
    # 其他类目不受补充词表影响：Rosary 只对宗教类目生效
    # （"Rosary Bracelets" 会因 taxonomy 里的 bracelets 命中服饰类目，
    #   那是补充词表出现之前的原有行为，不在本次改动范围）
    assert match_title("apparel_accessories", "Watches")
    assert not match_title("apparel_accessories", "Rosary")
    assert match_title("religious_ceremonial", "Rosary")


def test_extra_phrases_table_structure():
    """词表结构：键为一级分类名，值为短语元组，短语均为小写"""
    assert "religious_ceremonial" in CATEGORY_EXTRA_PHRASES
    for cat, phrases in CATEGORY_EXTRA_PHRASES.items():
        assert isinstance(phrases, tuple) and phrases, cat
        for p in phrases:
            assert p == p.lower().strip(), p


# ── site_classifier 集成 ─────────────────────

def test_site_classifier_matches_religious_jewelry():
    """站点主营类目判断同样识别宗教珠宝"""
    clf = SiteClassifier.__new__(SiteClassifier)   # 不初始化 http
    matched = clf._match_text_to_all_categories("Cross Necklaces")
    assert "religious_ceremonial" in matched


def test_site_classifier_generic_jewelry_not_religious():
    clf = SiteClassifier.__new__(SiteClassifier)
    matched = clf._match_text_to_all_categories("Gold Necklaces")
    assert "religious_ceremonial" not in matched
