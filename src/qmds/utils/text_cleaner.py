"""通用文本/数据清洗工具（移植自 BB_Data_Tool 数据预处理流程）

提供与来源无关的通用清洗能力：
- 文本净化：控制字符 / Unicode 转义 / 汉字 / 非 ASCII 字符移除
- 标题清洗：逗号、反斜杠、多余空白、内嵌 HTML 处理
- 描述 HTML 白名单清洗：实体解码、去注释与 style、a 解包、剥离标签属性
- 价格解析：货币符号/杂字符兜底，保留数字与小数点
- 图片链接：多链接取第一张
- 品牌黑名单：命中即判定为侵权数据
- 站点名 / 站点域名移除
- Excel 非法控制字符兜底清理

仅依赖标准库与 beautifulsoup4。
"""

import math
import random
import re
import string
from html import unescape

from bs4 import BeautifulSoup

# Excel/openpyxl 不允许写入工作表的控制字符（如 \x05 会导致 IllegalCharacterError）
ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

# 描述中允许保留的 HTML 标签，白名单之外的标签解包并保留其文字内容
ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
    "strong", "b", "em", "i", "blockquote", "span", "br",
    "table", "tr", "td", "th",
]

# 品牌黑名单：标题/描述命中任意品牌词即视为侵权数据
# （与 BB_Data_Tool 源表逐条对齐，含其错拼变体项；大小写不敏感子串匹配）
BRAND_BLACKLIST = [
    "Hermes", "Chanel", "Givenchy", "Prada", "Gucci", " LV ", "YSL", "Delvaux", "Marni",
    "Melberry", "Dior", "Chloe", "Loewe", "Fendi", "Proenza", "McQueen", "Vetements",
    "Balenciaga", "MOSCHINO", "Issey Miyake", "Canada Goose", "Celine", "KENZO",
    "COMME DES GAR?ONS", "Supreme", "Phillip Lim", "Y-3", "Thom Browne", "Coach",
    "Michael Kors", "Kate Spade", "Under Armour", "Tory Burch", "Marc Jacobs",
    "Armani Exchange", "Nike", "Adidas", "Louis Vuitton", "Patek Philippe",
    "Audermars Piguet", "Vacheron Constantin", "Vacherron Constantin",
    "A. Lange&Sohne", "Breguet", "Roger Dubius", "Parmigiani", "Blancpain",
    "Ulysse Nardin", "Franck Muller", "Glashutte Original", "Gurard-Perregaux",
    "Rolex", "IWC", "Jaeger-LeCoultre", "Cartier", "Chopard", "Piaget", "OMEGA",
    "Chrond", "Corum", "Zenith", "Movado", "Longiness", "Tissot", "Seiko", "Citizen",
    "Casio", "Bulova", "Swatch", "Lego", "Daneil Wellington", "lego", "disney",
    "nintendo", "Hello Kitty", "Cannabis", "Marijuana", "Roach clip", "Hydroponic",
    "Indica", "Sativa", " strain ", "Medical",
    "Hermès", "Céline", "Bvlgari", "Miu Miu", "Christian Dior", "Saint Laurent",
    "Chloé", "Bottega Veneta", "Valentino", "Alexander McQueen", "Moncler", "Lacoste",
    "Ralph Lauren", "Hugo Boss", "Off-White", "The Row", "Acne Studios", "Jil Sander",
    "Dries Van Noten", "Furla", "Rimowa", "Goyard", "Sophie Hulme", "Tod's",
    "Brunello Cucinelli", "Philipp Plein", "Balmain", "Stella McCartney",
    "Isabel Marant", "Kenzo", "Comme des Garçons", "Rei Kawakubo",
]

_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1F\x7F-\x9F]")
_CJK_CHARS_RE = re.compile(r"[\u4e00-\u9fa5]")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]+")
_NON_ALLOWED_CHARS_RE = re.compile(r"[^a-zA-Z0-9!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~]")
_HTML_TAG_RE = re.compile(r"<.*?>")
_MULTISPACE_RE = re.compile(r"\s+")


def clean_excel_illegal_chars(value):
    """清理 Excel/openpyxl 不允许写入的非法控制字符"""
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        value = value.replace("\ufffd", "")
    return value


def clean_text(text) -> str:
    """净化文本：移除控制字符、Unicode 转义符、汉字及一切非 ASCII 字符，
    仅保留字母、数字和常用标点符号"""
    if not isinstance(text, str):
        return ""

    try:
        text = text.encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    text = _CONTROL_CHARS_RE.sub("", text)
    text = _UNICODE_ESCAPE_RE.sub("", text)
    text = _CJK_CHARS_RE.sub("", text)
    text = _NON_ASCII_RE.sub("", text)
    text = _NON_ALLOWED_CHARS_RE.sub("", text)
    return text.strip()


def clean_title_text(name) -> str:
    """清洗标题：ASCII 净化（去控制字符/汉字/非ASCII）、实体解码、
    去除逗号与反斜杠、合并空白；含 HTML 时提取纯文本。
    对应 BB 流程：clean_text(全表) -> clean_name -> replace_entities"""
    if not isinstance(name, str):
        name = str(name or "")
    name = clean_text(name)
    name = name.replace("  ", " ").replace(",", "").replace("，", "")
    name = name.replace("\\", "")
    name = _MULTISPACE_RE.sub(" ", name)
    if _HTML_TAG_RE.search(name):
        return unescape(BeautifulSoup(name, "html.parser").get_text()).strip()
    return unescape(name).strip()


