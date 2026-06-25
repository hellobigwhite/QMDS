"""数据爬取模块单元测试"""

from qmds.modules.data_scraper.models.schemas import Product, Platform, Store
from qmds.modules.data_scraper.pipeline import ProductFilter, ProductProcessor


class TestProductFilter:
    def setup_method(self):
        self.filter = ProductFilter()
        self.valid_product = Product(
            source_url="https://example.com/p/test",
            title="Test Product Title",
            price=29.99,
            images=["https://example.com/img.jpg"],
        )

    def test_valid_product(self):
        assert self.filter.is_valid(self.valid_product)

    def test_price_too_low(self):
        p = Product(source_url="", title="Test", price=1.0)
        assert not self.filter.is_valid(p)

    def test_price_too_high(self):
        p = Product(source_url="", title="Test", price=9999.0)
        assert not self.filter.is_valid(p)

    def test_short_title(self):
        p = Product(source_url="", title="AB", price=10.0)
        assert not self.filter.is_valid(p)

    def test_prohibited_keyword(self):
        p = Product(source_url="", title="weapon for sale", price=10.0)
        assert self.filter.has_prohibited_content(p)

    def test_clean_product(self):
        p = Product(source_url="", title="Weapon shop", price=10.0)
        assert ProductFilter.has_prohibited_content(p)
        p2 = Product(source_url="", title="Normal product", price=10.0)
        assert not ProductFilter.has_prohibited_content(p2)
        assert self.filter.is_valid(p2)

    def test_image_valid_filter(self):
        """测试图片有效性过滤 - 匹配到占位图片则整个商品无效"""
        # 测试占位图片过滤
        p1 = Product(
            source_url="",
            title="Test Product",
            price=10.0,
            images=[
                "https://example.com/coming-soon.jpg",
                "https://example.com/no-image.png",
                "https://example.com/placeholder.gif",
                "https://example.com/image.svg",
                "https://example.com/valid-image.jpg",
            ]
        )
        # 包含占位图片，商品无效
        assert not self.filter._image_valid(p1)

    def test_image_valid_filter_logo(self):
        """测试logo图片过滤"""
        p = Product(
            source_url="",
            title="Test Product",
            price=10.0,
            images=["https://example.com/logo.png"]
        )
        assert not self.filter._image_valid(p)

    def test_image_valid_filter_no_placeholders(self):
        """测试没有占位图片的情况"""
        p = Product(
            source_url="",
            title="Test Product",
            price=10.0,
            images=[
                "https://example.com/img1.jpg",
                "https://example.com/img2.png",
            ]
        )
        assert self.filter._image_valid(p)

    def test_image_valid_filter_empty_images(self):
        """测试空图片列表的情况"""
        p = Product(
            source_url="",
            title="Test Product",
            price=10.0,
            images=[]
        )
        assert self.filter._image_valid(p)

    def test_is_english_filter(self):
        """测试英文商品过滤"""
        # 英文商品应该通过
        p1 = Product(
            source_url="",
            title="Beautiful Summer Dress",
            price=10.0,
            images=["https://example.com/img.jpg"],
            body_html="<p>A beautiful dress for summer</p>"
        )
        assert self.filter._is_english(p1)

    def test_is_english_filter_chinese(self):
        """测试中文商品过滤"""
        # 中文商品应该被过滤
        p = Product(
            source_url="",
            title="漂亮的夏季连衣裙",
            price=10.0,
            images=["https://example.com/img.jpg"],
            body_html="<p>适合夏季穿着的漂亮连衣裙</p>"
        )
        assert not self.filter._is_english(p)

    def test_is_english_filter_japanese(self):
        """测试日文商品过滤"""
        # 日文商品应该被过滤
        p = Product(
            source_url="",
            title="美しい夏のドレス",
            price=10.0,
            images=["https://example.com/img.jpg"],
            body_html="<p>夏に最適なドレス</p>"
        )
        assert not self.filter._is_english(p)

    def test_is_english_filter_french(self):
        """测试法文商品过滤"""
        # 法文商品应该被过滤
        p = Product(
            source_url="",
            title="Belle robe d'été",
            price=10.0,
            images=["https://example.com/img.jpg"],
            body_html="<p>Une belle robe pour l'été</p>"
        )
        assert not self.filter._is_english(p)

    def test_is_english_filter_empty(self):
        """测试空内容"""
        # 空内容应该通过（不判断为非英文）
        p = Product(
            source_url="",
            title="",
            price=10.0,
            images=["https://example.com/img.jpg"],
            body_html=""
        )
        assert self.filter._is_english(p)


class TestProductProcessor:
    def test_clean_html(self):
        html = "<p>Hello <b>World</b></p>"
        assert ProductProcessor.clean_html(html) == "Hello World"

    def test_clean_title(self):
        assert ProductProcessor.clean_title("  My Product  ") == "My Product"

    def test_process_all(self):
        products = [
            Product(source_url="", title="  Test  ", body_html="<p>Desc</p>", tags=["a", "  b  "]),
        ]
        result = ProductProcessor.process_all(products)
        assert result[0].title == "Test"
        assert result[0].body_html == "Desc"
        assert result[0].tags == ["a", "b"]

    def test_deduplicate_by_title(self):
        products = [
            Product(source_url="https://example.com/1", title="Product A", price=10.0),
            Product(source_url="https://example.com/2", title="Product B", price=20.0),
            Product(source_url="https://example.com/3", title="product a", price=15.0),  # 重复标题（大小写不同）
            Product(source_url="https://example.com/4", title="Product C", price=30.0),
            Product(source_url="https://example.com/5", title="Product B", price=25.0),  # 重复标题
        ]
        result = ProductProcessor.deduplicate_by_title(products)
        assert len(result) == 3
        titles = [p.title for p in result]
        assert "Product A" in titles
        assert "Product B" in titles
        assert "Product C" in titles

    def test_deduplicate_by_title_empty(self):
        products = []
        result = ProductProcessor.deduplicate_by_title(products)
        assert len(result) == 0

    def test_deduplicate_by_title_no_duplicates(self):
        products = [
            Product(source_url="https://example.com/1", title="Product A", price=10.0),
            Product(source_url="https://example.com/2", title="Product B", price=20.0),
        ]
        result = ProductProcessor.deduplicate_by_title(products)
        assert len(result) == 2
