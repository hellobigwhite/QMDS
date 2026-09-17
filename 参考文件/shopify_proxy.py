import os
import time
import re
import requests
import urllib3
import cloudscraper
from urllib.parse import urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import pymysql
from pymysql.cursors import DictCursor
from contextlib import contextmanager
import random
from lxml import etree
from langdetect import detect, LangDetectException
from langdetect import DetectorFactory
DetectorFactory.seed = 0  # 保证语言检测结果确定性

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ====================== 汇率列表 ======================
CURRENCY_LIST = [
    {"nation": "USD", "exchange_rate_usd": 1}, {"nation": "CNY", "exchange_rate_usd": 0.14},
    {"nation": "HKD", "exchange_rate_usd": 0.13}, {"nation": "GBP", "exchange_rate_usd": 1.35},
    {"nation": "EUR", "exchange_rate_usd": 1.17}, {"nation": "JPY", "exchange_rate_usd": 0.0068},
    {"nation": "CAD", "exchange_rate_usd": 0.75}, {"nation": "AUD", "exchange_rate_usd": 0.65},
    {"nation": "CHF", "exchange_rate_usd": 1}, {"nation": "SGD", "exchange_rate_usd": 0.74},
    {"nation": "KRW", "exchange_rate_usd": 0.00075}, {"nation": "INR", "exchange_rate_usd": 0.012},
    {"nation": "BRL", "exchange_rate_usd": 0.18}, {"nation": "RUB", "exchange_rate_usd": 0.012},
    {"nation": "MYR", "exchange_rate_usd": 0.22}, {"nation": "ZAR", "exchange_rate_usd": 0.053},
    {"nation": "ARS", "exchange_rate_usd": 0.005}, {"nation": "AED", "exchange_rate_usd": 0.27},
    {"nation": "TRY", "exchange_rate_usd": 0.052}, {"nation": "IDR", "exchange_rate_usd": 0.000065},
    {"nation": "ILS", "exchange_rate_usd": 0.29}, {"nation": "THB", "exchange_rate_usd": 0.029},
    {"nation": "PHP", "exchange_rate_usd": 0.018}, {"nation": "NZD", "exchange_rate_usd": 0.61},
    {"nation": "CLP", "exchange_rate_usd": 0.0012}, {"nation": "CRC", "exchange_rate_usd": 0.0017},
    {"nation": "GEL", "exchange_rate_usd": 0.34}, {"nation": "MXN", "exchange_rate_usd": 0.053},
    {"nation": "NGN", "exchange_rate_usd": 0.00068}, {"nation": "PKR", "exchange_rate_usd": 0.0036},
    {"nation": "TWD", "exchange_rate_usd": 0.033}, {"nation": "JOD", "exchange_rate_usd": 1.41},
    {"nation": "COP", "exchange_rate_usd": 0.00026}, {"nation": "SAR", "exchange_rate_usd": 0.27},
    {"nation": "PLN", "exchange_rate_usd": 0.27}, {"nation": "QAR", "exchange_rate_usd": 0.27},
    {"nation": "EGP", "exchange_rate_usd": 0.021}, {"nation": "KWD", "exchange_rate_usd": 3.27},
    {"nation": "KYD", "exchange_rate_usd": 1.20}, {"nation": "TTD", "exchange_rate_usd": 0.15},
    {"nation": "LKR", "exchange_rate_usd": 0.0033}, {"nation": "JMD", "exchange_rate_usd": 0.0062},
    {"nation": "KES", "exchange_rate_usd": 0.0077}, {"nation": "SEK", "exchange_rate_usd": 0.11},
    {"nation": "MUR", "exchange_rate_usd": 0.022}, {"nation": "BBD", "exchange_rate_usd": 0.50},
    {"nation": "SBD", "exchange_rate_usd": 0.12}, {"nation": "BSD", "exchange_rate_usd": 1.00},
    {"nation": "MVR", "exchange_rate_usd": 0.065}, {"nation": "GHS", "exchange_rate_usd": 0.089},
    {"nation": "BWP", "exchange_rate_usd": 0.075}, {"nation": "TZS", "exchange_rate_usd": 0.00041},
    {"nation": "BZD", "exchange_rate_usd": 0.50}, {"nation": "DKK", "exchange_rate_usd": 0.16},
    {"nation": "RWF", "exchange_rate_usd": 0.00069}, {"nation": "CHF", "exchange_rate_usd": 1.24},
    {"nation": "LYD", "exchange_rate_usd": 0.18}, {"nation": "BHD", "exchange_rate_usd": 2.65},
    {"nation": "LBP", "exchange_rate_usd": 0.000011}, {"nation": "OMR", "exchange_rate_usd": 2.60},
    {"nation": "BND", "exchange_rate_usd": 0.77}, {"nation": "VND", "exchange_rate_usd": 0.000038},
    {"nation": "ZMW", "exchange_rate_usd": 0.044}, {"nation": "ALL", "exchange_rate_usd": 0.012},
    {"nation": "UGX", "exchange_rate_usd": 0.00028}, {"nation": "GYD", "exchange_rate_usd": 0.0048},
    {"nation": "MKD", "exchange_rate_usd": 0.019}, {"nation": "GTQ", "exchange_rate_usd": 0.13},
    {"nation": "RON", "exchange_rate_usd": 0.23}, {"nation": "ANG", "exchange_rate_usd": 0.56},
    {"nation": "MMK", "exchange_rate_usd": 0.0004762}, {"nation": "AMD", "exchange_rate_usd": 0.0026},
    {"nation": "FJD", "exchange_rate_usd": 0.44}, {"nation": "IQD", "exchange_rate_usd": 0.00076},
    {"nation": "HNL", "exchange_rate_usd": 0.038},{"nation": "CZK", "exchange_rate_usd": 0.049},
    {"nation": "XCD", "exchange_rate_usd": 0.37},{"nation": "MDL", "exchange_rate_usd": 0.058},
    {"nation": "NOK", "exchange_rate_usd": 0.11},{"nation": "HUF", "exchange_rate_usd": 0.0033},
    {"nation": "PGK", "exchange_rate_usd": 0.22}
]
# ====================== 数据库配置 ======================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "shopify",
    "charset": "utf8mb4",
    "cursorclass": DictCursor
}


