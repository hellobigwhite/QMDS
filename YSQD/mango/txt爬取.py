#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shopify 批量抓取（最终版）
表头：SKU, 标题, 描述, 子描述, 图片, 原价, 折扣价, 变体名, 变体值, 分类
每爬完一个网站保存一次 + 详细日志 + 防403/429
"""

import os
import time
import re
import ast
import json
import requests
import pandas as pd
from urllib.parse import urlparse
from threading import Thread, RLock
from queue import Queue
import random
from src.utils.proxy_manager import get_random_proxy, get_valid_proxies

# ---------------------- 配置 ----------------------
CURRENCY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'currency_config.json')
DESKTOP_PATH = os.path.expanduser("~/Desktop")
TASKS_TXT_PATH = os.path.join(DESKTOP_PATH, "tasks.txt")
LOG_FILE = os.path.join(os.path.dirname(__file__), 'data', 'logs', 'txt_crawler.log')

SAVE_FOLDER = os.path.join(os.path.dirname(__file__), 'data', 'output')
if not os.path.exists(SAVE_FOLDER):
    os.makedirs(SAVE_FOLDER)

EXCEL_OUTPUT_PATH = os.path.join(SAVE_FOLDER, "shopify_products.xlsx")

MAX_WORKERS = 4
MAX_RETRY_PER_SITE = 3
API_RETRY_WAIT = [10, 20, 40]

# 初始化代理池
valid_proxies = get_valid_proxies()
print(f"🚀 代理池已装填 {len(valid_proxies)} 个代理...")

file_lock = RLock()

# ---------------------- 日志 ----------------------
from src.utils.logger import setup_logger
logger = setup_logger('txt_crawler', LOG_FILE)

# ---------------------- 请求头 ----------------------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edge/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/"
    }


# ---------------------- 请求 ----------------------
def build_meta_json_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}/meta.json"

def local_fetch(url, timeout=30, max_retries=3, allow_direct_fallback=True):
    if not valid_proxies:
        logger.warning("代理池为空，将使用直连...")
    
    for attempt in range(max_retries):
        headers = get_random_headers()
        
        proxy_dict, proxy_display = None, "直连"
        if valid_proxies:
            proxy_dict, proxy_display = get_random_proxy()
        
        try:
            logger.info(f"🌐 请求 {url} （代理：{proxy_display}）")
            
            if proxy_dict:
                r = requests.get(url, headers=headers, proxies=proxy_dict, 
                              timeout=timeout, allow_redirects=True)
            else:
                r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            
            if r.status_code == 200:
                logger.info(f"✅ 请求成功 {url}")
                return r.json(), 200
            
            logger.warning(f"⚠️ 失败 {r.status_code} | {url} | 第{attempt+1}次重试（代理：{proxy_display}）")
            time.sleep(API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT)-1)])
        
        except (requests.Timeout, requests.ConnectionError, requests.ProxyError) as e:
            logger.warning(f"⚠️ 代理异常 {proxy_display} → {type(e).__name__} （第{attempt+1}次）")
            time.sleep(API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT)-1)])
        
        except Exception as e:
            logger.error(f"❌ 异常 {str(e)[:80]}")
            time.sleep(API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT)-1)])
    
    # 所有代理失败后尝试直连兜底
    if allow_direct_fallback and valid_proxies:
        logger.warning(f"🔄 所有代理失败，尝试直连: {url}")
        try:
            r = requests.get(url, headers=get_random_headers(), timeout=max(timeout, 15), allow_redirects=True)
            if r.status_code == 200:
                logger.info(f"✅ 直连成功 {url}")
                return r.json(), 200
        except Exception as e:
            logger.error(f"❌ 直连也失败: {str(e)[:80]}")
    
    return None, 0

def fetch_currency(url):
    data, status = local_fetch(build_meta_json_url(url), timeout=15)
    if status == 200 and data:
        currency = data.get("currency", "USD")
        return str(currency).upper() if currency else "USD"
    return ""

def get_json(url, page):
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    target = f"{url}/products.json?limit=200&page={page}"
    logger.info(f"📄 抓取第 {page} 页：{target}")
    return local_fetch(target, max_retries=4)

# ---------------------- 解析 ----------------------
def extract_images(images):
    if not images:
        return ""
    m = re.search(r"'src'\s*:\s*'([^']+)'", str(images))
    return m.group(1).split("?")[0] if m else ""

def extract_variant_info(variants, options):
    try:
        sku = variants[0].get("sku", "") if isinstance(variants, list) and variants else ""
        variant_names = []
        variant_values = []
        if isinstance(options, list):
            for opt in options:
                name = opt.get("name", "")
                vals = opt.get("values", [])
                if name and name != "Title":
                    variant_names.append(name)
                    variant_values.append("/".join(map(str, vals)))
        return sku, " | ".join(variant_names), " | ".join(variant_values)
    except:
        return "", "", ""

def extract_prices(variants):
    try:
        if not isinstance(variants, list) or len(variants) == 0:
            return "", ""
        v = variants[0]
        return v.get("compare_at_price", ""), v.get("price", "")
    except:
        return "", ""

# ---------------------- 保存（爬完一个网站保存一次） ----------------------
def save_website(rows):
    if not rows:
        return
    with file_lock:
        try:
            df_old = pd.read_excel(EXCEL_OUTPUT_PATH, engine="openpyxl") if os.path.exists(EXCEL_OUTPUT_PATH) else pd.DataFrame()
            df_new = pd.DataFrame(rows)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
            df_all.drop_duplicates(subset=["SKU", "标题", "图片"], keep="first", inplace=True)
            df_all.to_excel(EXCEL_OUTPUT_PATH, index=False, engine="openpyxl")
            logger.info(f"💾 网站保存成功 | 本次 {len(rows)} 条")
        except Exception as e:
            logger.error(f"❌ 保存失败：{str(e)}")

# ---------------------- 爬取网站 ----------------------
def crawl_site(task):
    url, cat1, cat2, CURRENCY_MAP = task
    domain = urlparse(url).netloc.replace("www.", "")
    logger.info("="*70)
    logger.info(f"🚀 开始爬取：{domain}")
    logger.info(f"🔗 {url}")
    logger.info("="*70)

    rows = []
    try:
        currency = fetch_currency(url)
        if not currency:
            logger.error(f"❌ 非 Shopify 站点（meta.json 无响应），跳过：{domain}")
            return
        if currency.upper() not in CURRENCY_MAP:
            logger.error(f"❌ 货币不支持：{currency}")
            return
        rate = CURRENCY_MAP[currency.upper()]
        logger.info(f"💱 汇率：1 {currency} = {rate} USD")

        for retry in range(MAX_RETRY_PER_SITE):
            logger.info(f"🔁 第 {retry+1}/{MAX_RETRY_PER_SITE} 次尝试")
            page = 1
            empty = 0
            while empty < 3:
                data, code = get_json(url, page)
                if code != 200:
                    break
                if not data or "products" not in data or not data["products"]:
                    empty += 1
                    page += 1
                    time.sleep(random.uniform(6, 10))
                    continue

                products = data["products"]
                logger.info(f"✅ 第{page}页：{len(products)} 个商品")

                for p in products:
                    title = p.get("title", "").strip()
                    if not title:
                        continue

                    desc = p.get("body_html", "")
                    sub_desc = p.get("tags", "")
                    images = p.get("images", [])
                    variants = p.get("variants", [])
                    options = p.get("options", [])
                    product_type = p.get("product_type", "")

                    img = extract_images(images)
                    sku, var_names, var_values = extract_variant_info(variants, options)
                    compare, price = extract_prices(variants)

                    def c(v):
                        try:
                            return round(float(v)*rate, 2)
                        except:
                            return ""
                    compare = c(compare)
                    price = c(price)

                    rows.append({
                        "SKU": sku,
                        "标题": title,
                        "描述": desc,
                        "子描述": sub_desc,
                        "图片": img,
                        "原价": compare,
                        "折扣价": price,
                        "变体名": var_names,
                        "变体值": var_values,
                        "分类": product_type
                    })

                page += 1
                empty = 0
                time.sleep(random.uniform(8, 12))
            if rows:
                break

        logger.info(f"🏁 {domain} 完成：共 {len(rows)} 条")
        save_website(rows)

    except Exception as e:
        logger.error(f"💥 {domain} 异常：{str(e)[:200]}")
    finally:
        t = random.uniform(20, 30)
        logger.info(f"⏳ 网站冷却：{t:.1f}s")
        time.sleep(t)

# ---------------------- 线程 ----------------------
def worker():
    while True:
        task = task_queue.get()
        if task is None:
            break
        crawl_site(task)
        task_queue.task_done()

# ---------------------- 加载任务 ----------------------
def load_tasks(path, cm):
    if not os.path.exists(path):
        logger.error(f"❌ 无任务文件：{path}")
        return []
    tasks = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            url = parts[0]
            c1 = parts[1] if len(parts) > 1 else ""
            c2 = parts[2] if len(parts) > 2 else ""
            tasks.append((url, c1, c2, cm))
    logger.info(f"📋 加载任务：{len(tasks)} 个")
    return tasks

# ---------------------- 主程序 ----------------------
if __name__ == "__main__":
    logger.info("")
    logger.info("="*70)
    logger.info("🛒 Shopify 多线程爬取（最终版）")
    logger.info("表头：SKU,标题,描述,子描述,图片,原价,折扣价,变体名,变体值,分类")
    logger.info("="*70)
    logger.info("")

    if not os.path.exists(CURRENCY_CONFIG_PATH):
        CURRENCY_CONFIG_PATH = os.path.join(DESKTOP_PATH, "currency_config.json")
        if not os.path.exists(CURRENCY_CONFIG_PATH):
            logger.error("❌ 缺少 currency_config.json")
            exit(1)

    with open(CURRENCY_CONFIG_PATH, "r", encoding="utf-8") as f:
        c_data = json.load(f)

    CURRENCY_MAP = {}
    if isinstance(c_data, list):
        for item in c_data:
            n = item.get("nation", "").upper()
            r = item.get("exchange_rate_usd")
            if n and isinstance(r, (int, float)):
                CURRENCY_MAP[n] = float(r)
    else:
        for k, v in c_data.items():
            CURRENCY_MAP[k.upper()] = float(v)

    tasks = load_tasks(TASKS_TXT_PATH, CURRENCY_MAP)
    if not tasks:
        logger.error("❌ 无有效任务")
        exit(1)

    random.shuffle(tasks)
    task_queue = Queue()
    for t in tasks:
        task_queue.put(t)

    threads = []
    for i in range(MAX_WORKERS):
        th = Thread(target=worker, name=f"W-{i+1}")
        th.daemon = True
        th.start()
        threads.append(th)

    task_queue.join()
    for _ in range(MAX_WORKERS):
        task_queue.put(None)
    for t in threads:
        t.join()

    logger.info("")
    logger.info("🎉 全部完成！")
    logger.info(f"📊 文件：{EXCEL_OUTPUT_PATH}")