"""商品数据处理器"""

import re
from typing import Any

from qmds.modules.data_scraper.models.schemas import Product

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


class ProductProcessor:
    """商品数据清洗和标准化"""

    @staticmethod
    def clean_html(text: str) -> str:
        text = HTML_TAG_RE.sub(" ", text)
        text = WHITESPACE_RE.sub(" ", text)
        return text.strip()

    @staticmethod
    def clean_title(title: str) -> str:
        return title.strip().replace("\n", " ")

    @staticmethod
    def clean_description(desc: str) -> str:
        cleaned = ProductProcessor.clean_html(desc)
        return cleaned[:5000] if len(cleaned) > 5000 else cleaned

    @staticmethod
    def process_all(products: list[Product]) -> list[Product]:
        processed = []
        for p in products:
            p.title = ProductProcessor.clean_title(p.title)
            p.body_html = ProductProcessor.clean_description(p.body_html)
            p.tags = [t.strip() for t in p.tags if t.strip()]
            processed.append(p)
        return processed
