import warnings
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import os
import sys
import signal

# ============== 日志配置 - 必须在最前面 ==============
from src.utils.logger import setup_logger
logger = setup_logger('text_scraper', os.path.join(os.path.dirname(__file__), 'data', 'logs', 'text_scraper.log'))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync
import requests
import time
import random
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pymongo import MongoClient
import json
import glob
import re
from src.utils.proxy_manager import get_random_proxy

# ---------------------- MongoDB 配置 ----------------------
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ============== 全局变量用于中断时保存 ==============
CURRENT_RESULTS = []          # 实时保存的所有结果

# ============== 信号处理器 ==============
def signal_handler(sig, frame):
    """Ctrl+C 或系统终止信号的统一处理"""
    print("\n\n检测到中断信号，已保存数据到 MongoDB，无需额外操作。")
    logger.info("接收到中断信号，程序退出。")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ============== 目录定位（自动适配 main.py 位置）==============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORDS_DIR = os.path.join(SCRIPT_DIR, "data", "keywords")

print(f"关键词目录：{KEYWORDS_DIR}\n")

# ============== User Agents ==============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15",
]

def get_random_user_agent():
    user_agent = random.choice(USER_AGENTS)
    logger.info(f"生成用户代理: {user_agent}")
    return user_agent

# ============== URL 清洗与去重 ==============
def clean_url_to_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            logger.error(f"URL {url} 无有效域名")
            return None, None
        if '.myshopify.com' in domain:
            main_domain = domain.replace('.myshopify.com', '.com')
            myshopify_url = urlunparse(('https', domain, '', '', '', ''))
        else:
            main_domain = domain
            myshopify_url = None
        if main_domain.startswith('www.'):
            main_domain = main_domain[4:]
        cleaned = urlunparse(('https', main_domain, '', '', '', ''))
        return cleaned, myshopify_url
    except Exception as e:
        logger.error(f"清洗URL {url} 时出错: {e}")
        return None, None

def get_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith('www.'):
            domain = domain[4:]
        if domain.endswith('.myshopify.com'):
            domain = domain.replace('.myshopify.com', '.com')
        return domain
    except Exception as e:
        logger.error(f"提取 {url} 的域名时出错: {e}")
        return None

def filter_urls(urls):
    cleaned_urls = set()
    seen_domains = set()
    url_map = {}
    for url in urls:
        cleaned, myshopify_url = clean_url_to_domain(url)
        domain = get_domain(cleaned)
        if not cleaned or not domain:
            continue
        if 'translate.google.com' in cleaned:
            continue
        if domain.lower() in seen_domains:
            continue
        cleaned_urls.add(cleaned)
        seen_domains.add(domain.lower())
        if myshopify_url:
            url_map[cleaned] = myshopify_url
    return list(cleaned_urls), url_map

# ============== 检测电商平台 ==============
def detect_ecommerce_platform(url, url_map, timeout=10):
    try:
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        if not url.endswith("/"):
            url += "/"
        headers = {'User-Agent': get_random_user_agent()}

        # Shopify
        try:
            response = requests.get(f"{url}meta.json", headers=headers, timeout=timeout)
            if response.status_code == 200 and 'published_products_count' in response.json():
                return "Shopify", url
        except: pass

        # WooCommerce
        try:
            response = requests.get(f"{url}wp-json/wc/v3/products?per_page=1", headers=headers, timeout=timeout)
            if response.status_code == 200 and isinstance(response.json(), list):
                return "WooCommerce", url
        except: pass

        # Magento
        try:
            response = requests.get(f"{url}magento_version", headers=headers, timeout=timeout)
            if response.status_code == 200 and "Magento" in response.text:
                return "Magento", url
        except: pass

        # Generic E-commerce
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                html_content = response.text.lower()
                payment_indicators = [
                    'js.stripe.com', 'stripe.js', 'paypal.com/sdk', 'paypalobjects.com',
                    'klarna.com', 'squareup.com', 'shopify_payments', 'afterpay.com',
                    '<meta name="generator" content="prestashop">', 'opencart'
                ]
                for indicator in payment_indicators:
                    if indicator in html_content:
                        return "Generic E-commerce", url
        except: pass

        myshopify_url = url_map.get(url.rstrip('/'))
        if myshopify_url:
            if not myshopify_url.endswith("/"):
                myshopify_url += "/"
            try:
                response = requests.get(f"{myshopify_url}meta.json", headers=headers, timeout=timeout)
                if response.status_code == 200 and 'published_products_count' in response.json():
                    return "Shopify", myshopify_url
            except: pass

        return None, None
    except Exception as e:
        logger.error(f"检测 {url} 电商平台时出错: {e}")
        return None, None

