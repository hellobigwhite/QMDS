"""语言检测工具"""

import re

NON_ENGLISH_EUROPEAN_CHARS = set(
    "àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞß"
)

NON_LATIN_RANGES = [
    (0x0400, 0x04FF),  # 西里尔文
    (0x0590, 0x05FF),  # 希伯来文
    (0x0600, 0x06FF),  # 阿拉伯文
    (0x0900, 0x097F),  # 天城文
    (0x0E00, 0x0E7F),  # 泰文
    (0x3040, 0x30FF),  # 日文假名
    (0x3400, 0x4DBF),  # CJK统一汉字扩展A
    (0x4E00, 0x9FFF),  # CJK统一汉字
    (0xAC00, 0xD7AF),  # 韩文
]


def has_non_latin_script(text: str) -> bool:
    """检测是否包含非拉丁文字（中文、日文、韩文、阿拉伯文等）"""
    for ch in str(text or ""):
        code = ord(ch)
        for start, end in NON_LATIN_RANGES:
            if start <= code <= end:
                return True
    return False


def has_european_non_english(text: str) -> bool:
    """检测是否包含欧洲非英文字符（法语、德语、西班牙语等）"""
    if not text:
        return False
    return any(ch in NON_ENGLISH_EUROPEAN_CHARS for ch in text)


def is_non_english_text(text: str) -> bool:
    """判断文本是否为非英文"""
    sample = str(text or "").strip()
    if not sample:
        return False
    if has_non_latin_script(sample):
        return True
    if has_european_non_english(sample):
        return True

    all_letters = re.findall(r"[^\W\d_]", sample, re.UNICODE)
    latin_letters = re.findall(r"[A-Za-z]", sample)
    if len(all_letters) >= 30:
        latin_ratio = len(latin_letters) / max(len(all_letters), 1)
        if latin_ratio < 0.6:
            return True
    return False


def is_english_product(product) -> bool:
    """判断单个商品是否为英文商品"""
    from qmds.modules.data_scraper.models.schemas import Product
    if isinstance(product, Product):
        text = f"{product.title} {product.body_html}"
    elif isinstance(product, dict):
        text = f"{product.get('title', '')} {product.get('body_html', '')}"
    else:
        return True
    return not is_non_english_text(text)


def is_english_products(products: list, limit: int = 5) -> bool:
    """判断商品列表是否为英文商品（抽样检测）"""
    sample_products = products[:limit]
    chunks = []
    for p in sample_products:
        if hasattr(p, 'title'):
            chunks.append(p.title)
            chunks.append(p.body_html)
        elif isinstance(p, dict):
            chunks.append(str(p.get('title', '')))
            chunks.append(str(p.get('body_html', '')))
    sample = " ".join(chunks).strip()
    return not is_non_english_text(sample)