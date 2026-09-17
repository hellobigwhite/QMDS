"""Shopify Sitemap 兜底通道 — 商品链接发现与商品页兜底解析

启用场景（由 ProductCrawler 判定，其他情况仍走 products.json 分页）：

1. 店铺的 /products.json 被禁用（403 / 非 JSON / 无响应）
2. meta.json 的 published_products_count 达到 products.json 上限
   （25000），说明分页接口已无法覆盖全店商品

通道流程::

    /sitemap.xml → sitemapindex → /sitemap_products_1.xml → /products/<handle>

本模块只负责「发现链接」和「商品页兜底解析」，不关心传输通道：
调用方传入 fetcher(url, timeout) -> Optional[bytes]，由 ProductCrawler 决定
走 cloudscraper 直连、远程代理服务还是本地代理池。
"""

import gzip
import json
import re
from typing import Callable, List, Optional
from urllib.parse import urlparse

from xml.etree import ElementTree as ET

from qmds.utils.logger import get_logger

log = get_logger("sitemap_fetcher")

# 单次 sitemap 请求超时
SITEMAP_TIMEOUT = 20
# 单个店铺最多解析多少个子 sitemap（防失控）
MAX_SITEMAPS = 200
# 单个店铺最多发现多少商品链接（防失控）
MAX_PRODUCT_URLS = 60000

# 多语言路径段：fr / en-us / pt-br 等
LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(-[a-z0-9]{2,4})?$", re.IGNORECASE)

# JSON-LD 中代表商品的 @type
_PRODUCT_TYPES = {"product", "productgroup"}


# ── XML 工具 ──────────────────────────────────────────────

def _localname(tag) -> str:
    """去掉命名空间前缀，返回小写标签名"""
    text = str(tag)
    if "}" in text:
        text = text.rsplit("}", 1)[-1]
    return text.lower()


def parse_sitemap_xml(content: Optional[bytes]) -> Optional[ET.Element]:
    """解析 sitemap 内容，自动处理 .gz 压缩包"""
    if not content:
        return None
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError as exc:
            log.debug(f"sitemap gzip 解压失败: {exc}")
            return None
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        log.debug(f"sitemap XML 解析失败: {exc}")
        return None


def _collect_locs(root: ET.Element, parent: str) -> List[str]:
    """收集 <sitemap><loc> 或 <url><loc> 的文本"""
    out: List[str] = []
    for el in root.iter():
        if _localname(el.tag) != parent:
            continue
        for child in el:
            if _localname(child.tag) == "loc":
                text = (child.text or "").strip()
                if text:
                    out.append(text)
    return out


def is_locale_prefixed(url: str) -> bool:
    """判断 sitemap URL 是否位于多语言子路径下（如 /fr/sitemap_products_1.xml）

    这类子 sitemap 是同一批商品的翻译版本，解析会造成大量重复。
    """
    path = urlparse(url).path
    lowered = path.lower()
    idx = lowered.find("/sitemap")
    if idx <= 0:
        return False
    prefix = path[:idx]
    segments = [s for s in prefix.split("/") if s]
    if not segments:
        return False
    return all(LOCALE_SEGMENT_RE.match(s) for s in segments)


def _looks_like_product_sitemap(url: str) -> bool:
    """路径同时含 sitemap 与 product 即视为产品 sitemap

    覆盖 Shopify 的 /sitemap_products_1.xml、以及主题自定的
    /sitemap.xml_products、/products-sitemap.xml 等写法。
    """
    path = urlparse(url).path.lower()
    return "sitemap" in path and "product" in path


# ── 链接发现 ──────────────────────────────────────────────

def discover_product_sitemaps(base_url: str, fetcher: Callable, stop_event=None) -> List[str]:
    """从 /sitemap.xml 发现产品 sitemap 列表

    Args:
        base_url: 店铺根 URL（如 https://store.com）
        fetcher: 传输回调 (url, timeout) -> Optional[bytes]
        stop_event: 可选停止信号

    Returns:
        产品 sitemap URL 列表；发现不到时返回空列表
    """
    base = base_url.rstrip("/")
    candidates = [f"{base}/sitemap.xml"]
    netloc = urlparse(base).netloc
    if not netloc.startswith("www."):
        candidates.append(f"{urlparse(base).scheme}://www.{netloc}/sitemap.xml")

    for sitemap_url in candidates:
        if stop_event is not None and stop_event.is_set():
            return []
        root = parse_sitemap_xml(fetcher(sitemap_url, SITEMAP_TIMEOUT))
        if root is None:
            continue

        root_tag = _localname(root.tag)
        if root_tag == "sitemapindex":
            found = []
            for loc in _collect_locs(root, "sitemap"):
                if not _looks_like_product_sitemap(loc):
                    continue
                if is_locale_prefixed(loc):
                    log.debug(f"跳过多语言 sitemap: {loc}")
                    continue
                found.append(loc)
            if found:
                log.info(f"sitemap 索引发现 {len(found)} 个产品 sitemap: {base}")
                return found[:MAX_SITEMAPS]
            log.info(f"sitemap 索引中无产品 sitemap: {sitemap_url}")
            return []

        if root_tag == "urlset":
            locs = _collect_locs(root, "url")
            if any("/products/" in u for u in locs):
                log.info(f"sitemap 直接包含商品链接: {sitemap_url}")
                return [sitemap_url]
            return []

    log.info(f"无法获取 sitemap: {base}")
    return []