# ============== Shopify 产品数量采集 ==============
def get_shopify_product_count(url, url_map, timeout=5):
    used_url = url
    final_url = url
    try:
        if not used_url.startswith("http://") and not used_url.startswith("https://"):
            used_url = f"https://{used_url}"
        if not used_url.endswith("/"):
            used_url += "/"
        request_url = f"{used_url}meta.json"
        response = requests.get(request_url, timeout=timeout)
        response.raise_for_status()
        meta_json = response.json()
        published_products_count = meta_json.get('published_products_count', 0)
        final_url = used_url
        return published_products_count, final_url
    except:
        myshopify_url = url_map.get(url.rstrip('/'))
        if myshopify_url and used_url != myshopify_url:
            try:
                if not myshopify_url.endswith("/"):
                    myshopify_url += "/"
                request_url = f"{myshopify_url}meta.json"
                response = requests.get(request_url, timeout=timeout)
                response.raise_for_status()
                meta_json = response.json()
                published_products_count = meta_json.get('published_products_count', 0)
                final_url = myshopify_url
                return published_products_count, final_url
            except: pass
        return 0, final_url

# ============== 实时保存到 MongoDB ==============
def save_single_result(result_tuple, category):
    global CURRENT_RESULTS
    CURRENT_RESULTS.append(result_tuple)

    try:
        collection_name = f"{category}_Unfiltered_URLs"
        collection_dynamic = db[collection_name]
        doc = {
            "URL": result_tuple[0],
            "E-commerce Platform": result_tuple[1],
            "Product Count": result_tuple[2],
            "Domain": result_tuple[3],
            "Category": category,
            "Timestamp": result_tuple[4]
        }
        collection_dynamic.update_one(
            {"Domain": doc["Domain"]},
            {"$set": doc},
            upsert=True
        )
        logger.info(f"已保存到 MongoDB → {doc['URL']} ({doc['E-commerce Platform']}) 集合: {collection_name}")
        print(f"\n已实时保存 → {doc['URL']} ({doc['E-commerce Platform']})，累计 {len(CURRENT_RESULTS)} 条")
    except Exception as e:
        logger.error(f"保存到 MongoDB 出错: {e}")

# ============== 页面处理逻辑 ==============
def process_page_urls(urls, url_map, group_size=10, min_product_count=200, category=None):
    results = []
    ecommerce_urls = []
    with ThreadPoolExecutor(max_workers=group_size) as executor:
        future_to_url = {executor.submit(detect_ecommerce_platform, url, url_map): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                platform, used_url = future.result()
                if platform and used_url:
                    ecommerce_urls.append((url, used_url, platform))
            except: pass

    if ecommerce_urls:
        with ThreadPoolExecutor(max_workers=group_size) as executor:
            future_to_url = {}
            for url, used_url, platform in ecommerce_urls:
                if platform.lower() == 'shopify':
                    future_to_url[executor.submit(get_shopify_product_count, url, url_map)] = (url, used_url, platform)
                else:
                    domain = get_domain(used_url)
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    result = (used_url, platform, None, domain, timestamp)
                    results.append(result)
                    save_single_result(result, category)

            for future in as_completed(future_to_url):
                url, used_url, platform = future_to_url[future]
                try:
                    product_count, final_url = future.result()
                    if product_count >= min_product_count:
                        domain = get_domain(final_url)
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        result = (final_url, platform, product_count, domain, timestamp)
                        results.append(result)
                        save_single_result(result, category)
                except: pass
    return results

# ============== 搜索执行逻辑 ==============
def perform_search(pw, query, max_results=10000000, min_product_count=200, proxy=None, category=None):
    all_results = []
    page_size = 10
    current_page = 0
    consecutive_single_link_pages = 0
    browser = None
    context = None
    page = None

    def start_browser():
        nonlocal browser, context, page
        user_agent = get_random_user_agent()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=user_agent, proxy=proxy)
        page = context.new_page()
        stealth_sync(page)

    def close_browser():
        nonlocal browser, context, page
        if page: page.close()
        if context: context.close()
        if browser: browser.close()

    try:
        start_browser()
        while len(CURRENT_RESULTS) < max_results:
            start = current_page * page_size
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&start={start}"
            try:
                page.goto(search_url, timeout=60000)
                if page.locator("div.g-recaptcha, div.recaptcha").count() > 0:
                    print("检测到验证码，请手动完成后按 Enter 继续...")
                    input("按 Enter 继续...")
                    page.reload()
                    time.sleep(random.uniform(3, 5))
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(3, 5))

                page_urls = []
                selectors = [
                    "div.yuRUbf a[href^='http']",
                    "div.tF2Cxc a[href^='http']",
                    "a[href^='http'][jsname='UWckNb']",
                    "a[href*='http']:not([href*='google.com']):not([href*='apple.com'])"
                ]
                for selector in selectors:
                    links = page.query_selector_all(selector)
                    if links:
                        page_urls = [link.get_attribute("href") for link in links if link.get_attribute("href") and "http" in link.get_attribute("href")]
                        break

                if not page_urls:
                    break

                if len(page_urls) <= 3:
                    consecutive_single_link_pages += 1
                    if consecutive_single_link_pages >= 2:
                        break
                else:
                    consecutive_single_link_pages = 0

                cleaned_urls, url_map = filter_urls(page_urls)
                page_results = process_page_urls(cleaned_urls, url_map, group_size=10, min_product_count=min_product_count, category=category)
                all_results.extend(page_results)

                if len(CURRENT_RESULTS) >= max_results:
                    break

                current_page += 1
                time.sleep(random.uniform(3, 5))
            except PlaywrightTimeoutError:
                close_browser()
                start_browser()
            except Exception as e:
                close_browser()
                start_browser()

        close_browser()
        return all_results, []

    except Exception as e:
        close_browser()
        return all_results, []