def clean_description_html(description) -> str:
    """描述 HTML 白名单清洗：
    - 还原全部 HTML 实体
    - 删除注释与 style/script 内容
    - <a> 标签解包保留文字
    - 剥离所有标签属性，白名单外标签解包
    解析失败时返回原文。"""
    if not isinstance(description, str) or not description.strip():
        return description if isinstance(description, str) else ""

    description = unescape(description)

    description = re.sub(r"<!--.*?-->", "", description, flags=re.DOTALL)
    description = re.sub(r"<style>.*?</style>", "", description, flags=re.DOTALL)
    description = re.sub(r"<script>.*?</script>", "", description, flags=re.DOTALL)

    try:
        soup = BeautifulSoup(description, "html.parser")
        for a in soup.find_all("a"):
            a.unwrap()
        for tag in soup.find_all(True):
            tag.attrs = {}
            if tag.name not in ALLOWED_TAGS:
                tag.unwrap()
        return str(soup).strip()
    except Exception:
        return description


def clean_price_value(value) -> float:
    """解析价格：剔除货币符号等杂字符，兼容千分位与小数逗号（欧式格式），
    四舍五入两位；无法解析时返回 0.0"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        result = round(float(value), 2)
        return 0.0 if math.isnan(result) else result
    value = str(value)
    value = value.replace("'", ",")
    for symbol in ("￥", "$", "元", "€", "£"):
        value = value.replace(symbol, "")
    if "," in value and "." in value:
        # 同时存在时，靠右的为小数分隔符，另一个为千分位
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        # 仅逗号：末段 1-2 位视为欧式小数，否则视为千分位
        tail = value.rsplit(",", 1)[-1].strip()
        value = value.replace(",", ".") if len(tail) <= 2 else value.replace(",", "")
    value = "".join(re.findall(r"[0-9.]", value))
    try:
        result = round(float(value), 2)
        return 0.0 if math.isnan(result) else result
    except (ValueError, TypeError):
        return 0.0


def pick_first_image(image_urls) -> str:
    """图片链接处理：多个链接（逗号分隔）时仅保留第一个"""
    if not isinstance(image_urls, str):
        return ""
    image_list = image_urls.split(",") if "," in image_urls else [image_urls]
    return image_list[0].strip() if image_list else ""


def hit_brand_blacklist(text) -> bool:
    """检测文本是否命中品牌黑名单（不区分大小写）"""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in BRAND_BLACKLIST)


def remove_site_name(text: str, name: str) -> str:
    """从文本中移除站点名称（按单词边界匹配）"""
    if isinstance(text, str) and name:
        return re.sub(rf"\b{re.escape(name)}\b", "", text, flags=re.IGNORECASE)
    return text


def remove_site_domain(text: str, domain: str) -> str:
    """从文本中移除站点域名（裸域名 / www. / http:// / https:// 形式）"""
    if isinstance(text, str) and domain:
        for pattern in (re.escape(domain), f"www.{re.escape(domain)}",
                        f"http://{re.escape(domain)}", f"https://{re.escape(domain)}"):
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text


def site_name_from_domain(domain: str) -> str:
    """从域名提取站点名主体（注册域名标签，如 'www.example.com' -> 'example'）"""
    if not domain:
        return ""
    host = re.sub(r"^https?://", "", domain.lower().strip())
    host = re.sub(r"^www\.", "", host)
    host = host.split("/")[0].split(":")[0]
    try:
        import tldextract
        ext = tldextract.extract(host)
        if ext.domain:
            return ext.domain
    except Exception:
        pass
    parts = host.split(".")
    return parts[0] if len(parts) >= 2 else ""


def generate_reference_sku() -> str:
    """生成参考 SKU：随机 1-4 位大写字母 + 8-12 位数字"""
    prefix = "".join(random.choices(string.ascii_uppercase, k=random.randint(1, 4)))
    suffix = "".join(random.choices(string.digits, k=random.randint(8, 12)))
    return prefix + suffix


class SKUGenerator:
    """基于参考 SKU（字母前缀+数字）的递增 SKU 生成器"""

    def __init__(self, reference_sku: str):
        match = re.match(r"([a-zA-Z]+)(\d+)", str(reference_sku or ""))
        if not match:
            raise ValueError("参考SKU格式无效，请确保格式为字母+数字（例如AWD324324543）")
        self.prefix = match.group(1)
        self.current_num = int(match.group(2))

    def generate_sku(self) -> str:
        sku = f"{self.prefix}{self.current_num}"
        self.current_num += 1
        return sku

    def reset_counter(self, reference_sku: str):
        match = re.match(r"([a-zA-Z]+)(\d+)", str(reference_sku or ""))
        if match:
            self.prefix = match.group(1)
            self.current_num = int(match.group(2))


def normalize_variant_field(content) -> str:
    """变体字段规范化：去除首部 #、连续 # 合一（对应 clean_cf_opingts）"""
    if not isinstance(content, str):
        return ""
    content = content.lstrip("#")
    content = re.sub(r"#+", "#", content)
    return content.strip()


def truncate_variant_field(variant_str, max_segments: int = 2) -> str:
    """变体字段按 ||| 截断，仅保留前 N 段属性"""
    if not isinstance(variant_str, str):
        return ""
    if "|||" in variant_str:
        return "|||".join(variant_str.split("|||")[:max_segments])
    return variant_str


def is_valid_variant_format(variant_str, max_segments: int = 2) -> bool:
    """校验变体字段格式：空串合法（无变体商品）；
    非空时段数不超过 max_segments，每段必须为 属性^值 形式，
    属性与值非空，# 分隔的各值均非空"""
    if not isinstance(variant_str, str):
        return False
    value = variant_str.strip()
    if not value:
        return True

    parts = value.split("|||")
    if len(parts) > max_segments:
        return False
    for part in parts:
        if "^" not in part:
            return False
        attribute, _, values_part = part.partition("^")
        if not attribute.strip():
            return False
        for v in values_part.split("#"):
            if not v.strip():
                return False
    return True
