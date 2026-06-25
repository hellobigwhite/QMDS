"""产品数据爬取模块 - 基于导航的深度爬取"""

import re
import time
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from qmds.config import settings
from qmds.db.mongodb import MongoDBClient
from qmds.db.product_db import ProductDBClient
from qmds.modules.data_scraper.shopify_nav_parser import parse_navigation
from qmds.utils.logger import get_logger

log = get_logger("product_crawler")

# 请求配置
REQUEST_TIMEOUT = 25
MAX_PAGE_LIMIT = 100
MAX_EMPTY_PAGES = 3
PAGE_SLEEP_RANGE = (1.5, 3.5)
SITE_COOLDOWN_RANGE = (6, 12)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def normalize_url(url: str) -> str:
    """标准化URL"""
    text = str(url or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text.rstrip("/")


def get_domain(url: str) -> str:
    """获取域名"""
    return urlparse(url).netloc.replace("www.", "").lower()


def product_unique_key(domain: str, product_id: str, title: str, image: str) -> str:
    """生成商品唯一标识"""
    normalized_id = str(product_id or "").strip()
    if normalized_id:
        return f"{domain}|||id|||{normalized_id}"
    return f"{domain}|||fallback|||{str(title or '').strip().lower()}|||{str(image or '').strip().lower()}"


def convert_price(value, rate):
    """转换价格"""
    try:
        if value in ("", None):
            return ""
        return round(float(value) * float(rate), 2)
    except Exception:
        return ""


def extract_images(images):
    """提取主图"""
    if not isinstance(images, list):
        return ""
    for item in images:
        if isinstance(item, dict):
            src = str(item.get("src") or "").strip()
            if src:
                return src.split("?")[0]
    return ""


def extract_variant_info(variants, options):
    """提取变体信息"""
    sku = ""
    variant_parts = []

    if isinstance(variants, list) and variants:
        sku = str(variants[0].get("sku") or "").strip()

    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            name = str(opt.get("name") or "").strip()
            values = opt.get("values") or []
            if name and name != "Title":
                variant_parts.append(f"{name}^{'#'.join(map(str, values))}")

    return sku, "|||".join(variant_parts)


def extract_prices(variants):
    """提取价格"""
    if not isinstance(variants, list) or not variants:
        return "", ""
    first_variant = variants[0]
    return first_variant.get("compare_at_price", ""), first_variant.get("price", "")


class ProductCrawler:
    """产品数据爬取器"""
    
    def __init__(self, currency_map: Dict[str, float], proxies: Optional[List[str]] = None):
        self.currency_map = currency_map
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
        })
        
        # 代理配置
        self.proxies = proxies or []
        self.proxy_index = 0
    
    def get_next_proxy(self) -> Optional[str]:
        """获取下一个代理"""
        if not self.proxies:
            return None
        proxy = self.proxies[self.proxy_index % len(self.proxies)]
        self.proxy_index += 1
        return proxy
    
    def fetch_json(self, url: str, timeout: int = REQUEST_TIMEOUT) -> Tuple[Optional[dict], int]:
        """获取JSON数据"""
        for attempt in range(3):
            try:
                proxy = self.get_next_proxy()
                proxies = {"http": proxy, "https": proxy} if proxy else None
                
                response = self.session.get(url, timeout=timeout, proxies=proxies)
                return response.json(), response.status_code
            except Exception as e:
                if attempt == 2:
                    log.warning(f"请求失败 {url}: {e}")
                    return None, 0
                time.sleep(1)
        return None, 0
    
    def fetch_currency(self, url: str) -> str:
        """获取货币类型"""
        meta_url = f"{normalize_url(url)}/meta.json"
        data, status = self.fetch_json(meta_url, timeout=15)
        if status == 200 and isinstance(data, dict):
            currency = data.get("currency", "USD")
            return str(currency).upper() if currency else "USD"
        return ""
    
    def crawl_site(self, url: str, category: str, progress_callback=None) -> Dict:
        """爬取单个站点的商品数据
        
        Args:
            url: 站点URL
            category: 类目名称
            progress_callback: 进度回调函数
            
        Returns:
            {"success": bool, "products": list, "count": int}
        """
        url = normalize_url(url)
        domain = get_domain(url)
        
        try:
            if progress_callback:
                progress_callback(f"[{domain}] 开始爬取")
            
            # 获取货币
            if progress_callback:
                progress_callback(f"[{domain}] 获取货币...")
            currency = self.fetch_currency(url)
            if not currency:
                log.warning(f"[{domain}] 非 Shopify 站点（meta.json 无响应）")
                return {"success": False, "products": [], "count": 0, "error": "非 Shopify 站点"}
            
            rate = self.currency_map.get(currency)
            if rate is None:
                log.warning(f"[{domain}] 未找到汇率: {currency}")
                return {"success": False, "products": [], "count": 0, "error": f"无汇率配置: {currency}"}
            
            if progress_callback:
                progress_callback(f"[{domain}] 汇率 OK: {currency}，开始爬取商品")
            
            # 探针检测
            probe_url = f"{url}/products.json?limit=200&page=1"
            probe_data, probe_code = self.fetch_json(probe_url, timeout=15)
            
            if probe_code != 200 or not isinstance(probe_data, dict):
                log.warning(f"[{domain}] products.json 无响应")
                return {"success": False, "products": [], "count": 0, "error": "products.json 无响应"}
            
            products_count = len(probe_data.get("products", []))
            if products_count == 0:
                log.warning(f"[{domain}] products.json 返回空列表")
                return {"success": False, "products": [], "count": 0, "error": "无商品数据"}
            
            if progress_callback:
                progress_callback(f"[{domain}] 探针通过 ({products_count} 商品)")
            
            # 爬取所有页面
            all_products = []
            seen_unique_keys = set()
            page = 1
            empty_pages = 0
            
            while empty_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                products_url = f"{url}/products.json?limit=200&page={page}"
                data, code = self.fetch_json(products_url)
                
                if code != 200:
                    break
                
                products = data.get("products", []) if isinstance(data, dict) else []
                if not products:
                    empty_pages += 1
                    page += 1
                    time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
                    continue
                
                empty_pages = 0
                page_products = []
                
                for product in products:
                    if not isinstance(product, dict):
                        continue
                    
                    title = str(product.get("title") or "").strip()
                    desc = str(product.get("body_html") or "").strip()
                    if not title or not desc:
                        continue
                    
                    images = product.get("images", []) or []
                    variants = product.get("variants", []) or []
                    options = product.get("options", []) or []
                    product_type = str(product.get("product_type") or "").strip()
                    
                    image = extract_images(images)
                    sku, variant_str = extract_variant_info(variants, options)
                    compare_at_price, price = extract_prices(variants)
                    original_price = convert_price(compare_at_price, rate)
                    discount_price = convert_price(price, rate)
                    price_value = discount_price if discount_price != "" else original_price
                    
                    if price_value == "" or float(price_value) < 1:
                        continue
                    
                    product_id = str(product.get("id") or "").strip()
                    unique_key = product_unique_key(domain, product_id, title, image)
                    
                    if unique_key in seen_unique_keys:
                        continue
                    seen_unique_keys.add(unique_key)
                    
                    page_products.append({
                        "product_id": product_id,
                        "SKU": sku,
                        "标题": title,
                        "描述": desc,
                        "子描述": str(product.get("tags") or "").strip(),
                        "图片": image,
                        "原价": original_price,
                        "折扣价": discount_price,
                        "变体": variant_str,
                        "分类": product_type if product_type else category,
                        "currency": currency,
                        "source_url": url,
                        "source_domain": domain,
                        "source_category": category,
                        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "unique_key": unique_key,
                    })
                
                all_products.extend(page_products)
                
                if progress_callback and page % 5 == 0:
                    progress_callback(f"[{domain}] 第{page}页: 累计{len(all_products)}件")
                
                page += 1
                time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
            
            if progress_callback:
                progress_callback(f"[{domain}] 完成: {len(all_products)} 件商品")
            
            return {
                "success": True,
                "products": all_products,
                "count": len(all_products),
                "domain": domain,
                "currency": currency
            }
            
        except Exception as e:
            log.error(f"[{domain}] 爬取异常: {e}")
            return {"success": False, "products": [], "count": 0, "error": str(e)}
    
    def crawl_site_with_nav(self, url: str, category: str, progress_callback=None) -> Dict:
        """基于导航的深度爬取单个站点

        解析店铺导航栏获取分类结构，按集合逐类爬取商品数据。
        商品分类来自导航栏的两级结构（level1 > level2），而非 product_type。

        Args:
            url: 站点URL
            category: 类目名称（来源类目，用于数据库存储）
            progress_callback: 进度回调函数

        Returns:
            {"success": bool, "products": list, "count": int, "collections": int}
        """
        url = normalize_url(url)
        domain = get_domain(url)

        try:
            if progress_callback:
                progress_callback(f"[{domain}] 开始导航爬取")

            # 获取货币
            if progress_callback:
                progress_callback(f"[{domain}] 获取货币...")
            currency = self.fetch_currency(url)
            if not currency:
                log.warning(f"[{domain}] 非 Shopify 站点（meta.json 无响应）")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "非 Shopify 站点"}

            rate = self.currency_map.get(currency)
            if rate is None:
                log.warning(f"[{domain}] 未找到汇率: {currency}")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": f"无汇率配置: {currency}"}

            if progress_callback:
                progress_callback(f"[{domain}] 汇率 OK: {currency}，解析导航...")

            # 解析导航栏
            nav_items = parse_navigation(url)
            if not nav_items:
                log.warning(f"[{domain}] 导航解析为空")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "导航解析为空"}

            # 过滤出有 handle 的分类项
            valid_nav = [(l1, l2, cu, h) for l1, l2, cu, h in nav_items if h]
            if not valid_nav:
                log.warning(f"[{domain}] 无有效集合")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "无有效集合"}

            if progress_callback:
                progress_callback(f"[{domain}] 发现 {len(valid_nav)} 个集合，开始爬取")

            all_products = []
            seen_unique_keys = set()
            crawled_collections = 0

            for level1, level2, coll_url, handle in valid_nav:
                if not handle:
                    continue

                if progress_callback:
                    progress_callback(f"[{domain}] 爬取分类: {level1} > {level2} (/{handle})")

                collection_saved = 0
                page = 1
                empty_pages = 0

                while empty_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                    products_url = f"{url}/collections/{handle}/products.json?limit=200&page={page}"
                    data, code = self.fetch_json(products_url)

                    if code != 200:
                        break

                    products = data.get("products", []) if isinstance(data, dict) else []
                    if not products:
                        empty_pages += 1
                        page += 1
                        time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
                        continue

                    empty_pages = 0
                    page_products = []

                    for product in products:
                        if not isinstance(product, dict):
                            continue

                        title = str(product.get("title") or "").strip()
                        desc = str(product.get("body_html") or "").strip()
                        if not title or not desc:
                            continue

                        images = product.get("images", []) or []
                        variants = product.get("variants", []) or []
                        options = product.get("options", []) or []
                        product_type = str(product.get("product_type") or "").strip()

                        image = extract_images(images)
                        sku, variant_str = extract_variant_info(variants, options)
                        compare_at_price, price = extract_prices(variants)
                        original_price = convert_price(compare_at_price, rate)
                        discount_price = convert_price(price, rate)
                        price_value = discount_price if discount_price != "" else original_price

                        if price_value == "" or float(price_value) < 1:
                            continue

                        product_id = str(product.get("id") or "").strip()
                        unique_key = product_unique_key(domain, product_id, title, image)

                        if unique_key in seen_unique_keys:
                            continue
                        seen_unique_keys.add(unique_key)

                        page_products.append({
                            "product_id": product_id,
                            "SKU": sku,
                            "标题": title,
                            "描述": desc,
                            "子描述": str(product.get("tags") or "").strip(),
                            "图片": image,
                            "原价": original_price,
                            "折扣价": discount_price,
                            "变体": variant_str,
                            "分类": level2 if level2 else (product_type if product_type else category),
                            "currency": currency,
                            "source_url": url,
                            "source_domain": domain,
                            "source_category": level1,
                            "source_subcategory": level2,
                            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "unique_key": unique_key,
                        })

                    all_products.extend(page_products)
                    collection_saved += len(page_products)

                    page += 1
                    time.sleep(random.uniform(*PAGE_SLEEP_RANGE))

                crawled_collections += 1
                if progress_callback:
                    progress_callback(f"[{domain}] /{handle} 完成: {collection_saved} 件")

                # 集合间短暂休息
                time.sleep(random.uniform(1.0, 2.5))

            if progress_callback:
                progress_callback(f"[{domain}] 导航爬取完成: {crawled_collections} 个集合，{len(all_products)} 件商品")

            return {
                "success": True,
                "products": all_products,
                "count": len(all_products),
                "collections": crawled_collections,
                "domain": domain,
                "currency": currency,
            }

        except Exception as e:
            log.error(f"[{domain}] 导航爬取异常: {e}")
            return {"success": False, "products": [], "count": 0, "collections": 0, "error": str(e)}

    def crawl_category(self, category: str, max_sites: int = 10, progress_callback=None) -> Dict:
        """基于导航的深度爬取指定类目的商品数据

        从数据库的 {category}_filtered 集合获取店铺URL，解析导航栏按集合逐类爬取。
        商品分类来自店铺导航栏的两级结构，而非 product_type。

        Args:
            category: 类目名称
            max_sites: 最大爬取站点数
            progress_callback: 进度回调函数

        Returns:
            {"total_sites": int, "success_sites": int, "total_products": int, "total_collections": int}
        """
        # 从MongoDB获取_filtered集合中的店铺URL（去重）
        source_db = MongoDBClient()
        filtered_col = source_db.filtered_col(category)

        # 获取去重后的店铺URL（优先用 store_url，回退到 url 的域名根路径）
        seen_domains = set()
        store_urls = []
        for doc in filtered_col.find({}, {"url": 1, "domain": 1, "store_url": 1, "_id": 0}):
            domain = doc.get("domain", "")
            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)
            store_url = doc.get("store_url") or ""
            if not store_url:
                # 从 collection URL 推导 store URL
                raw_url = doc.get("url", "")
                parsed = urlparse(raw_url)
                store_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
            if store_url:
                store_urls.append({"url": store_url, "domain": domain})

        source_db.close()

        if not store_urls:
            log.warning(f"类目 {category} 无可用URL")
            return {"total_sites": 0, "success_sites": 0, "total_products": 0, "total_collections": 0, "error": "无可用URL"}

        # 限制站点数
        store_urls = store_urls[:max_sites]

        # 使用ProductDBClient保存数据
        product_db = ProductDBClient()
        product_db.ensure_product_indexes(category)

        total_sites = len(store_urls)
        success_sites = 0
        total_products = 0
        total_collections = 0

        for i, url_doc in enumerate(store_urls):
            url = url_doc.get("url", "")
            if not url:
                continue

            if progress_callback:
                progress_callback(f"处理站点 {i+1}/{total_sites}: {url}")

            result = self.crawl_site_with_nav(url, category, progress_callback)

            if result["success"] and result["products"]:
                saved_count = product_db.save_raw_products(category, result["products"])
                total_products += saved_count
                success_sites += 1
                total_collections += result.get("collections", 0)

                if progress_callback:
                    progress_callback(f"保存 {saved_count} 件商品到 {category}_raw")

            # 站点间冷却
            if i < total_sites - 1:
                time.sleep(random.uniform(*SITE_COOLDOWN_RANGE))

        product_db.close()

        return {
            "total_sites": total_sites,
            "success_sites": success_sites,
            "total_products": total_products,
            "total_collections": total_collections,
        }


def create_crawler() -> ProductCrawler:
    """创建爬取器实例"""
    # 加载汇率配置
    currency_config_path = settings.data_dir / "currency_config.json"
    if currency_config_path.exists():
        import json
        with open(currency_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        currency_map = {}
        if isinstance(data, list):
            for item in data:
                key = item.get("nation")
                value = item.get("exchange_rate_usd")
                if key and value is not None:
                    currency_map[str(key).upper()] = float(value)
        elif isinstance(data, dict):
            for key, value in data.items():
                currency_map[str(key).upper()] = float(value)
    else:
        # 默认汇率
        currency_map = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "CAD": 1.36, "AUD": 1.53}
    
    # 加载代理配置
    proxies_file = settings.data_dir / "proxies.txt"
    proxies = []
    if proxies_file.exists():
        with open(proxies_file, "r", encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]
    
    return ProductCrawler(currency_map=currency_map, proxies=proxies)
