"""AI 分类器单元测试

测试 qmds.modules.data_scraper.ai_classifier 模块的核心功能：
- classify_store: LLM 分类（正常分类/黑五类/综合站/重试/失败）
- detect_language: 语言检测（html_lang/CJK/英文/空）
- is_non_english: 非英文判断
- google_to_qmds_category: Google 分类名 -> QMDS 简化名映射
- extract_page_info: HTML 信息提取
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from qmds.modules.data_scraper import ai_classifier
from qmds.modules.data_scraper.ai_classifier import (
    _unrecognized,
    build_prompt,
    classify_store,
    detect_language,
    extract_page_info,
    google_to_qmds_category,
    is_non_english,
    reset_glm_client,
)


# =========================
# TestGoogleToQmdsCategory
# =========================

class TestGoogleToQmdsCategory:
    def test_hardware(self):
        assert google_to_qmds_category("Hardware") == "hardware"

    def test_animals_pet_supplies(self):
        assert google_to_qmds_category("Animals & Pet Supplies") == "animals_pet_supplies"

    def test_food_beverages(self):
        assert google_to_qmds_category("Food, Beverages & Tobacco") == "food_beverages_tobacco"

    def test_electronics(self):
        assert google_to_qmds_category("Electronics") == "electronics"

    def test_unknown(self):
        assert google_to_qmds_category("Unknown Category") is None

    def test_empty(self):
        assert google_to_qmds_category("") is None

    def test_none(self):
        assert google_to_qmds_category(None) is None


# =========================
# TestDetectLanguage
# =========================

class TestDetectLanguage:
    def test_html_lang_priority(self):
        """html_lang 字段优先级最高"""
        page_info = {
            "html_lang": "zh-cn",
            "title": "English Title",
            "meta_description": "English description",
        }
        assert detect_language(page_info) == "zh-cn"

    def test_html_lang_english(self):
        page_info = {"html_lang": "en-us", "title": "Test"}
        assert detect_language(page_info) == "en-us"

    def test_cjk_fallback_chinese(self):
        """无 html_lang，文本含 >=3 个 CJK 汉字 -> zh"""
        page_info = {
            "title": "在线购物商城特价商品促销",
            "meta_description": "优质商品在线销售",
        }
        lang = detect_language(page_info)
        assert lang == "zh"

    def test_cjk_fallback_japanese(self):
        """无 html_lang，文本含 >=3 个 CJK 且含 >=2 个假名 -> ja"""
        page_info = {
            "title": "オンラインショップ",
            "meta_description": "商品を販売",
        }
        lang = detect_language(page_info)
        assert lang == "ja"

    def test_english_text(self):
        """英文文本 -> en (langdetect)"""
        page_info = {
            "title": "Best Online Store for Electronics and Gadgets",
            "meta_description": "We sell the best electronics products online",
        }
        lang = detect_language(page_info)
        assert lang is not None

    def test_empty_page_info(self):
        """空 page_info -> None"""
        assert detect_language({}) is None

    def test_short_text(self):
        """文本过短（<10 字符）-> None"""
        page_info = {"title": "Hi", "meta_description": ""}
        assert detect_language(page_info) is None


# =========================
# TestIsNonEnglish
# =========================

class TestIsNonEnglish:
    def test_chinese(self):
        page_info = {"html_lang": "zh-cn"}
        non_en, lang = is_non_english(page_info)
        assert non_en is True
        assert lang == "zh-cn"

    def test_japanese(self):
        page_info = {"html_lang": "ja"}
        non_en, lang = is_non_english(page_info)
        assert non_en is True
        assert lang == "ja"

    def test_english(self):
        page_info = {"html_lang": "en-us"}
        non_en, lang = is_non_english(page_info)
        assert non_en is False
        assert lang == "en-us"

    def test_unknown(self):
        """无法判定语言时返回 (False, None)"""
        page_info = {}
        non_en, lang = is_non_english(page_info)
        assert non_en is False
        assert lang is None


# =========================
# TestExtractPageInfo
# =========================

class TestExtractPageInfo:
    def test_extract_title_meta_nav(self):
        html = """
        <html lang="en">
        <head>
            <title>Test Store - Online Shop</title>
            <meta name="description" content="We sell the best products">
        </head>
        <body>
            <nav>
                <a href="/collections/all">All Products</a>
                <a href="/collections/shoes">Shoes</a>
                <a href="/collections/shirts">Shirts</a>
            </nav>
        </body>
        </html>
        """
        info = extract_page_info(html)
        assert info.get("html_lang") == "en"
        assert info.get("title") == "Test Store - Online Shop"
        assert info.get("meta_description") == "We sell the best products"
        assert "All Products" in info.get("nav_categories", [])
        assert "Shoes" in info.get("nav_categories", [])

    def test_extract_shop_name(self):
        html = '<html><body><script>{"shop":{"name":"MyShop"}}</script></body></html>'
        info = extract_page_info(html)
        assert info.get("shop_name") == "MyShop"

    def test_empty_html(self):
        info = extract_page_info("")
        assert "title" not in info
        assert "meta_description" not in info

    def test_url_keywords(self):
        html = '<html><body><a href="/collections/all">All</a></body></html>'
        info = extract_page_info(html)
        assert "collections" in info.get("url_keywords", [])


# =========================
# TestBuildPrompt
# =========================

class TestBuildPrompt:
    def test_prompt_contains_categories(self):
        prompt = build_prompt("Title", "Desc", "Nav", "Shop", "url_kw")
        assert "Apparel & Accessories" in prompt
        assert "Hardware" in prompt
        assert "Electronics" in prompt
        assert "黑五类" in prompt or "black-five" in prompt.lower()
        assert "综合站" in prompt

    def test_prompt_contains_store_info(self):
        prompt = build_prompt("My Title", "My Desc", "Nav1, Nav2", "MyShop", "kw1")
        assert "My Title" in prompt
        assert "My Desc" in prompt
        assert "Nav1, Nav2" in prompt
        assert "MyShop" in prompt
        assert "kw1" in prompt

    def test_prompt_with_homepage_content(self):
        prompt = build_prompt("T", "D", "N", "S", "K", homepage_content="live content")
        assert "live content" in prompt

    def test_prompt_with_collection_titles(self):
        prompt = build_prompt("T", "D", "N", "S", "K", collection_titles=["Coll1", "Coll2"])
        assert "Coll1" in prompt
        assert "Coll2" in prompt


# =========================
# TestClassifyStore
# =========================

class TestClassifyStore:
    def setup_method(self):
        """每个测试前重置 LLM 客户端"""
        reset_glm_client()

    def _mock_completion(self, content: str):
        """构造 mock completion 对象"""
        mock_msg = MagicMock()
        mock_msg.content = content
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        return mock_completion

    def _setup_mimo_key(self):
        """设置 MIMO_API_KEY（classify_store 内部检查）"""
        ai_classifier.settings.mimo_api_key = "test-key"

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_normal_category(self, mock_get_client):
        """正常分类：LLM 返回 Hardware > Tools"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_completion(
            json.dumps({"category": "Hardware", "subcategory": "Hardware > Tools", "is_black_five": False})
        )
        mock_get_client.return_value = mock_client

        page_info = {"title": "Tool Shop", "meta_description": "We sell tools", "nav_categories": ["Tools"]}
        result = classify_store(page_info, "example.com")

        assert result["category"] == "Hardware"
        assert result["subcategory"] == "Hardware > Tools"
        assert result["is_filtered"] is False
        assert result["is_comprehensive"] is False
        assert result["source_subcategory"] == "Hardware > Tools"

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_black_five(self, mock_get_client):
        """黑五类：LLM 返回 is_black_five=true"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_completion(
            json.dumps({
                "category": "黑五类",
                "subcategory": "黑五类 > weapons",
                "is_black_five": True,
                "black_five_type": "Weapons/Guns/Ammunition",
            })
        )
        mock_get_client.return_value = mock_client

        page_info = {"title": "Gun Shop", "meta_description": "Firearms", "nav_categories": ["Guns"]}
        result = classify_store(page_info, "gunshop.com")

        assert result["category"] == "黑五类"
        assert result["is_filtered"] is True
        assert result["is_comprehensive"] is False
        assert result["black_five_type"] == "Weapons/Guns/Ammunition"

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_comprehensive(self, mock_get_client):
        """综合站：LLM 返回 category=综合站"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_completion(
            json.dumps({
                "category": "综合站",
                "subcategories": ["Electronics", "Apparel & Accessories"],
                "is_black_five": False,
            })
        )
        mock_get_client.return_value = mock_client

        page_info = {"title": "Mega Store", "meta_description": "Everything", "nav_categories": ["Electronics", "Clothing"]}
        result = classify_store(page_info, "megastore.com")

        assert result["category"] == "综合站"
        assert result["is_filtered"] is False
        assert result["is_comprehensive"] is True
        assert "Electronics" in result["subcategory"]

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_json_with_markdown(self, mock_get_client):
        """LLM 返回带 markdown 代码块的 JSON"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_completion(
            '```json\n{"category": "Electronics", "subcategory": "Electronics > Computers", "is_black_five": false}\n```'
        )
        mock_get_client.return_value = mock_client

        page_info = {"title": "Computer Shop", "meta_description": "Computers", "nav_categories": ["Laptops"]}
        result = classify_store(page_info, "computershop.com")

        assert result["category"] == "Electronics"
        assert result["subcategory"] == "Electronics > Computers"

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_json_parse_retry(self, mock_get_client):
        """第一次返回非法 JSON，第二次成功 -> 重试后成功"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        # 第一次抛异常（JSON 解析失败），第二次返回有效 JSON
        good_completion = self._mock_completion(
            json.dumps({"category": "Hardware", "subcategory": "Hardware > Tools", "is_black_five": False})
        )
        mock_client.chat.completions.create.side_effect = [json.JSONDecodeError("err", "doc", 0), good_completion]
        mock_get_client.return_value = mock_client

        page_info = {"title": "Tool Shop", "meta_description": "Tools", "nav_categories": ["Tools"]}
        result = classify_store(page_info, "example.com")

        assert result["category"] == "Hardware"
        assert mock_client.chat.completions.create.call_count == 2

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_all_retries_failed(self, mock_get_client):
        """三次重试都失败 -> 返回无法识别"""
        self._setup_mimo_key()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")
        mock_get_client.return_value = mock_client

        page_info = {"title": "Shop", "meta_description": "Desc", "nav_categories": ["Nav"]}
        result = classify_store(page_info, "example.com")

        assert result["category"] == "无法识别"
        assert result["subcategory"] == "无法识别"
        assert mock_client.chat.completions.create.call_count == 3

    def test_no_mimo_key(self):
        """未配置 MIMO_API_KEY -> 返回无法识别"""
        ai_classifier.settings.mimo_api_key = ""
        reset_glm_client()

        page_info = {"title": "Shop", "meta_description": "Desc", "nav_categories": ["Nav"]}
        result = classify_store(page_info, "example.com")

        assert result["category"] == "无法识别"

    def test_needs_fetch_no_domain(self):
        """page_info 缺 meta/nav 且无 domain -> 无法识别"""
        self._setup_mimo_key()
        page_info = {"title": "Shop"}  # 无 meta_description, 无 nav_categories
        result = classify_store(page_info, "")
        assert result["category"] == "无法识别"

    @patch("qmds.modules.data_scraper.ai_classifier.fetch_and_clean")
    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_needs_fetch_success(self, mock_get_client, mock_fetch):
        """page_info 缺 meta/nav，抓取首页后成功分类"""
        self._setup_mimo_key()
        mock_fetch.return_value = "cleaned homepage content"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_completion(
            json.dumps({"category": "Hardware", "subcategory": "Hardware > Tools", "is_black_five": False})
        )
        mock_get_client.return_value = mock_client

        page_info = {"title": "Shop"}  # 无 meta_description, 无 nav_categories
        result = classify_store(page_info, "example.com")

        assert result["category"] == "Hardware"
        mock_fetch.assert_called_once()

    @patch("qmds.modules.data_scraper.ai_classifier.fetch_and_clean")
    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_needs_fetch_empty(self, mock_get_client, mock_fetch):
        """page_info 缺 meta/nav，抓取首页返回空 -> 无法识别"""
        self._setup_mimo_key()
        mock_fetch.return_value = ""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        page_info = {"title": "Shop"}
        result = classify_store(page_info, "example.com")

        assert result["category"] == "无法识别"
        mock_client.chat.completions.create.assert_not_called()


