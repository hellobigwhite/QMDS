"""
Shopify 导航栏解析模块
解析店铺首页的导航菜单，提取两级分类结构
"""
import logging
import random
import re
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


NAV_SELECTORS = [
    {"menu": "nav.header__inline-menu", "item": "li", "link": "a", "children": "ul"},
    {"menu": "nav#AccessibleNav", "item": "li", "link": "a", "children": "ul"},
    {"menu": "nav[role='navigation']", "item": "li", "link": "a", "children": "ul"},
    {"menu": "ul.nav", "item": "li", "link": "a", "children": "ul"},
    {"menu": "header nav", "item": "li", "link": "a", "children": "ul"},
    {"menu": ".nav", "item": "li", "link": "a", "children": "ul"},
    {"menu": "nav", "item": "li", "link": "a", "children": "ul"},
    {"menu": "[class*='menu']", "item": "li", "link": "a", "children": "ul"},
]

GARBAGE_KEYWORDS = [
    "all", "all products", "全部", "全部商品",
    "new", "new arrivals", "新品",
    "sale", "sale", "特价", "促销",
    "frontpage", "home", "首页",
    "shop", "store", "gift card", "giftcard",
    "blog", "news", "about", "contact", "faq",
    "search", "login", "register", "cart",
    "wishlist", "wish list",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]

_logger = logging.getLogger("shopify_nav_parser")


def _log(msg: str):
    _logger.info(msg)


def _is_garbage(title: str) -> bool:
    title_lower = title.lower().strip()
    if not title_lower:
        return True
    for kw in GARBAGE_KEYWORDS:
        if title_lower == kw or title_lower.startswith(kw + " ") or title_lower.startswith(kw + "-"):
            return True
    return False


def _extract_collection_handle(url: str) -> Optional[str]:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/collections/([^/]+)", path)
    if match:
        return match.group(1)
    return None


def _fetch_soup(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        _log(f"导航解析请求失败 {url}: {exc}")
    return None


def _parse_nav_html(url: str, soup: BeautifulSoup) -> List[Tuple[str, str, str, str]]:
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    for selector in NAV_SELECTORS:
        menu_el = soup.select_one(selector["menu"])
        if not menu_el:
            continue

        items = menu_el.find_all(selector["item"], recursive=False)
        if not items:
            container = menu_el.find(["ul", "ol"])
            if container:
                items = container.find_all(selector["item"], recursive=False)
        if not items:
            continue

        nav_items = []
        for item in items:
            link = item.find(selector["link"]) if selector["link"] else item
            if not link:
                continue
            href = link.get("href") or ""
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            level1_name = link.get_text(strip=True)
            if not level1_name or _is_garbage(level1_name):
                continue
            full_href = urljoin(base_url, href)

            children_container = item.find(selector["children"]) if selector["children"] else None
            if children_container:
                child_links = children_container.find_all(selector["link"]) if selector["link"] else []
                for child in child_links:
                    child_href = child.get("href") or ""
                    child_href = urljoin(base_url, child_href)
                    child_name = child.get_text(strip=True)
                    if not child_name or not child_href or child_href == "#":
                        continue
                    handle = _extract_collection_handle(child_href)
                    if handle:
                        nav_items.append((level1_name, child_name, child_href, handle))
                    else:
                        nav_items.append((level1_name, child_name, child_href, ""))
            else:
                handle = _extract_collection_handle(full_href)
                if handle:
                    nav_items.append((level1_name, level1_name, full_href, handle))

        if nav_items:
            _log(f"导航解析成功: {len(nav_items)} 个分类项 (选择器: {selector['menu']})")
            return nav_items

    _log("导航 HTML 解析未找到菜单元素")
    return []


def parse_navigation(url: str) -> List[Tuple[str, str, str, str]]:
    """
    解析 Shopify 店铺导航栏，返回两级分类列表

    返回: [(level1, level2, collection_url, handle), ...]
    - level1: 一级分类名（导航栏顶层）
    - level2: 二级分类名（导航栏子层，无子层时与 level1 相同）
    - collection_url: 完整的集合 URL
    - handle: 集合 handle（空表示非集合页面）
    """
    url = url.rstrip("/")
    soup = _fetch_soup(url)
    if soup is None:
        _log("首页获取失败，回退到 collections.json")
        return _collections_fallback(url)

    result = _parse_nav_html(url, soup)
    if result:
        return result

    _log("导航解析为空，回退到 collections.json")
    return _collections_fallback(url)


def _collections_fallback(url: str) -> List[Tuple[str, str, str, str]]:
    """兜底：从 collections.json 获取分类，用分隔符推断两级"""
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(f"{url}/collections.json?limit=250", headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        collections = data.get("collections", [])
    except Exception as exc:
        _log(f"collections.json 回退失败: {exc}")
        return []

    result = []
    for c in collections:
        title = (c.get("title") or "").strip()
        handle = c.get("handle") or ""
        if not title or not handle or _is_garbage(title):
            continue
        coll_url = f"{base_url}/collections/{handle}"
        parts = re.split(r"\s*[>\|–—-]\s*", title, maxsplit=1)
        if len(parts) == 2:
            result.append((parts[0].strip(), parts[1].strip(), coll_url, handle))
        else:
            result.append((title, title, coll_url, handle))
    _log(f"collections.json 回退: {len(result)} 个分类")
    return result


def build_product_category_map(
    url: str,
    nav_items: List[Tuple[str, str, str, str]],
    fetcher: Optional[Callable] = None,
) -> dict:
    """
    根据导航分类列表，获取每个集合下的商品 ID，建立 product_id → (level1, level2) 映射

    参数:
        url: 店铺基础 URL
        nav_items: parse_navigation() 返回的分类列表
        fetcher: 自定义请求函数，默认使用 requests.get

    返回:
        {product_id_str: (level1, level2), ...}
    """
    product_map = {}
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    for level1, level2, coll_url, handle in nav_items:
        if not handle:
            continue
        products_url = f"{base_url}/collections/{handle}/products.json?limit=250"
        page = 1
        while True:
            try:
                page_url = f"{products_url}&page={page}"
                if fetcher:
                    data_dict, status, _ = fetcher(page_url, timeout=15, max_retries=2)
                    data = data_dict if isinstance(data_dict, dict) else {}
                else:
                    headers = {"User-Agent": random.choice(USER_AGENTS)}
                    resp = requests.get(page_url, headers=headers, timeout=15)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
            except Exception:
                break

            products = data.get("products", []) if isinstance(data, dict) else []
            if not products:
                break

            for p in products:
                pid = str(p.get("id") or "")
                if pid and pid not in product_map:
                    product_map[pid] = (level1, level2)

            if len(products) < 250:
                break
            page += 1

    _log(f"商品分类映射完成: {len(product_map)} 个商品")
    return product_map