def extract_product_urls(sitemap_url: str, fetcher: Callable) -> List[str]:
    """从产品 sitemap 中提取 /products/ 链接"""
    root = parse_sitemap_xml(fetcher(sitemap_url, SITEMAP_TIMEOUT))
    if root is None:
        log.debug(f"产品 sitemap 解析失败: {sitemap_url}")
        return []
    urls: List[str] = []
    for loc in _collect_locs(root, "url"):
        if "/products/" in loc:
            urls.append(loc)
            if len(urls) >= MAX_PRODUCT_URLS:
                break
    log.debug(f"产品 sitemap {sitemap_url} 提取到 {len(urls)} 个商品链接")
    return urls


def collect_product_urls(base_url: str, fetcher: Callable, *, stop_event=None,
                         progress_callback=None) -> List[str]:
    """发现并汇总一个店铺的全部商品链接（去重）"""
    sitemaps = discover_product_sitemaps(base_url, fetcher, stop_event=stop_event)
    if not sitemaps:
        return []

    urls: List[str] = []
    seen = set()
    for idx, sitemap_url in enumerate(sitemaps, 1):
        if stop_event is not None and stop_event.is_set():
            break
        for url in extract_product_urls(sitemap_url, fetcher):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= MAX_PRODUCT_URLS:
                log.warning(f"商品链接数达到上限 {MAX_PRODUCT_URLS}，停止发现: {base_url}")
                if progress_callback:
                    progress_callback(f"[sitemap] 商品链接达到上限 {MAX_PRODUCT_URLS}")
                return urls
        if progress_callback and (idx % 5 == 0 or idx == len(sitemaps)):
            progress_callback(f"[sitemap] 已解析 {idx}/{len(sitemaps)} 个 sitemap，累计 {len(urls)} 个商品链接")

    log.info(f"sitemap 发现 {len(urls)} 个商品链接: {base_url}")
    return urls


# ── 商品页 JSON-LD 兜底解析 ───────────────────────────────

def _iter_jsonld_products(data):
    """在 JSON-LD 结构里迭代所有 Product 节点（兼容 @graph / 数组嵌套）"""
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_products(item)
        return
    if not isinstance(data, dict):
        return
    node_type = data.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(str(t).lower() in _PRODUCT_TYPES for t in types if t):
        yield data
    graph = data.get("@graph")
    if isinstance(graph, (list, dict)):
        yield from _iter_jsonld_products(graph)


def _first_image_src(value) -> str:
    """JSON-LD 的 image 字段可能是 str / list / {"url": ...} / ImageObject 数组"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or value.get("contentUrl") or "").strip()
    if isinstance(value, list):
        for item in value:
            src = _first_image_src(item)
            if src:
                return src
    return ""


def _extract_offer(offers):
    """从 offers（dict 或 list）中取价格与 SKU 相关字段"""
    if isinstance(offers, list):
        for item in offers:
            if isinstance(item, dict):
                return item
        return {}
    if isinstance(offers, dict):
        return offers
    return {}


def jsonld_to_product(node: dict) -> Optional[dict]:
    """把 JSON-LD Product 节点转换成 Shopify products.json 的商品结构

    转换后可直接交给 ProductCrawler 的解析逻辑，无需单独一套字段映射。
    JSON-LD 没有 compare_at_price（原价），该字段置空即可。
    """
    title = str(node.get("name") or node.get("headline") or "").strip()
    description = str(node.get("description") or "").strip()
    if not title or not description:
        return None

    image = _first_image_src(node.get("image"))
    offer = _extract_offer(node.get("offers"))
    price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
    sku = str(node.get("sku") or offer.get("sku") or "").strip()
    category = node.get("category")
    if isinstance(category, list):
        category = category[0] if category else ""
    product_type = str(category or "").strip()

    return {
        "title": title,
        "body_html": description,
        "images": [{"src": image}] if image else [],
        "variants": [{"price": price, "compare_at_price": None, "sku": sku}],
        "options": [],
        "product_type": product_type,
    }


def extract_jsonld_product(html: str) -> Optional[dict]:
    """从商品页 HTML 的 JSON-LD 中兜底解析商品数据

    仅在 <url>.json 同样不可用时使用。
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:  # pragma: no cover - bs4 为项目必备依赖
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in _iter_jsonld_products(data):
            product = jsonld_to_product(node)
            if product:
                return product
    return None
