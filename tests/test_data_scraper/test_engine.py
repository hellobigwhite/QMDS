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
        p = Product(source_url="", title="Gun shop", price=10.0)
        assert not ProductFilter.has_prohibited_content(p)
        assert self.filter.is_valid(p)


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