# ============== 多关键词搜索 ==============
def google_search_urls(keywords, max_results=10000000, min_product_count=200, proxy=None, category=None):
    all_results = []
    with sync_playwright() as pw:
        for keyword in keywords:
            keyword = keyword.strip()
            if not keyword:
                continue
            variants = [
                f"{keyword} inurl:collections/all",
                f"{keyword} inurl:collections/all - page 123",
                f"{keyword} inurl:collections/all - page 211"
            ]
            for query in variants:
                if len(CURRENT_RESULTS) >= max_results:
                    break
                results, _ = perform_search(pw, query, max_results=max_results, min_product_count=min_product_count, proxy=proxy, category=category)
                all_results.extend(results)
            if len(CURRENT_RESULTS) >= max_results:
                break
    return all_results

# ============== 主函数（手动选 JSON + 自动随机 + 集合名只用文件名）==============
def main():
    global CURRENT_RESULTS

    print("谷歌电商店铺采集工具（手动选择 JSON 文件 + 自动随机采集 + 集合名只用文件名）\n")

    json_files = sorted(glob.glob(os.path.join(KEYWORDS_DIR, "*.json")))
    if not json_files:
        print("错误：在 mango 目录下未找到任何 .json 文件！")
        sys.exit(1)

    print("找到以下关键词文件：")
    for i, fp in enumerate(json_files, 1):
        print(f"  {i}. {os.path.basename(fp)}")

    while True:
        try:
            choice = int(input("\n请选择要使用的关键词文件（输入序号）: "))
            if 1 <= choice <= len(json_files):
                selected_file = json_files[choice - 1]
                break
        except ValueError:
            print("请输入有效数字。")

    json_filename = os.path.basename(selected_file).replace(".json", "")
    print(f"\n已选择文件：{json_filename}.json")

    # 集合名使用 json_filename_Unfiltered_URLs
    category = json_filename

    try:
        with open(selected_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"读取文件失败：{e}")
        sys.exit(1)

    all_data = {}
    if isinstance(data, dict):
        all_data = data
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                all_data.update(item)

    if not all_data:
        print("错误：文件内无有效数据")
        sys.exit(1)

    big_cats = list(all_data.keys())
    selected_big = random.choice(big_cats)
    print(f"自动随机选择大类：{selected_big}")

    sub_cats = all_data[selected_big]
    sub_list = list(sub_cats.keys())
    selected_sub = random.choice(sub_list)
    print(f"自动随机选择子分类：{selected_sub} ({len(sub_cats[selected_sub])} 个关键词)")

    keywords_all = sub_cats[selected_sub]
    selected_keywords = random.sample(keywords_all, min(20, len(keywords_all)))

    print(f"\nMongoDB 集合名称：{category}_Unfiltered_URLs")
    print(f"自动随机选取 {len(selected_keywords)} 个关键词：")
    for kw in selected_keywords:
        print(f"  - {kw}")

    print("\n开始采集（随时按 Ctrl+C 安全退出）...\n")

    proxy_dict, proxy_display = get_random_proxy()
    if proxy_dict:
        proxy = {
            "server": proxy_dict["http"]
        }
    else:
        proxy = None
    google_search_urls(selected_keywords, max_results=10000000, min_product_count=200, proxy=proxy, category=category)

    print("\n" + "="*60)
    print("采集完成！")
    if CURRENT_RESULTS:
        print(f"共成功采集并保存 {len(CURRENT_RESULTS)} 条有效店铺到集合：{category}_Unfiltered_URLs")
        for url, platform, count, domain, ts in CURRENT_RESULTS[-10:]:
            pc = f"{count}个产品" if count else "未验证"
            print(f"{url} → {platform}，{pc}")
    else:
        print("未采集到符合条件的店铺")

if __name__ == "__main__":
    main()