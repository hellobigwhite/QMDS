"""商品过滤器"""

import re
from typing import Callable

from qmds.modules.data_scraper.models.schemas import Product
from qmds.utils.language import is_non_english_text

PLACEHOLDER_IMAGES = re.compile(r"(coming-?soon|no-?image|placeholder|\.svg|logo)", re.I)
PROHIBITED_KEYWORDS = [
    "weapon", "gun", "knife", "drug", "cannabis", "counterfeit",
    "replica", "fake", "adult", "porn", "ivermectin",
]

MIN_TITLE_LENGTH = 5
MIN_DESCRIPTION_LENGTH = 10
MIN_PRICE = 3.0
MAX_PRICE = 6000.0


class ProductFilter:
    """商品数据过滤器，支持组合多个过滤规则"""

    def __init__(self):
        self._rules: list[Callable[[Product], bool]] = [
            self._price_range,
            self._title_length,
            self._image_valid,
            self._is_english,
        ]

    def add_rule(self, rule: Callable[[Product], bool]):
        self._rules.append(rule)

    def is_valid(self, product: Product) -> bool:
        return all(rule(product) for rule in self._rules)

    def filter(self, products: list[Product]) -> list[Product]:
        return [p for p in products if self.is_valid(p)]

    @staticmethod
    def _price_range(p: Product) -> bool:
        return MIN_PRICE <= p.price <= MAX_PRICE

    @staticmethod
    def _title_length(p: Product) -> bool:
        return len(p.title.strip()) >= MIN_TITLE_LENGTH

    def _image_valid(self, p: Product) -> bool:
        for url in p.images:
            if PLACEHOLDER_IMAGES.search(url):
                return False
        return True

    @staticmethod
    def has_prohibited_content(p: Product) -> bool:
        text = f"{p.title} {p.body_html} {' '.join(p.tags)}".lower()
        return any(kw in text for kw in PROHIBITED_KEYWORDS)

    @staticmethod
    def _is_english(p: Product) -> bool:
        """检测商品是否为英文"""
        text = f"{p.title} {p.body_html}"
        return not is_non_english_text(text)