@contextmanager
def get_db_connection():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def create_table(table_name: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS `{table_name}` (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    SKU VARCHAR(255),
                    标题 VARCHAR(768) NOT NULL,
                    描述 TEXT NOT NULL,
                    子描述 TEXT NOT NULL,
                    图片 TEXT NOT NULL,
                    原价 DECIMAL(15,2),
                    折扣价 DECIMAL(15,2),
                    变体 TEXT,
                    分类 TEXT,
                    huilv TINYINT DEFAULT 1,
                    status TINYINT DEFAULT 0,
                    site VARCHAR(255) NOT NULL,
                    UNIQUE KEY uk_title (标题)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    print(f"[V] 数据表 `{table_name}` 已创建")


# ====================== ProxyFlow 代理池 ======================
PROXY_POOL_URL = "http://66.154.112.62:8001"
PROXY_FLOW_KEY = "change-me-please"

scraper = cloudscraper.create_scraper()
_direct_mode_sites = set()


# ====================== 工具函数 ======================
def get_clean_site(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc or parsed.path
    return netloc.replace("www.", "").split(":")[0]


def build_meta_json_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}/meta.json"


def fetch_currency(url):
    try:
        meta_url = build_meta_json_url(url)
        r = scraper.get(meta_url, timeout=15)
        if r.status_code == 200:
            return r.json().get("currency", "USD")
        if r.status_code == 404 and "www." not in url:
            www_url = url.replace("://", "://www.")
            r2 = scraper.get(build_meta_json_url(www_url), timeout=15)
            if r2.status_code == 200:
                return r2.json().get("currency", "USD")
    except:
        pass
    return "USD"


def get_exchange_rate(nation: str):
    for c in CURRENCY_LIST:
        if c['nation'].upper() == nation.upper():
            return c['exchange_rate_usd'], True
    return 1.0, False


# ====================== Sitemap 解析 ======================
def fetch_sitemap(url, max_retries=3):
    """获取并解析 sitemap XML（cloudscraper 直连优先）"""
    # 1. cloudscraper 直连（sitemap 是静态文件，不需要代理池）
    try:
        print(f"  [>] cloudscraper 直连: {url}")
        r = scraper.get(url, timeout=15)
        if r.status_code == 200:
            return etree.fromstring(r.content)
        if r.status_code == 404 and "www." not in url:
            www_url = url.replace("://", "://www.")
            r2 = scraper.get(www_url, timeout=15)
            if r2.status_code == 200:
                return etree.fromstring(r2.content)
    except Exception as e:
        print(f"  [!] cloudscraper 失败: {e}")

    # 2. 回退到代理池（缩短超时）
    proxy_url = f"{PROXY_POOL_URL}/fetch"
    for attempt in range(max_retries):
        try:
            print(f"  [>] 代理池获取: {url} | 尝试 {attempt + 1}/{max_retries}")
            r = requests.get(proxy_url, params={"url": url, "key": PROXY_FLOW_KEY}, timeout=(5, 20))
            if r.status_code == 200:
                return etree.fromstring(r.content)
            if r.status_code == 404 and "www." not in url:
                www_url = url.replace("://", "://www.")
                r2 = requests.get(proxy_url, params={"url": www_url, "key": PROXY_FLOW_KEY}, timeout=(5, 20))
                if r2.status_code == 200:
                    return etree.fromstring(r2.content)
            if r.status_code in (502, 503, 506):
                time.sleep(5)
                continue
        except requests.exceptions.Timeout:
            print(f"  [!] 代理超时")
        except Exception as e:
            print(f"  [!] 代理失败: {e}")
        time.sleep(2)

    return None


def discover_product_sitemaps(domain):
    """从站点的 sitemap.xml 发现产品 sitemap（只取主站点，排除多语言子路径）"""
    base_url = f"https://{domain}"
    sitemap_url = f"{base_url}/sitemap.xml"
    print(f"[>] 获取 sitemap: {sitemap_url}")

    root = fetch_sitemap(sitemap_url)
    if root is None:
        # 尝试加 www
        if not domain.startswith("www."):
            www_domain = f"www.{domain}"
            sitemap_url = f"https://{www_domain}/sitemap.xml"
            print(f"[>] 尝试 www: {sitemap_url}")
            root = fetch_sitemap(sitemap_url)
            if root is not None:
                base_url = f"https://{www_domain}"
        if root is None:
            print(f"[X] 无法获取 sitemap: {domain}")
            return []

    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    root_tag = etree.QName(root.tag).localname

    product_sitemaps = []

    # sitemap index: 包含多个子 sitemap
    if root_tag == 'sitemapindex':
        sitemap_locs = root.xpath('//sm:sitemap/sm:loc/text()', namespaces=ns)
        for loc in sitemap_locs:
            loc_str = str(loc).strip()
            parsed = urlparse(loc_str)
            path = parsed.path
            # 只要 /sitemap.xml_products 这种，排除多语言子路径如 /fr/sitemap.xml_products
            # 多语言路径格式: /xx/sitemap.xml_products 或 /xx-xx/sitemap.xml_products
            # 主站点格式: /sitemap.xml_products 或 /sitemap_products.xml
            if re.search(r'/sitemap.*product', path, re.IGNORECASE):
                # 检查是否是多语言路径: /xx/ 或 /xx-xx/ 前缀
                # 排除路径中 /sitemap 之前有语言代码的情况
                # 正常: /sitemap.xml_products, /sitemap_products.xml
                # 排除: /fr/sitemap.xml_products, /en-au/sitemap.xml_products
                path_before_sitemap = path[:path.lower().index('/sitemap')]
                if path_before_sitemap and path_before_sitemap != '/':
                    # 路径中 /sitemap 之前有内容，可能是多语言路径
                    # 检查是否是语言代码格式（2-5个字母，可带连字符）
                    segments = [s for s in path_before_sitemap.split('/') if s]
                    is_locale = all(re.match(r'^[a-z]{2}(-[a-z]{2,4})?$', s, re.I) for s in segments)
                    if is_locale:
                        print(f"  [×] 跳过多语言 sitemap: {loc_str}")
                        continue
                product_sitemaps.append(loc_str)
                print(f"  [V] 发现产品 sitemap: {loc_str}")

    # 直接是 urlset，检查是否包含产品链接
    elif root_tag == 'urlset':
        locs = root.xpath('//sm:url/sm:loc/text()', namespaces=ns)
        if any('/products/' in str(loc) for loc in locs):
            product_sitemaps.append(sitemap_url)
            print(f"  [V] 当前 sitemap 包含产品链接")

    return product_sitemaps


def extract_product_urls_from_sitemap(sitemap_url):
    """从产品 sitemap 中提取所有产品 URL"""
    print(f"[>] 解析产品 sitemap: {sitemap_url}")
    root = fetch_sitemap(sitemap_url)
    if root is None:
        print(f"[X] 无法获取产品 sitemap: {sitemap_url}")
        return []

    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    locs = root.xpath('//sm:url/sm:loc/text()', namespaces=ns)
    product_urls = []
    for loc in locs:
        loc_str = str(loc).strip()
        # 只保留产品链接（包含 /products/）
        if '/products/' in loc_str:
            product_urls.append(loc_str)

    print(f"  [V] 发现 {len(product_urls)} 个产品链接")
    return product_urls


def fetch_product_json(product_url, max_retries=3):
    """获取单个产品的 JSON 数据"""
    # 将产品 URL 转为 .json 格式
    json_url = product_url.rstrip('/') + '.json'

    proxy_url = f"{PROXY_POOL_URL}/fetch"

    # 直连模式
    parsed = urlparse(json_url)
    site_key = f"{parsed.scheme}://{parsed.netloc}"
    if site_key in _direct_mode_sites:
        try:
            r = scraper.get(json_url, timeout=20)
            if r.status_code == 200:
                return r.json()
            print(f"  [!] 直连非200: {r.status_code} ← {json_url}")
        except Exception as e:
            print(f"  [!] 直连异常: {e} ← {json_url}")
        return None

    # 代理模式
    for attempt in range(max_retries):
        try:
            r = requests.get(proxy_url, params={"url": json_url, "key": PROXY_FLOW_KEY}, timeout=(8, 65))
            if r.status_code == 200:
                return r.json()
            print(f"  [!] 代理非200: {r.status_code} ← {json_url} | 尝试 {attempt + 1}/{max_retries}")
            if r.status_code in (502, 503, 506):
                wait = min(2 ** attempt * 5, 30)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
        except Exception as e:
            print(f"  [!] 代理异常: {e} ← {json_url}")
        time.sleep(2)

    # 回退 cloudscraper
    try:
        r = scraper.get(json_url, timeout=20)
        if r.status_code == 200:
            _direct_mode_sites.add(site_key)
            return r.json()
        print(f"  [!] 兜底非200: {r.status_code} ← {json_url}")
    except Exception as e:
        print(f"  [!] 兜底异常: {e} ← {json_url}")

    return None


# ====================== 语言检测 ======================
def is_english(text):
    """检测文本是否为英语"""
    if not text or len(text.strip()) < 10:
        return True  # 太短的文本默认放行
    try:
        lang = detect(text)
        return lang == 'en'
    except LangDetectException:
        return True  # 检测失败默认放行


# ====================== 数据处理 + 过滤 ======================
def process_and_insert_products(products, currency, site_url, table_name):
    clean_site = get_clean_site(site_url)
    rate, rate_found = get_exchange_rate(currency)

    if not rate_found:
        print(f" [!] 汇率缺失，跳过该站点: {site_url} (货币: {currency})")
        return 0, [{"url": site_url, "currency": currency}]

    data_list = []
    inserted = 0

    for product in products:
        try:
            sku = ''
            title = str(product.get('title') or '').strip()
            description = str(product.get('body_html') or '').strip()
            sub_description = ''

            if len(title) < 15 or re.fullmatch(r"[0-9\s,.\-]+", title):
                continue
            if len(description) < 30:
                continue

            if not is_english(title):
                continue

            variants = product.get('variants', [])
            orig_price = None
            disc_price = None

            if variants:
                try:
                    orig = variants[0].get('compare_at_price')
                    disc = variants[0].get('price')
                    if orig: orig_price = round(float(orig) * rate, 2)
                    if disc: disc_price = round(float(disc) * rate, 2)
                except:
                    pass

            prices = [p for p in [orig_price, disc_price] if p is not None and p > 0]
            if not prices: continue
            max_price = max(prices)
            has_valid_price = any(2 <= p <= 2000 for p in prices)
            if not has_valid_price or max_price > 2000: continue

            image_url = ''
            images = product.get('images')
            if images and isinstance(images, list) and len(images) > 0:
                image_url = str(images[0].get('src') or '')

            variant_str = ''
            for opt in product.get('options', []):
                if opt.get('name') != 'Title':
                    values = '#'.join(map(str, opt.get('values', [])))
                    variant_str += f"{opt.get('name')}^{values}|||"

            product_type = str(product.get('product_type') or '').strip()
            data_list.append((
                sku, title, description, sub_description, image_url,
                orig_price, disc_price, variant_str.strip("|||"),
                product_type, clean_site
            ))
            inserted += 1

        except Exception as e:
            print(f" [!] 单商品处理异常: {e}")
            continue

    if data_list:
        max_db_retries = 3
        for attempt in range(max_db_retries):
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        sql = f"""
                            INSERT IGNORE INTO `{table_name}`
                            (SKU, 标题, 描述, 子描述, 图片, 原价, 折扣价, 变体, 分类, huilv, status, site)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, %s)
                        """
                        cur.executemany(sql, data_list)
                    conn.commit()
                print(f"[V] 处理 {len(products)} 个商品，成功插入 {len(data_list)} 条")
                break
            except pymysql.err.OperationalError as db_err:
                if db_err.args[0] in (1213, 1205):
                    wait_time = (attempt + 1) * 2
                    print(f"[!] 数据库死锁/锁超时，{wait_time}秒后重试 ({attempt + 1}/{max_db_retries})...")
                    time.sleep(wait_time)
                    if attempt == max_db_retries - 1:
                        print(f"[X] 数据插入失败 (死锁重试耗尽): {db_err}")
                        return 0, []
                else:
                    print(f"[X] 数据库错误: {db_err}")
                    return 0, []
            except Exception as e:
                print(f"[X] 插入数据时发生未知错误: {e}")
                return 0, []

    return len(data_list), []


# ====================== 单店采集（基于 sitemap） ======================
def run_single_shop(url, table_name):
    try:
        domain = urlparse(url).netloc or urlparse(url).path
        if domain.startswith("www."):
            domain_no_www = domain[4:]
        else:
            domain_no_www = domain

        currency = fetch_currency(url)
        clean_site = get_clean_site(url)
        print(f"\n==================== 开始采集: {domain} ====================")
        print(f"站点: {clean_site} | 货币: {currency}")

        # 1. 发现产品 sitemaps
        product_sitemaps = discover_product_sitemaps(domain)
        if not product_sitemaps:
            print(f"[X] {domain} 未发现产品 sitemap，跳过")
            return 0, []

        # 2. 从所有产品 sitemap 中提取产品 URL
        all_product_urls = []
        for sm_url in product_sitemaps:
            urls = extract_product_urls_from_sitemap(sm_url)
            all_product_urls.extend(urls)

        if not all_product_urls:
            print(f"[X] {domain} 产品 sitemap 中无产品链接，跳过")
            return 0, []

        print(f"[i] {domain} 共发现 {len(all_product_urls)} 个产品链接")

        # 3. 多线程并发获取产品 JSON，每200个批量写入数据库
        total = 0
        local_missing = []
        batch = []
        done_count = 0
        workers = 10

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(fetch_product_json, pu): pu for pu in all_product_urls}

            for future in as_completed(future_map):
                done_count += 1
                try:
                    data = future.result()
                    if data and 'product' in data:
                        batch.append(data['product'])
                except Exception:
                    pass

                if len(batch) >= 200:
                    count, missing = process_and_insert_products(batch, currency, url, table_name)
                    if missing:
                        local_missing.extend(missing)
                    total += count
                    print(f"[V] 进度 {done_count}/{len(all_product_urls)} | 本批插入 {count} 条 | 累计 {total} 条")
                    batch = []

        # 处理剩余不足200的产品
        if batch:
            count, missing = process_and_insert_products(batch, currency, url, table_name)
            if missing:
                local_missing.extend(missing)
            total += count
            print(f"[V] 进度 {len(all_product_urls)}/{len(all_product_urls)} | 本批插入 {count} 条 | 累计 {total} 条")

        return total, local_missing

    except Exception as e:
        print(f"[X] 异常 {url}: {e}")
        return 0, []


# ====================== 批量执行 ======================
def run_batch_from_txt(txt_path):
    table_name = os.path.splitext(os.path.basename(txt_path))[0]
    create_table(table_name)

    with open(txt_path, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and line.startswith("http")]

    print(f"\n开始采集 {len(urls)} 个店铺 → 表名: {table_name}\n")

    all_missing = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(run_single_shop, url, table_name): url for url in urls}
        for future in as_completed(future_to_url):
            result_count, result_missing = future.result()
            if result_missing:
                all_missing.extend(result_missing)

    if all_missing:
        print("\n\n[!] === 汇率缺失站点汇总 ===")
        for i, s in enumerate(all_missing, 1):
            print(f"{i}. {s['url']} → {s['currency']}")


if __name__ == "__main__":
    print("=" * 50)
    print("[*] Shopify 店铺采集工具（Sitemap 模式）")
    print("=" * 50)

    txt_input = input("\n请输入 TXT 文件路径: ").strip()
    if txt_input and os.path.isfile(txt_input):
        run_batch_from_txt(txt_input)
    else:
        print("[X] 文件不存在或未输入路径")
