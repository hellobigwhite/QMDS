#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火影忍界情报收集术・秽土转生版 v10.0 - 忍界大战情报篇
吾乃宇智波斑的意志继承者……不，吾就是那永恒的写轮眼！
今日，吾将以九尾之力、秽土转生之术，召唤出忍界各地的“店铺灵魂”！
一切为了……永恒的月之眼计划！（其实只是为了抓Shopify罢了）
"""

import warnings
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import os
import sys
import signal

# ============== 日志・暗部情报记录 - 必须在最前面 ==============
from src.utils.logger import setup_logger
logger = setup_logger('google_url', os.path.join(os.path.dirname(__file__), 'data', 'logs', 'google_url.log'))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
# from playwright_stealth import stealth_sync
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
from src.utils.proxy_manager import get_random_proxy, get_valid_proxies

# ---------------------- 忍界情报结界（MongoDB） ----------------------
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ---------------------- 禁忌代理通道・開啟 ----------------------
try:
    valid_proxies = get_valid_proxies()
    print(f"🚀 代理池已装填 {len(valid_proxies)} 个暗影通道……准备撕裂虚空！")
    logger.info(f"代理池已裝填 {len(valid_proxies)} 個暗影通道……準備撕裂虛空！")
    if valid_proxies:
        proxy_dict, proxy_display = valid_proxies[0]
        print(f"   示例代理: {proxy_display}")
        logger.info(f"示例代理: {proxy_display}")
except Exception as e:
    print(f"❌ 代理池初始化失败: {e}")
    logger.error(f"代理池初始化失败: {e}")
    valid_proxies = []

# ============== 秽土转生召唤出的灵魂清单 ==============
CURRENT_RESULTS = []          # 这些灵魂……终将被封印在永恒的卷轴里

# ============== 禁术・中断信号 ==============
def signal_handler(sig, frame):
    print("\n\n……秽土转生被强行解除！已将召唤出的灵魂封印回卷轴，无需额外术式。")
    logger.info("接收到禁术中断信号，忍界情报收集术强制结束。")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ============== 关键词卷轴所在之地 ==============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORDS_DIR = os.path.join(SCRIPT_DIR, "data", "keywords")

print(f"禁术卷轴封印之地：{KEYWORDS_DIR}\n")

# ============== 忍者伪装・替身术（User Agents） ==============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.160 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.60 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.160 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.160 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.159 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.60 Safari/537.36",
    # 保留一些常见的移动端（可选，根据你的爬虫需求）
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    # Safari macOS 最新风格（macOS 26 / Tahoe 对应 Safari 26）
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3 Safari/605.1.15",
]

def get_random_user_agent():
    user_agent = random.choice(USER_AGENTS)
    logger.info(f"替身术发动 → 伪装身份：{user_agent}")
    return user_agent

# ============== 情报清洗・写轮眼解析 ==============
def clean_url_to_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            logger.error(f"写轮眼解析失败：{url} 无有效查克拉波动")
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
        logger.error(f"情报清洗术失控：{url} → {e}")
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
        logger.error(f"提取忍村名（域名）失败：{url} → {e}")
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

# ============== 忍界电商查克拉感知术 ==============
def detect_ecommerce_platform(url, url_map, timeout=10):
    try:
        if not url.startswith("http"):
            url = f"https://{url}"
        if not url.endswith("/"):
            url += "/"
        headers = {'User-Agent': get_random_user_agent()}
        
        proxy_dict, proxy_display = None, "直连"
        if valid_proxies:
            proxy_dict, proxy_display = get_random_proxy()
            logger.info(f"突入結界 → {url} （暗影：{proxy_display}）")

        # Shopify
        try:
            if proxy_dict:
                response = requests.get(f"{url}meta.json", headers=headers, proxies=proxy_dict, timeout=timeout)
            else:
                response = requests.get(f"{url}meta.json", headers=headers, timeout=timeout)
            if response.status_code == 200 and 'published_products_count' in response.json():
                logger.info(f"感知到Shopify查克拉！忍村确认 → {url}")
                return "Shopify", url
        except: pass

        # WooCommerce
        try:
            if proxy_dict:
                response = requests.get(f"{url}wp-json/wc/v3/products?per_page=1", headers=headers, proxies=proxy_dict, timeout=timeout)
            else:
                response = requests.get(f"{url}wp-json/wc/v3/products?per_page=1", headers=headers, timeout=timeout)
            if response.status_code == 200 and isinstance(response.json(), list):
                logger.info(f"WooCommerce的忍术波动……确认 → {url}")
                return "WooCommerce", url
        except: pass

        # Magento
        try:
            if proxy_dict:
                response = requests.get(f"{url}magento_version", headers=headers, proxies=proxy_dict, timeout=timeout)
            else:
                response = requests.get(f"{url}magento_version", headers=headers, timeout=timeout)
            if response.status_code == 200 and "Magento" in response.text:
                logger.info(f"Magento的古老封印……已破解 → {url}")
                return "Magento", url
        except: pass

        # Generic
        try:
            if proxy_dict:
                response = requests.get(url, headers=headers, proxies=proxy_dict, timeout=timeout)
            else:
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
                        logger.info(f"泛用忍具波动……判定为通用电商 → {url}")
                        return "Generic E-commerce", url
        except: pass

        myshopify_url = url_map.get(url.rstrip('/'))
        if myshopify_url:
            if not myshopify_url.endswith("/"):
                myshopify_url += "/"
            try:
                if proxy_dict:
                    response = requests.get(f"{myshopify_url}meta.json", headers=headers, proxies=proxy_dict, timeout=timeout)
                else:
                    response = requests.get(f"{myshopify_url}meta.json", headers=headers, timeout=timeout)
                if response.status_code == 200 and 'published_products_count' in response.json():
                    logger.info(f"隐藏的Shopify分身……现身吧！ → {myshopify_url}")
                    return "Shopify", myshopify_url
            except: pass

        logger.info(f"……查克拉感知失败。无电商忍术痕迹 → {url}")
        return None, None
    except Exception as e:
        logger.error(f"感知术暴走：{url} → {e}")
        return None, None

# ============== 尾兽玉级产品数量感知 ==============
def get_shopify_product_count(url, url_map, timeout=5):
    used_url = url
    final_url = url
    try:
        if not used_url.startswith("http"):
            used_url = f"https://{used_url}"
        if not used_url.endswith("/"):
            used_url += "/"
        headers = {'User-Agent': get_random_user_agent()}
        
        proxy_dict, proxy_display = None, "直连"
        if valid_proxies:
            proxy_dict, proxy_display = get_random_proxy()
            logger.info(f"尾兽玉感知 → {url} （暗影：{proxy_display}）")
        
        request_url = f"{used_url}meta.json"
        if proxy_dict:
            response = requests.get(request_url, headers=headers, proxies=proxy_dict, timeout=timeout)
        else:
            response = requests.get(request_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        meta_json = response.json()
        published_products_count = meta_json.get('published_products_count', 0)
        final_url = used_url
        logger.info(f"尾兽玉级感知完成 → {published_products_count}个忍具库存 → {final_url}")
        return published_products_count, final_url
    except:
        myshopify_url = url_map.get(url.rstrip('/'))
        if myshopify_url and used_url != myshopify_url:
            try:
                if not myshopify_url.endswith("/"):
                    myshopify_url += "/"
                headers = {'User-Agent': get_random_user_agent()}
                
                proxy_dict, proxy_display = None, "直连"
                if valid_proxies:
                    proxy_dict, proxy_display = get_random_proxy()
                    logger.info(f"分身感知 → {myshopify_url} （暗影：{proxy_display}）")
                
                request_url = f"{myshopify_url}meta.json"
                if proxy_dict:
                    response = requests.get(request_url, headers=headers, proxies=proxy_dict, timeout=timeout)
                else:
                    response = requests.get(request_url, headers=headers, timeout=timeout)
                response.raise_for_status()
                meta_json = response.json()
                published_products_count = meta_json.get('published_products_count', 0)
                final_url = myshopify_url
                logger.info(f"分身感知成功 → {published_products_count} → {final_url}")
                return published_products_count, final_url
            except: pass
        logger.info(f"感知失败……库存归零 → {url}")
        return 0, final_url

# ============== 情报封印术・实时保存 ==============
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
        logger.info(f"情报封印完成 → {doc['URL']} ({doc['E-commerce Platform']}) 已收入卷轴 {collection_name}")
        print(f"\n封印成功 → {doc['URL']} ({doc['E-commerce Platform']})，累计 {len(CURRENT_RESULTS)} 条情报")
    except Exception as e:
        logger.error(f"封印术失效：{e}")

# ============== 多重影分身・页面处理 ==============
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

# ============== 神威・搜索执行 ==============
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
        print(f"🌐 启动浏览器...")
        logger.info(f"启动浏览器，User-Agent: {user_agent}")
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(user_agent=user_agent, proxy=proxy)
        page = context.new_page()
        print(f"✅ 浏览器启动成功")
        # stealth_sync(page)

    def close_browser():
        nonlocal browser, context, page
        if page: page.close()
        if context: context.close()
        if browser: browser.close()

    try:
        start_browser()
        logger.info(f"开始搜索查询: {query}")
        print(f"🔍 开始搜索查询: {query}")
        while len(CURRENT_RESULTS) < max_results:
            start = current_page * page_size
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&start={start}"
            logger.info(f"访问搜索页面: {search_url}")
            try:
                page.goto(search_url, timeout=60000)
                logger.info(f"搜索页面加载成功")
                if page.locator("div.g-recaptcha, div.recaptcha").count() > 0:
                    print("……写轮眼感知到谷歌的结界术！（验证码）")
                    print("必须以肉眼破解……手动完成后按 Enter 继续神威！")
                    input("按 Enter 继续……")
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

                logger.info(f"从搜索页面提取到 {len(page_urls)} 个链接")
                print(f"📄 第 {current_page+1} 页，提取到 {len(page_urls)} 个链接")

                if not page_urls:
                    break

                if len(page_urls) <= 3:
                    consecutive_single_link_pages += 1
                    if consecutive_single_link_pages >= 2:
                        break
                else:
                    consecutive_single_link_pages = 0

                cleaned_urls, url_map = filter_urls(page_urls)
                logger.info(f"清洗后得到 {len(cleaned_urls)} 个有效URL")
                page_results = process_page_urls(cleaned_urls, url_map, group_size=10, min_product_count=min_product_count, category=category)
                all_results.extend(page_results)

                if len(CURRENT_RESULTS) >= max_results:
                    break

                current_page += 1
                time.sleep(random.uniform(3, 5))
            except PlaywrightTimeoutError as e:
                logger.error(f"搜索页面超时: {e}")
                print(f"⏱️ 搜索页面超时，重新启动浏览器...")
                close_browser()
                start_browser()
            except Exception as e:
                logger.error(f"搜索过程出错: {type(e).__name__}: {e}")
                print(f"❌ 搜索过程出错: {type(e).__name__}: {e}")
                close_browser()
                start_browser()

        close_browser()
        return all_results, []

    except Exception as e:
        logger.error(f"perform_search 严重错误: {type(e).__name__}: {e}")
        print(f"💥 perform_search 严重错误: {type(e).__name__}: {e}")
        close_browser()
        return all_results, []

# ============== 多重影分身・关键词搜索 ==============
def google_search_urls(keywords, max_results=10000000, min_product_count=200, proxy=None, category=None):
    all_results = []
    logger.info(f"google_search_urls 启动，关键词数量: {len(keywords)}")
    print(f"🚀 开始处理 {len(keywords)} 个关键词")
    with sync_playwright() as pw:
        logger.info("Playwright 初始化成功")
        for i, keyword in enumerate(keywords):
            keyword = keyword.strip()
            if not keyword:
                continue
            logger.info(f"处理关键词 {i+1}/{len(keywords)}: {keyword}")
            print(f"\n📝 处理关键词 {i+1}/{len(keywords)}: {keyword}")
            variants = [
                f"{keyword} inurl:collections/all",
                f"{keyword} inurl:collections/all - page 123",
                f"{keyword} inurl:collections/all - page 211"
            ]
            for query in variants:
                if len(CURRENT_RESULTS) >= max_results:
                    break
                try:
                    results, _ = perform_search(pw, query, max_results=max_results, min_product_count=min_product_count, proxy=proxy, category=category)
                    all_results.extend(results)
                except Exception as e:
                    logger.error(f"搜索查询 '{query}' 失败: {type(e).__name__}: {e}")
                    print(f"❌ 搜索查询 '{query}' 失败: {type(e).__name__}: {e}")
            if len(CURRENT_RESULTS) >= max_results:
                break
    logger.info(f"google_search_urls 结束，共找到 {len(all_results)} 个结果")
    return all_results

# ============== 终末主线・主函数 ==============
def main():
    global CURRENT_RESULTS

    print("""
    ╔════════════════════════════════════════════╗
    ║      火影忍界情报收集术・秽土转生篇        ║
    ║  吾乃宇智波斑的意志……今日开启月之眼计划！  ║
    ╚════════════════════════════════════════════╝
    """)

    json_files = sorted(glob.glob(os.path.join(KEYWORDS_DIR, "*.json")))
    if not json_files:
        print("……连禁术卷轴都不剩了。忍界已无吾容身之地……")
        sys.exit(1)

    print("发现以下禁术卷轴：")
    for i, fp in enumerate(json_files, 1):
        print(f"  {i}. {os.path.basename(fp)}")

    while True:
        try:
            choice = int(input("\n请选择要启封的禁术卷轴（输入序号）："))
            if 1 <= choice <= len(json_files):
                selected_file = json_files[choice - 1]
                break
        except ValueError:
            print("……连数字都写不对？查克拉都浪费了。")

    json_filename = os.path.basename(selected_file).replace(".json", "")
    print(f"\n已启封禁术卷轴 → {json_filename}.json")

    category = json_filename

    try:
        with open(selected_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"卷轴阅读失败：{e}")
        sys.exit(1)

    all_data = {}
    if isinstance(data, dict):
        all_data = data
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                all_data.update(item)

    if not all_data:
        print("……卷轴空空如也。连忍术都不剩。")
        sys.exit(1)

    big_cats = list(all_data.keys())
    selected_big = random.choice(big_cats)
    print(f"自动发动写轮眼随机选择大类：{selected_big}")

    sub_cats = all_data[selected_big]
    sub_list = list(sub_cats.keys())
    selected_sub = random.choice(sub_list)
    print(f"自动选择子分类：{selected_sub} ({len(sub_cats[selected_sub])} 个关键词)")

    keywords_all = sub_cats[selected_sub]
    selected_keywords = random.sample(keywords_all, min(20, len(keywords_all)))

    print(f"\n情报封印目标集合：{category}_Unfiltered_URLs")
    print(f"自动随机选取 {len(selected_keywords)} 个关键词：")
    for kw in selected_keywords:
        print(f"  - {kw}")

    print("\n开始发动秽土转生情报收集术（随时按 Ctrl+C 解除秽土）...\n")
    logger.info("开始发动秽土转生情报收集术")

    proxy = None
    print(f"🎭 浏览器将使用直连（先测试能否正常访问 Google）")
    logger.info(f"浏览器将使用直连")
    
    try:
        google_search_urls(selected_keywords, max_results=10000000, min_product_count=200, proxy=proxy, category=category)
    except Exception as e:
        logger.error(f"主搜索流程异常: {type(e).__name__}: {e}")
        print(f"💥 主搜索流程异常: {type(e).__name__}: {e}")

    print("\n" + "="*60)
    print("情报收集术・完结！")
    if CURRENT_RESULTS:
        print(f"共成功召唤并封印 {len(CURRENT_RESULTS)} 条店铺灵魂到集合：{category}_Unfiltered_URLs")
        for url, platform, count, domain, ts in CURRENT_RESULTS[-10:]:
            pc = f"{count}个忍具" if count else "库存未知"
            print(f"{url} → {platform}，{pc}")
    else:
        print("……连一个忍村都没召唤出来。月之眼计划又失败了……")

if __name__ == "__main__":
    main()