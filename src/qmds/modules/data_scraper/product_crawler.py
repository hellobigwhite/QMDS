"""产品数据爬取模块 - 基于导航的深度爬取"""

import json
import re
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from qmds.config import settings
from qmds.config.categories import normalize_subcategory
from qmds.db.mongodb import MongoDBClient
from qmds.db.product_db import ProductDBClient
from qmds.modules.data_scraper.shopify_nav_parser import parse_navigation
from qmds.utils.logger import get_logger
from qmds.utils.proxy_manager import ProxyManager

log = get_logger("product_crawler")

# 请求配置
REQUEST_TIMEOUT = 25
MAX_PAGE_LIMIT = 100
MAX_EMPTY_PAGES = 5
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


def product_unique_key(title: str) -> str:
    """生成商品唯一标识 - 以商品标题为唯一主键（全局去重）"""
    return str(title or "").strip().lower()


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


class ProxyServiceClient:
    """代理服务客户端 - 通过代理服务接口请求目标URL"""

    BASE_URL = "http://66.154.112.62:8000/fetch"
    API_KEY = "change-me-please"
    TIMEOUT = 90

    def __init__(self):
        self._success = 0
        self._failure = 0

    def fetch(self, target_url: str) -> Tuple[Optional[dict], int]:
        """通过代理服务请求目标URL"""
        params = {"key": self.API_KEY, "url": target_url}
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct.lower():
                    self._success += 1
                    return resp.json(), 200
            self._failure += 1
            log.warning(f"代理服务请求失败 {target_url} | HTTP {resp.status_code}")
            return None, resp.status_code
        except requests.exceptions.Timeout:
            self._failure += 1
            log.warning(f"代理服务超时 {target_url} | timeout={self.TIMEOUT}s")
            return None, 0
        except Exception as e:
            self._failure += 1
            log.warning(f"代理服务异常 {target_url}: {type(e).__name__}: {e}")
            return None, 0

    def log_stats(self):
        """输出统计信息"""
        total = self._success + self._failure
        rate = (self._success / total * 100) if total > 0 else 0
        log.info(f"代理服务统计: 成功={self._success}, 失败={self._failure}, 成功率={rate:.1f}%")


