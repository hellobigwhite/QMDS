"""Shopify 网站分类器 - 判断专一站/综合站并输出主营类目

基于 meta.json 和 collections.json 进行分类判断。
独立模块，可单独提取使用。
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from qmds.config.categories import SHOPIFY_CATEGORIES, match_category_extra_phrases
from qmds.config.settings import settings
from qmds.utils.http_client import HttpClient
from qmds.utils.language import is_non_english_text
from qmds.utils.logger import get_logger

log = get_logger("site_classifier")

# ── 常量 ──────────────────────────────────────────────────

CATEGORY_TXT_DIR = Path(__file__).resolve().parents[1] / "data" / "categories"

CATEGORY_TO_TXT: dict[str, str] = {
    "animals_pet_supplies": "Animals & Pet Supplies.txt",
    "apparel_accessories": "Apparel & Accessories.txt",
    "arts_entertainment": "Arts & Entertainment.txt",
    "baby_toddler": "Baby & Toddler.txt",
    "business_industrial": "Business & Industrial.txt",
    "cameras_optics": "Cameras & Optics.txt",
    "electronics": "Electronics.txt",
    "food_beverages_tobacco": "Food, Beverages & Tobacco.txt",
    "furniture": "Furniture.txt",
    "hardware": "Hardware.txt",
    "health_beauty": "Health & Beauty.txt",
    "home_garden": "Home & Garden.txt",
    "luggage_bags": "Luggage & Bags.txt",
    "mature": "Mature.txt",
    "media": "Media.txt",
    "office_supplies": "Office Supplies.txt",
    "religious_ceremonial": "Religious & Ceremonial.txt",
    "software": "Software.txt",
    "sporting_goods": "Sporting Goods.txt",
    "toys_games": "Toys & Games.txt",
    "vehicles_parts": "Vehicles & Parts.txt",
}

ENGLISH_COUNTRIES = {"US", "GB", "CA", "AU", "NZ", "IE", "SG", "ZA"}


# ── 标准化函数 ─────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """标准化文本：小写、去空格符号、去复数"""
    if not text:
        return ""

    # 小写并去除首尾空格
    text = text.lower().strip()

    # 去除空格、连字符、下划线、&符号及所有特殊字符（只保留字母和数字）
    text = re.sub(r'[^a-z0-9]', '', text)

    # 去除常见复数（保守处理）
    if len(text) > 4:
        # 以 "ies" 结尾 → 去掉 "ies" 加 "y" (cities → city)
        if text.endswith('ies'):
            text = text[:-3] + 'y'
        # 以 "ses", "xes", "zes", "ches", "shes" 结尾 → 去掉 "es"
        elif text.endswith(('ses', 'xes', 'zes', 'ches', 'shes')):
            text = text[:-2]
        # 以 "es" 结尾 → 去掉 "s" (保留 e)
        elif text.endswith('es') and len(text) > 4:
            text = text[:-1]
        # 以 "s" 结尾且不是 "ss" → 去掉 "s"
        elif text.endswith('s') and not text.endswith('ss'):
            text = text[:-1]

    return text


# ── 词库加载 ───────────────────────────────────────────────

@lru_cache(maxsize=32)
def load_category_leaves(category: str) -> set[str]:
    """加载类目词库的所有关键词，返回标准化后的词集合

    提取每一行的所有层级的词，而不仅仅是叶子节点。
    例如 "Electronics > Audio > Speakers" 提取出 {"electronics", "audio", "speakers"}
    同时也提取类目名称本身的关键词。
    """
    txt_filename = CATEGORY_TO_TXT.get(category)
    if not txt_filename:
        return set()

    txt_path = CATEGORY_TXT_DIR / txt_filename
    if not txt_path.exists():
        return set()

    keywords: set[str] = set()

    # 提取类目名称本身的关键词
    # 例如 "Apparel & Accessories" -> {"apparel", "accessory"}
    cat_name = txt_filename.replace(".txt", "")
    for part in re.split(r'[\s&]+', cat_name):
        normalized = normalize_text(part)
        if normalized and len(normalized) >= 2:
            keywords.add(normalized)

    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # 提取所有层级的词
            parts = [p.strip() for p in line.split(">")]
            for part in parts:
                # 标准化后加入集合
                normalized = normalize_text(part)
                if normalized and len(normalized) >= 2:
                    keywords.add(normalized)

    log.debug(f"类目 {category}: 加载了 {len(keywords)} 个关键词")
    return keywords


# ── 数据结构 ───────────────────────────────────────────────

@dataclass
class ClassificationResult:
    """单个网站的分类结果"""

    url: str
    site_type: str  # "niche" / "general" / "unknown"
    primary_category: str  # 主营类目（如 "electronics"），综合站为空
    category_distribution: dict[str, float]  # {类目名: 占比}
    confidence: float  # 0.0 ~ 1.0
    is_english: bool = True  # 是否英文网站
    signals_used: list[str] = field(default_factory=list)


# ── 分类器 ─────────────────────────────────────────────────

class SiteClassifier:
    """Shopify 网站分类器"""

    def __init__(
        self,
        http_client: Optional[HttpClient] = None,
        niche_threshold: int = 10,  # 主类目匹配次数阈值
        other_threshold: int = 3,   # 其他类目匹配次数阈值
        use_proxy: bool = True,     # 是否使用代理
    ):
        self.http = http_client or HttpClient()
        self.niche_threshold = niche_threshold
        self.other_threshold = other_threshold
        self.use_proxy = use_proxy

    def _create_thread_http(self) -> HttpClient:
        """为线程创建独立的 HttpClient 实例（带代理）"""
        if self.use_proxy:
            try:
                from qmds.utils.proxy_manager import ProxyManager
                pm = ProxyManager.from_settings()
                return HttpClient(proxy_manager=pm)
            except Exception:
                pass
        return HttpClient()

    # ── 公开接口 ──────────────────────────────────────────

    def classify(self, url: str) -> ClassificationResult:
        """分类单个网站"""
        url = self._normalize_url(url)

        log.debug(f"开始分类: {url}")

        # 1. 获取 meta.json
        meta = self._fetch_meta(url)
        if not meta:
            log.debug(f"  meta.json 获取失败")
            return self._unknown_result(url)

        # 2. 判断是否英文网站
        if not self._is_english_site(meta):
            log.debug(f"  非英文网站")
            return ClassificationResult(
                url=url,
                site_type="unknown",
                primary_category="",
                category_distribution={},
                confidence=0.0,
                is_english=False,
                signals_used=["meta"],
            )

        # 3. 获取 collections
        collections = self._fetch_collections(url)

        # 4. 统计匹配次数
        counts = {cat: 0 for cat in SHOPIFY_CATEGORIES}

        # 匹配 meta.description
        desc = meta.get("description", "")
        if desc:
            matched_cats = self._match_text_to_all_categories(desc)
            for cat in matched_cats:
                counts[cat] += 1
            if matched_cats:
                log.debug(f"  meta.description 匹配: {matched_cats}")

        # 匹配 collection.title
        for c in collections:
            title = c.get("title", "")
            if not title:
                continue
            matched_cats = self._match_text_to_all_categories(title)
            for cat in matched_cats:
                counts[cat] += 1

        # 5. 判断类型
        site_type, primary_category = self._determine_type(counts)

        # 计算分布
        distribution = self._calc_distribution(counts)

        # 计算置信度
        if site_type == "niche":
            top_count = max(counts.values())
            other_count = sum(counts.values()) - top_count
            confidence = min(top_count / 20, 1.0)  # 20次以上满置信度
        elif site_type == "general":
            confidence = 0.7
        else:
            confidence = 0.0

        log.debug(f"  结果: {site_type}, 类目: {primary_category or '-'}")

        return ClassificationResult(
            url=url,
            site_type=site_type,
            primary_category=primary_category,
            category_distribution=distribution,
            confidence=confidence,
            is_english=True,
            signals_used=["meta", "collections"],
        )

    def classify_from_excel(
        self,
        file_path: str,
        url_column: str = "domain",
        max_workers: int = 10,
    ) -> dict:
        """批量分类 Excel 中的网站，添加 Type 字段并保存到原文件"""
        log.info(f"开始批量分类: {file_path}, 线程数: {max_workers}")

        df = pd.read_excel(file_path, engine="openpyxl")

        if url_column not in df.columns:
            raise ValueError(f"列 '{url_column}' 不存在，可用列: {list(df.columns)}")

        total = len(df)
        log.info(f"共 {total} 条记录待处理")

        # 准备任务列表
        tasks = []
        for idx, row in df.iterrows():
            raw_url = str(row[url_column]).strip()
            if not raw_url or raw_url == "nan":
                tasks.append((idx, None))
            else:
                tasks.append((idx, raw_url))

        # 多线程处理
        results: list[ClassificationResult] = [None] * total
        completed_count = 0

        def process_one(idx: int, url: str) -> tuple[int, ClassificationResult]:
            if url is None:
                return idx, ClassificationResult(
                    url="", site_type="unknown", primary_category="",
                    category_distribution={}, confidence=0.0, is_english=False,
                )
            # 为每个线程创建独立的 HttpClient（带代理）
            thread_http = self._create_thread_http()
            thread_classifier = SiteClassifier(
                http_client=thread_http,
                niche_threshold=self.niche_threshold,
                other_threshold=self.other_threshold,
                use_proxy=False,  # 已经创建了带代理的 HttpClient
            )
            result = thread_classifier.classify(url)
            return idx, result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_one, idx, url): (idx, url)
                for idx, url in tasks
            }

            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                completed_count += 1

                url = futures[future][1]
                if url is None:
                    log.debug(f"[{completed_count}/{total}] 跳过空域名")
                elif not result.is_english:
                    log.info(f"[{completed_count}/{total}] {url} -> 非英文，将删除")
                else:
                    log.info(f"[{completed_count}/{total}] {url} -> {result.site_type}, {result.primary_category or '-'}")

        # 添加 Type 字段
        def get_type_label(r: ClassificationResult) -> str:
            if not r.is_english:
                return "unknown"
            if r.site_type == "niche" and r.primary_category:
                return r.primary_category
            return r.site_type

        df["Type"] = [get_type_label(r) for r in results]

        # 过滤非英文网站
        english_mask = [r.is_english for r in results]
        non_english_count = sum(1 for is_en in english_mask if not is_en)
        df = df[english_mask].reset_index(drop=True)

        # 保存到原文件
        df.to_excel(file_path, index=False, engine="openpyxl")
        log.info(f"已保存到原文件: {file_path}")

        # 统计结果
        niche_count = sum(1 for r in results if r.is_english and r.site_type == "niche")
        general_count = sum(1 for r in results if r.is_english and r.site_type == "general")
        unknown_count = sum(1 for r in results if r.is_english and r.site_type == "unknown")

        stats = {
            "total": total,
            "niche": niche_count,
            "general": general_count,
            "unknown": unknown_count,
            "non_english": non_english_count,
            "file_path": file_path,
        }

        log.info(f"分类完成: 总计 {total}, 专一 {niche_count}, 综合 {general_count}, 未知 {unknown_count}, 非英文 {non_english_count}")
        return stats

    # ── 数据获取（私有） ──────────────────────────────────

    def _fetch_meta(self, url: str) -> dict:
        """获取 meta.json"""
        try:
            resp = self.http.get(f"{url}/meta.json", timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.debug(f"meta.json 请求失败: {url} - {e}")
        return {}

    def _fetch_collections(self, url: str) -> list[dict]:
        """获取 collections.json（第一页）"""
        try:
            resp = self.http.get(f"{url}/collections.json?limit=250", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("collections", [])
        except Exception as e:
            log.debug(f"collections.json 请求失败: {url} - {e}")
        return []

    # ── 语言判断（私有） ──────────────────────────────────

    def _is_english_site(self, meta: dict) -> bool:
        """判断是否英文网站"""
        # 有 country 字段
        country = meta.get("country", "")
        if country:
            return country in ENGLISH_COUNTRIES

        # 无 country 字段，检查 description
        desc = meta.get("description", "")
        if not desc:
            return True  # 无描述默认为英文

        return not is_non_english_text(desc)

    # ── 类目匹配（私有） ──────────────────────────────────

    def _match_text_to_all_categories(self, text: str) -> list[str]:
        """完全匹配 text 到所有匹配的类目，返回匹配到的类目列表"""
        if not text:
            return []

        normalized = normalize_text(text)
        if not normalized or len(normalized) < 2:
            return []

        matched = []
        for cat in SHOPIFY_CATEGORIES:
            leaves = load_category_leaves(cat)
            if normalized in leaves:
                matched.append(cat)
            elif match_category_extra_phrases(cat, text):
                # taxonomy 未收录但业内属于该类目的商品（如宗教珠宝：
                # 十字架项链/念珠/圣牌），短语级匹配避免通用词误判
                matched.append(cat)

        return matched

    # ── 类型判断（私有） ──────────────────────────────────

    def _determine_type(self, counts: dict[str, int]) -> tuple[str, str]:
        """根据匹配次数判断类型

        Returns:
            (site_type, primary_category)
        """
        # 按次数降序排序
        sorted_cats = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        top_cat, top_count = sorted_cats[0]
        other_count = sum(c for _, c in sorted_cats[1:])

        # 主类目 >= 10 且 其他类目 < 3
        if top_count >= self.niche_threshold and other_count < self.other_threshold:
            return "niche", top_cat

        # 有任何匹配
        if top_count > 0:
            return "general", ""

        return "unknown", ""

    def _calc_distribution(self, counts: dict[str, int]) -> dict[str, float]:
        """计算类目分布（归一化）"""
        total = sum(counts.values())
        if total == 0:
            return {}
        return {k: round(v / total, 4) for k, v in counts.items() if v > 0}

    # ── 工具方法 ──────────────────────────────────────────

    def _unknown_result(self, url: str) -> ClassificationResult:
        """返回 unknown 结果"""
        return ClassificationResult(
            url=url,
            site_type="unknown",
            primary_category="",
            category_distribution={},
            confidence=0.0,
            is_english=True,
            signals_used=[],
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        """标准化 URL 格式"""
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url
