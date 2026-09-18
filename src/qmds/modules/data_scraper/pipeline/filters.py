"""商品过滤器"""

import re
from typing import Callable

from qmds.config.categories import is_adult_category
from qmds.modules.data_scraper.models.schemas import Product
from qmds.utils.language import is_non_english_text

PLACEHOLDER_IMAGES = re.compile(
    r"(coming[\s_\-\.]*soon|no[\s_\-\.]*image|placeholder|\.svg|logo|"
    r"default|missing|empty|blank|unavailable|not[\s_\-]*available|"
    r"sample|demo|temp|dummy|generic|stock[\s_\-]*photo)",
    re.IGNORECASE
)
# ── 违禁词表 ─────────────────────────────────────────────
# 按大类拆分成独立常量：成人（Mature）关键词单独成表，成人类目清洗时跳过
# （成人用品的标题/描述天然含 lingerie / vibrator / adult 等词，属正常表达）。
# 武器/药品/赌博/假货等关键词对所有类目（含成人类目）一律生效。

_WEAPON_KEYWORDS = [
    # 武器类
    "weapon", "weapons", "gun", "guns", "firearm", "firearms", "rifle", "rifles",
    "shotgun", "shotguns", "pistol", "pistols", "ammo", "ammunition", "bullet",
    "bullets", "explosive", "explosives", "bomb", "bombs", "grenade", "grenades",
    "knife", "knives", "dagger", "daggers", "sword", "swords", "blade", "blades",
    "brass knuckles", "knuckle", "crossbow", "taser", "stun gun",
]

_DRUG_KEYWORDS = [
    # 药品类
    "drug", "drugs", "narcotic", "narcotics", "opioid", "opioids", "cocaine",
    "heroin", "methamphetamine", "meth", "lsd", "ecstasy", "mdma", "fentanyl",
    "cannabis", "marijuana", "weed", "thc", "cbd", "steroid", "steroids",
    "ivermectin", "pill", "pills", "prescription drug",
]

# 成人（Mature）类目关键词：仅对非成人类目生效
ADULT_KEYWORDS = [
    # 色情类
    "adult", "porn", "porno", "pornography", "xxx", "sex toy", "sex toys",
    "vibrator", "lingerie", "erotic", "nude", "naked", "booty", "onlyfans",
    "nsfw", "fetish", "bdsm", "escort", "escorts", "prostitut",
]

_GAMBLING_KEYWORDS = [
    # 赌博类
    "gambling", "gamble", "casino", "poker", "slot machine", "betting",
    "lottery", "jackpot", "wager", "bookie", "sportsbook",
]

_COUNTERFEIT_KEYWORDS = [
    # 假货/诈骗类
    "counterfeit", "counterfeits", "replica", "replicas", "fake", "fakes",
    "knockoff", "knockoffs", "imitation", "imitations", "bootleg", "pirated",
    "forged", "forgery", "scam", "fraud", "phishing",
]

_OTHER_PROHIBITED_KEYWORDS = [
    # 其他违禁
    "tobacco", "cigarette", "cigarettes", "cigar", "vape", "e-cigarette",
    "lock pick", "lockpick", "spy camera", "hidden camera", "wiretap",
    "human growth hormone", "hgh", "dnp", "2,4-dinitrophenol",
]

# 非成人违禁词：所有类目（含成人类目）都生效
NON_ADULT_KEYWORDS = (
    _WEAPON_KEYWORDS + _DRUG_KEYWORDS + _GAMBLING_KEYWORDS
    + _COUNTERFEIT_KEYWORDS + _OTHER_PROHIBITED_KEYWORDS
)

# 全量违禁词（默认）：顺序与内容与拆分前完全一致，供无法判定类目的场景使用
PROHIBITED_KEYWORDS = (
    _WEAPON_KEYWORDS + _DRUG_KEYWORDS + ADULT_KEYWORDS + _GAMBLING_KEYWORDS
    + _COUNTERFEIT_KEYWORDS + _OTHER_PROHIBITED_KEYWORDS
)


def get_prohibited_keywords(category: str = "") -> list:
    """按一级分类返回本次生效的违禁词表

    成人类目（mature / 成人 / adult / 772 / 13）跳过成人关键词：成人用品标题
    天然包含 lingerie / vibrator / adult 等词，不应按违禁处理；
    武器、药品、赌博、假货等其余违禁词对成人类目照常生效。

    Args:
        category: 一级分类名；为空或无法判定时返回全量违禁词表

    Returns:
        生效的违禁词列表
    """
    return NON_ADULT_KEYWORDS if is_adult_category(category) else PROHIBITED_KEYWORDS

MIN_TITLE_LENGTH = 5
MIN_DESCRIPTION_LENGTH = 10
MIN_PRICE = 3.0
MAX_PRICE = 3000.0


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
    def has_prohibited_content(p: Product, category: str = "") -> bool:
        """检测商品是否命中违禁词

        Args:
            p: 商品
            category: 一级分类名；传成人类目（mature/成人）时跳过成人关键词，
                传空字符串（默认）时按全量违禁词表判定
        """
        text = f"{p.title} {p.body_html} {' '.join(p.tags)}".lower()
        return any(kw in text for kw in get_prohibited_keywords(category))

    @staticmethod
    def _is_english(p: Product) -> bool:
        """检测商品是否为英文"""
        text = f"{p.title} {p.body_html}"
        return not is_non_english_text(text)