class ProductCrawler:
    """产品数据爬取器"""
    
    def __init__(self, currency_map: Dict[str, float], proxy_manager=None):
        self.currency_map = currency_map
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })
        
        # 代理管理（支持标记坏代理 + 冷却轮换）
        self.proxy_manager = proxy_manager
        # 代理服务客户端（优先使用）
        self.proxy_service = ProxyServiceClient()
    
    def close(self):
        """关闭会话释放资源"""
        self.proxy_service.log_stats()
        if self.session:
            self.session.close()
    
    def get_next_proxy(self) -> Optional[dict]:
        """获取下一个可用代理（ProxyManager 自动跳过冷却中的代理）"""
        if not self.proxy_manager:
            return None
        return self.proxy_manager.get_proxy()

    def mark_proxy_bad(self, proxy_dict: Optional[dict], cooldown: float = 60.0):
        """标记代理为不可用（触发冷却）"""
        if self.proxy_manager and proxy_dict:
            self.proxy_manager.mark_bad(proxy_dict, cooldown=cooldown)
    
    def fetch_json(self, url: str, timeout: int = REQUEST_TIMEOUT) -> Tuple[Optional[dict], int]:
        """获取JSON数据（优先代理服务 → 本地代理池 → 直连降级）"""
        # 第一步：优先使用代理服务
        data, status = self.proxy_service.fetch(url)
        self.proxy_service.log_stats()
        if status == 200 and data:
            return data, status
        # 代理服务失败后等待3秒，避免触发频率限制
        time.sleep(3)

        # 第二步：降级到本地代理池（3次尝试）
        last_status = 0
        for attempt in range(3):
            proxy = self.get_next_proxy()
            try:
                response = self.session.get(url, timeout=timeout, proxies=proxy)
                status = response.status_code
                ct = response.headers.get("Content-Type", "")

                if status != 200:
                    body = response.text[:150].replace("\n", " ").strip() if response.text else ""
                    if status == 429:
                        self.mark_proxy_bad(proxy, cooldown=120.0)
                        last_status = 429
                        if attempt < 2:
                            continue  # 换下一个代理重试，不立即返回
                        log.warning(f"429限流 {url} | 已尝试{attempt+1}个代理均被限流 | body={body}")
                    elif status in (403, 401):
                        log.warning(f"{status}拒绝 {url} | proxy={proxy}")
                        return None, status
                    else:
                        log.warning(f"HTTP {status} {url} | proxy={proxy} | body={body}")
                        return None, status

                # 200但Content-Type不是JSON
                if status == 200 and "json" not in ct.lower():
                    body = response.text[:200].replace("\n", " ").strip() if response.text else ""
                    self.mark_proxy_bad(proxy, cooldown=60.0)
                    log.warning(f"非JSON响应 {url} | ct={ct} proxy={proxy} | body={body}")
                    return None, status

                if status == 200:
                    return response.json(), status

            except requests.exceptions.Timeout:
                last_status = 0
                if attempt == 2:
                    log.warning(f"请求超时 {url} | proxy={proxy} | timeout={timeout}s")
                    return None, 0
            except requests.exceptions.ConnectionError as e:
                self.mark_proxy_bad(proxy, cooldown=60.0)
                last_status = 0
                if attempt == 2:
                    log.warning(f"连接失败 {url} | proxy={proxy} | {e}")
                    return None, 0
            except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
                log.warning(f"JSON解析失败 {url} | proxy={proxy}")
                return None, 0
            except Exception as e:
                last_status = 0
                if attempt == 2:
                    log.warning(f"请求失败 {url} | proxy={proxy} | {type(e).__name__}: {e}")
                    return None, 0
            time.sleep(1)

        # 第三步：直连降级（仅429时）
        if last_status == 429:
            try:
                response = self.session.get(url, timeout=timeout)
                if response.status_code == 200:
                    ct = response.headers.get("Content-Type", "")
                    if "json" in ct.lower():
                        return response.json(), 200
                log.warning(f"直连降级失败 {url} | HTTP {response.status_code}")
                return None, response.status_code
            except Exception as e:
                log.warning(f"直连降级失败 {url} | {type(e).__name__}: {e}")
                return None, 0

        return None, last_status
    
    def fetch_currency(self, url: str) -> str:
        """获取货币类型"""
        meta_url = f"{normalize_url(url)}/meta.json"
        data, status = self.fetch_json(meta_url, timeout=15)
        if status == 200 and isinstance(data, dict):
            currency = data.get("currency", "USD")
            return str(currency).upper() if currency else "USD"
        return ""
    
    def crawl_site(self, url: str, category: str, progress_callback=None,
                   stop_event: threading.Event = None, subcategory: str = "") -> Dict:
        """爬取单个站点的商品数据
        
        Args:
            url: 站点URL
            category: 一级分类名称
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选）
            subcategory: 二级分类名称（空字符串归入 "other"）
            
        Returns:
            {"success": bool, "products": list, "count": int}
        """
        url = normalize_url(url)
        domain = get_domain(url)
        subcategory_norm = normalize_subcategory(subcategory)
        
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
            empty_saved_pages = 0

            while empty_pages < MAX_EMPTY_PAGES and empty_saved_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                if stop_event and stop_event.is_set():
                    log.info(f"[{domain}] 收到停止信号，已爬取 {len(all_products)} 件")
                    return {"success": True, "products": all_products, "count": len(all_products),
                            "domain": domain, "currency": currency}

                products_url = f"{url}/products.json?limit=200&page={page}"
                data, code = self.fetch_json(products_url)
                
                if code != 200:
                    break
                
                products = data.get("products", []) if isinstance(data, dict) else []
                if not products:
                    empty_pages += 1
                    empty_saved_pages += 1
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
                    if not image:
                        continue
                    sku, variant_str = extract_variant_info(variants, options)
                    compare_at_price, price = extract_prices(variants)
                    original_price = convert_price(compare_at_price, rate)
                    discount_price = convert_price(price, rate)
                    price_value = discount_price if discount_price != "" else original_price
                    
                    if price_value == "" or float(price_value) < 1:
                        continue
                    
                    product_id = str(product.get("id") or "").strip()
                    unique_key = product_unique_key(title)
                    
                    if unique_key in seen_unique_keys:
                        continue
                    seen_unique_keys.add(unique_key)
                    
                    page_products.append({
                        "product_id": product_id,
                        "SKU": sku,
                        "标题": title,
                        "描述": desc,
                        "子描述": "",
                        "图片": image,
                        "原价": str(original_price) if original_price != "" else "",
                        "折扣价": discount_price,
                        "变体": variant_str,
                        "分类": product_type if product_type else category,
                        "currency": currency,
                        "source_url": url,
                        "source_domain": domain,
                        "source_category": category,
                        "source_subcategory": subcategory_norm,
                        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "unique_key": unique_key,
                    })
                
                all_products.extend(page_products)

                if not page_products:
                    empty_saved_pages += 1
                else:
                    empty_saved_pages = 0

                if progress_callback and page % 5 == 0:
                    progress_callback(f"[{domain}] 第{page}页: 累计{len(all_products)}件")

                if empty_saved_pages >= MAX_EMPTY_PAGES:
                    if progress_callback:
                        progress_callback(f"[{domain}] 连续{MAX_EMPTY_PAGES}页无有效商品，跳过该站点")
                    log.info(f"[{domain}] 连续{MAX_EMPTY_PAGES}页无有效商品，跳过")
                    break

                page += 1
                if stop_event and stop_event.is_set():
                    break
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
    
    def crawl_site_with_nav(self, url: str, category: str, progress_callback=None, subcategory: str = "") -> Dict:
        """基于导航的深度爬取单个站点

        解析店铺导航栏获取分类结构，按集合逐类爬取商品数据。
        商品分类来自导航栏的两级结构（level1 > level2），而非 product_type。

        Args:
            url: 站点URL
            category: 一级分类名称（来源类目，用于数据库存储）
            progress_callback: 进度回调函数
            subcategory: 二级分类名称（空字符串归入 "other"，覆盖导航解析的 level2）

        Returns:
            {"success": bool, "products": list, "count": int, "collections": int}
        """
        url = normalize_url(url)
        domain = get_domain(url)
        override_sub = normalize_subcategory(subcategory) if subcategory else ""

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
                empty_saved_pages = 0

                while empty_pages < MAX_EMPTY_PAGES and empty_saved_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                    products_url = f"{url}/collections/{handle}/products.json?limit=200&page={page}"
                    data, code = self.fetch_json(products_url)

                    if code != 200:
                        break

                    products = data.get("products", []) if isinstance(data, dict) else []
                    if not products:
                        empty_pages += 1
                        empty_saved_pages += 1
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
                        if not image:
                            continue
                        sku, variant_str = extract_variant_info(variants, options)
                        compare_at_price, price = extract_prices(variants)
                        original_price = convert_price(compare_at_price, rate)
                        discount_price = convert_price(price, rate)
                        price_value = discount_price if discount_price != "" else original_price

                        if price_value == "" or float(price_value) < 1:
                            continue

                        product_id = str(product.get("id") or "").strip()
                        unique_key = product_unique_key(title)

                        if unique_key in seen_unique_keys:
                            continue
                        seen_unique_keys.add(unique_key)

                        page_products.append({
                            "product_id": product_id,
                            "SKU": sku,
                            "标题": title,
                            "描述": desc,
                            "子描述": "",
                            "图片": image,
                            "原价": str(original_price) if original_price != "" else "",
                            "折扣价": discount_price,
                            "变体": variant_str,
                            "分类": level2 if level2 else (product_type if product_type else category),
                            "currency": currency,
                            "source_url": url,
                            "source_domain": domain,
                            "source_category": level1,
                            "source_subcategory": override_sub if override_sub else normalize_subcategory(level2),
                            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "unique_key": unique_key,
                        })

                    all_products.extend(page_products)
                    collection_saved += len(page_products)

                    if not page_products:
                        empty_saved_pages += 1
                    else:
                        empty_saved_pages = 0

                    if empty_saved_pages >= MAX_EMPTY_PAGES:
                        if progress_callback:
                            progress_callback(f"[{domain}] /{handle} 连续{MAX_EMPTY_PAGES}页无有效商品，跳过该集合")
                        log.info(f"[{domain}] /{handle} 连续{MAX_EMPTY_PAGES}页无有效商品，跳过")
                        break

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

    def _crawl_single_site(self, url_doc: dict, category: str, site_index: int,
                           total_sites: int, progress_callback=None,
                           stop_event: threading.Event = None, subcategory: str = "") -> Dict:
        """爬取单个站点的商品数据并保存（线程安全，每线程独立 crawler + db 实例）

        Args:
            url_doc: 包含 url 和 domain 的字典
            category: 一级分类名称
            site_index: 站点序号（用于日志）
            total_sites: 总站点数
            progress_callback: 进度回调函数
            stop_event: 停止信号事件
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        url = url_doc.get("url", "")
        domain = url_doc.get("domain", "")

        if stop_event and stop_event.is_set():
            log.info(f"[{site_index}/{total_sites}] 跳过（已停止）: {domain}")
            return {"success": False, "saved": 0, "url": url, "domain": domain, "error": "任务已停止", "stopped": True}

        crawler = create_crawler()
        product_db = ProductDBClient()
        try:
            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 开始: {domain}")

            result = crawler.crawl_site(url, category, progress_callback, stop_event=stop_event, subcategory=subcategory)

            saved_count = 0
            if result["success"] and result["products"]:
                saved_count = product_db.save_raw_products(category, subcategory, result["products"])
                if progress_callback:
                    progress_callback(f"[{site_index}/{total_sites}] 保存 {saved_count} 件: {domain}")

            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 完成: {domain} ({saved_count} 件)")

            return {
                "success": result["success"],
                "saved": saved_count,
                "url": url,
                "domain": domain,
                "error": result.get("error"),
                "stopped": False,
            }
        except Exception as e:
            if stop_event and stop_event.is_set():
                log.info(f"[{site_index}/{total_sites}] 停止: {domain}")
            else:
                log.error(f"[{site_index}/{total_sites}] 站点异常 {domain}: {e}")
            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 失败: {domain} ({e})")
            return {"success": False, "saved": 0, "url": url, "domain": domain, "error": str(e), "stopped": bool(stop_event and stop_event.is_set())}
        finally:
            crawler.close()
            product_db.close()

    def _mark_crawled(self, source_db, category: str, crawled_domains: dict, subcategory: str = ""):
        """将已处理域名的 collection URL 在同一集合中标记 crawl_status（不再跨集合移动）

        Args:
            source_db: MongoDBClient 实例
            category: 一级分类名称
            crawled_domains: {domain: {"products": int, "success": bool}, ...}
                             success=False 表示爬取失败的域名（同样会被标记，避免反复重试）
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        if not crawled_domains:
            return
        try:
            from qmds.db.mongodb import CRAWL_STATUS_CRAWLED, CRAWL_STATUS_FAILED
            filtered_col = source_db.filtered_col(category, subcategory)
            domains = list(crawled_domains.keys())
            # 查询这些域名的所有 collection URL 文档
            filtered_docs = list(filtered_col.find(
                {"domain": {"$in": domains}},
                {"url": 1, "domain": 1, "_id": 0}
            ))
            if not filtered_docs:
                return
            url_crawl_info_list = []
            for doc in filtered_docs:
                domain = doc.get("domain", "")
                info = crawled_domains.get(domain, {})
                url_crawl_info_list.append({
                    "url": doc["url"],
                    "products": info.get("products", 0),
                    "success": info.get("success", False),
                })
            if url_crawl_info_list:
                moved = source_db.move_to_crawled_batch(category, url_crawl_info_list, subcategory=subcategory)
                log.info(f"[{category}/{subcategory or 'other'}] 已标记 {moved} 条集合URL为已爬取")
        except Exception as e:
            log.error(f"[{category}/{subcategory or 'other'}] 标记爬取状态失败: {e}")

    def crawl_category(self, category: str, max_sites: int = 0, workers: int = 1,
                       progress_callback=None, stop_event: threading.Event = None,
                       subcategory: str = "") -> Dict:
        """爬取指定分类的商品数据（支持多线程并发）

        从数据库的 {prefix} 集合获取 filter_status=filtered 且 crawl_status=uncrawled 的店铺URL，通过 products.json API 爬取商品。
        workers=1 时串行执行（与旧行为一致），workers>1 时使用线程池并发爬取。
        每个线程拥有独立的 ProductCrawler 实例（独立 Session + 代理）和 ProductDBClient 实例。

        Args:
            category: 一级分类名称
            max_sites: 最大爬取站点数（0 表示不限制，爬取所有可用 URL）
            workers: 并发线程数（默认 1 串行）
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选，传入后可即时停止所有线程）
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            {"total_sites": int, "success_sites": int, "total_products": int}
        """
        subcategory_norm = normalize_subcategory(subcategory)
        # 从MongoDB获取 {prefix} 集合中未爬取的 filtered URL（去重）
        source_db = MongoDBClient()
        filtered_col = source_db.filtered_col(category, subcategory_norm)

        # 获取去重后的店铺URL（优先用 store_url，回退到 url 的域名根路径）
        # 只查询 filter_status=filtered 且 crawl_status=uncrawled 的文档
        seen_domains = set()
        store_urls = []
        query = {"filter_status": "filtered", "crawl_status": "uncrawled"}
        for doc in filtered_col.find(query, {"url": 1, "domain": 1, "store_url": 1, "_id": 0}):
            domain = doc.get("domain", "")
            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)
            store_url = doc.get("store_url") or ""
            if not store_url:
                raw_url = doc.get("url", "")
                parsed = urlparse(raw_url)
                store_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
            if store_url:
                store_urls.append({"url": store_url, "domain": domain})

        if not store_urls:
            source_db.close()
            log.warning(f"分类 {category}/{subcategory_norm} 无可用URL")
            return {"total_sites": 0, "success_sites": 0, "total_products": 0, "error": "无可用URL"}

        if max_sites > 0:
            store_urls = store_urls[:max_sites]
        total_sites = len(store_urls)

        # 预创建索引（共享，MongoDB 索引操作本身是幂等的）
        product_db = ProductDBClient()
        product_db.ensure_product_indexes(category, subcategory_norm)
        product_db.close()

        log.info(f"开始爬取分类 {category}/{subcategory_norm}: {total_sites} 个站点, {workers} 线程")
        if progress_callback:
            progress_callback(f"开始爬取: {category}/{subcategory_norm} - {total_sites} 个站点, {workers} 线程")

        # ── 串行模式 ──
        if workers <= 1:
            success_sites = 0
            total_products = 0
            crawled_domains = {}
            for i, url_doc in enumerate(store_urls, 1):
                if stop_event and stop_event.is_set():
                    log.info(f"分类 {category}/{subcategory_norm}: 收到停止信号，已处理 {i-1}/{total_sites} 站点")
                    break
                result = self._crawl_single_site(url_doc, category, i, total_sites,
                                                 progress_callback, stop_event=stop_event,
                                                 subcategory=subcategory_norm)
                if result["success"]:
                    success_sites += 1
                    total_products += result["saved"]
                # 被停止信号跳过的站点保留为 uncrawled，以便下次继续；其余（成功/失败）均标记为已爬取
                if not result.get("stopped"):
                    crawled_domains[url_doc["domain"]] = {"products": result["saved"], "success": result["success"]}
                if i < total_sites and not (stop_event and stop_event.is_set()):
                    time.sleep(random.uniform(*SITE_COOLDOWN_RANGE))

            self._mark_crawled(source_db, category, crawled_domains, subcategory=subcategory_norm)
            source_db.close()

            return {
                "total_sites": total_sites,
                "success_sites": success_sites,
                "total_products": total_products,
            }

        # ── 并发模式 ──
        lock = threading.Lock()
        success_sites = 0
        total_products = 0
        crawled_domains = {}

        def _worker(idx: int, url_doc: dict) -> dict:
            nonlocal success_sites, total_products, crawled_domains
            result = self._crawl_single_site(url_doc, category, idx, total_sites,
                                             progress_callback, stop_event=stop_event,
                                             subcategory=subcategory_norm)
            if result["success"]:
                with lock:
                    success_sites += 1
                    total_products += result["saved"]
            # 被停止信号跳过的站点保留为 uncrawled；其余（成功/失败）均标记为已爬取
            if not result.get("stopped"):
                with lock:
                    crawled_domains[url_doc["domain"]] = {"products": result["saved"], "success": result["success"]}
            return result

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl_site") as executor:
            futures = {
                executor.submit(_worker, i, url_doc): url_doc
                for i, url_doc in enumerate(store_urls, 1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    url_doc = futures[future]
                    log.error(f"线程异常 {url_doc.get('domain', '')}: {e}")

        self._mark_crawled(source_db, category, crawled_domains, subcategory=subcategory_norm)
        source_db.close()

        log.info(f"分类 {category}/{subcategory_norm} 爬取完成: {success_sites}/{total_sites} 站点, {total_products} 件商品")
        if progress_callback:
            progress_callback(f"爬取完成: {category}/{subcategory_norm} - {success_sites}/{total_sites} 站点, {total_products} 件商品")

        return {
            "total_sites": total_sites,
            "success_sites": success_sites,
            "total_products": total_products,
        }

    def crawl_category_all_subcategories(self, category: str, max_sites: int = 0,
                                         workers: int = 1, progress_callback=None,
                                         stop_event: threading.Event = None) -> Dict:
        """爬取一级分类下所有二级分类的商品数据

        从 MongoDB 动态获取该一级分类下所有有 filtered 数据的二级分类，
        依次调用 crawl_category() 爬取每个二级分类，汇总结果。

        Args:
            category: 一级分类名称
            max_sites: 每个二级分类最大爬取站点数（0 表示不限制）
            workers: 并发线程数（默认 1 串行）
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选）

        Returns:
            {"total_subcategories": int, "total_sites": int,
             "success_sites": int, "total_products": int}
        """
        source_db = MongoDBClient()
        try:
            subcategories = source_db.list_filtered_subcategories(category)
        finally:
            source_db.close()

        if not subcategories:
            log.warning(f"一级分类 {category} 无可用二级分类")
            if progress_callback:
                progress_callback(f"一级分类 {category} 无可用二级分类")
            return {
                "total_subcategories": 0,
                "total_sites": 0,
                "success_sites": 0,
                "total_products": 0,
                "error": "无可用二级分类",
            }

        log.info(f"开始爬取一级分类 {category}: 共 {len(subcategories)} 个二级分类")
        if progress_callback:
            progress_callback(f"开始爬取一级分类 {category}: 共 {len(subcategories)} 个二级分类")

        grand_total_sites = 0
        grand_success_sites = 0
        grand_total_products = 0

        for idx, sub in enumerate(subcategories, 1):
            if stop_event and stop_event.is_set():
                log.info(f"一级分类 {category}: 收到停止信号，已完成 {idx - 1}/{len(subcategories)} 个二级分类")
                if progress_callback:
                    progress_callback(f"已停止: 完成 {idx - 1}/{len(subcategories)} 个二级分类")
                break

            sub_display = sub if sub else "other"
            if progress_callback:
                progress_callback(f"[{idx}/{len(subcategories)}] 开始爬取二级分类: {category}/{sub_display}")

            result = self.crawl_category(
                category, max_sites=max_sites, workers=workers,
                progress_callback=progress_callback, stop_event=stop_event,
                subcategory=sub,
            )

            grand_total_sites += result.get("total_sites", 0)
            grand_success_sites += result.get("success_sites", 0)
            grand_total_products += result.get("total_products", 0)

            if progress_callback:
                progress_callback(
                    f"[{idx}/{len(subcategories)}] 二级分类 {category}/{sub_display} 完成: "
                    f"成功 {result.get('success_sites', 0)}/{result.get('total_sites', 0)} 站点, "
                    f"{result.get('total_products', 0)} 件商品"
                )

        summary = (f"一级分类 {category} 爬取完成: {len(subcategories)} 个二级分类, "
                   f"{grand_success_sites}/{grand_total_sites} 站点, {grand_total_products} 件商品")
        log.info(summary)
        if progress_callback:
            progress_callback(summary)

        return {
            "total_subcategories": len(subcategories),
            "total_sites": grand_total_sites,
            "success_sites": grand_success_sites,
            "total_products": grand_total_products,
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
    
    # 加载代理配置（ProxyManager 自动转换格式 + 支持标记坏代理 + 冷却轮换）
    proxy_manager = ProxyManager.from_settings()

    return ProductCrawler(currency_map=currency_map, proxy_manager=proxy_manager)