# =========================
# TestUnrecognized
# =========================

class TestUnrecognized:
    def test_unrecognized_structure(self):
        result = _unrecognized()
        assert result["category"] == "无法识别"
        assert result["subcategory"] == "无法识别"
        assert result["is_filtered"] is False
        assert result["is_comprehensive"] is False
        assert result["black_five_type"] == ""
        assert result["source_subcategory"] == ""


# =========================
# TestClassifyBatch
# =========================

class TestClassifyBatch:
    def setup_method(self):
        reset_glm_client()
        ai_classifier.settings.mimo_api_key = "test-key"

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_batch_classify(self, mock_get_client):
        """批量分类多个站点"""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=json.dumps({
                "category": "Hardware",
                "subcategory": "Hardware > Tools",
                "is_black_five": False,
            })))]
        )
        mock_get_client.return_value = mock_client

        stores = [
            {"domain": "shop1.com", "url": "https://shop1.com",
             "page_info": {"title": "Tool Shop", "meta_description": "Tools", "nav_categories": ["Tools"]}},
            {"domain": "shop2.com", "url": "https://shop2.com",
             "page_info": {"title": "Hardware Store", "meta_description": "Hardware", "nav_categories": ["Tools"]}},
        ]
        results = ai_classifier.classify_batch(stores)

        assert len(results) == 2
        for r in results:
            assert r["result"]["category"] == "Hardware"

    def test_empty_batch(self):
        results = ai_classifier.classify_batch([])
        assert results == []

    @patch("qmds.modules.data_scraper.ai_classifier._get_glm_client")
    def test_batch_with_non_english(self, mock_get_client):
        """批量分类含非英文站（html_lang 优先检测，不调用 LLM）"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        stores = [
            {"domain": "en-shop.com", "url": "https://en-shop.com",
             "page_info": {"html_lang": "en", "title": "Shop", "meta_description": "Desc", "nav_categories": ["Nav"]}},
            {"domain": "cn-shop.com", "url": "https://cn-shop.com",
             "page_info": {"html_lang": "zh-cn"}},
        ]
        results = ai_classifier.classify_batch(stores)

        assert len(results) == 2
        # en-shop 调用 LLM
        assert results[0]["is_non_english"] is False
        # cn-shop 被识别为非英文，不调用 LLM
        assert results[1]["is_non_english"] is True
        assert results[1]["language"] == "zh-cn"
        assert results[1]["result"]["category"] == "非英文站"
