#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shopify 批量抓取 + MongoDB 存储（v9.5-direct-proxy）
更新：
- 完全切換到直接使用住宅代理池（不再走自建API）
- 代理輪換 + 自動重試
- MAX_WORKERS 降到 5 避免代理壓力過大
- 保留中二風格 log
- 使用统一的代理池管理模块
"""

import os
import time
import re
import ast
import json
import requests
import pandas as pd
from urllib.parse import urlparse
from pymongo import MongoClient
from threading import Thread, Lock
from queue import Queue
import random
from src.utils.proxy_manager import get_random_proxy, get_valid_proxies

# ---------------------- 基础配置 ----------------------
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"
CURRENCY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'currency_config.json')
LOG_FILE = os.path.join(os.path.dirname(__file__), 'data', 'logs', 'crawler_direct_proxy.log')
MAX_WORKERS = 5   # 建議 4~6，太多會把代理打爆
MAX_RETRY_PER_SITE = 3
REQUEST_TIMEOUT = 60
API_RETRY_WAIT = [5, 10, 20]  # 秒，重試間隔

# ---------------------- 日志配置 ----------------------
from src.utils.logger import setup_logger
logger = setup_logger('crawler', LOG_FILE)

# 初始化代理池
valid_proxies = get_valid_proxies()
logger.info(f"黑暗通道已開啟！裝填 {len(valid_proxies)} 個代理使魔……準備收割！")


# ---------------------- 工具函数 ----------------------
def build_meta_json_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}/meta.json"

def get_browser_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
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
    }

def api_fetch(target_url: str, method: str = "GET", headers: dict = None, timeout: int = REQUEST_TIMEOUT, max_retries: int = 3, allow_direct_fallback=True):
    if not valid_proxies:
        logger.error("代理池已枯竭……黑暗力量被封印！")
        return None, 0, "無代理可用"

    if headers is None:
        headers = get_browser_headers()

    for attempt in range(max_retries):
        proxy_dict, proxy_display = get_random_proxy()

        if proxy_dict is None:
            logger.error(f"第{attempt+1}次召喚失敗：無使魔可用")
            break

        try:
            logger.info(f"利爪撕裂結界 → {target_url} （使魔：{proxy_display}）")

            if method.upper() == "GET":
                resp = requests.get(
                    target_url,
                    headers=headers,
                    proxies=proxy_dict,
                    timeout=timeout
                )
            else:
                raise ValueError("僅支援 GET")

            status = resp.status_code
            body = resp.text

            if status == 200:
                logger.info(f"結界已破！ status=200 （使魔：{proxy_display}）")
                return {"status": status, "body": body}, status, proxy_display
            else:
                logger.info(f"目標反噬 status={status} （使魔：{proxy_display}）")
                # 非200不返回，换代理重试
                time.sleep(random.uniform(2, 5))
                continue

        except (requests.Timeout, requests.ConnectionError, requests.ProxyError) as e:
            logger.warning(f"使魔受創！ {proxy_display} → {type(e).__name__} (第{attempt+1}次)")
            time.sleep(random.uniform(API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT)-1)], API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT)-1)] + 5))
        except Exception as e:
            logger.error(f"次元崩壞！ {type(e).__name__} （使魔：{proxy_display}）")
            time.sleep(5)

    # 所有代理失败后，尝试直连兜底
    if allow_direct_fallback:
        logger.info("所有使魔皆敗，啟動直連禁術！")
        try:
            resp = requests.get(target_url, headers=headers, timeout=max(timeout, 15))
            status = resp.status_code
            body = resp.text
            if status == 200:
                logger.info("直連成功！ status=200")
                return {"status": status, "body": body}, status, "直連"
        except Exception as e:
            logger.error(f"直連禁術也失敗…… {e}")

    logger.error(f"已連續挑戰 {max_retries} 次……所有使魔皆被擊破！")
    return None, 0, "全部使魔失效"

# 以下函數保持不變（fetch_currency, get_json, extract_images 等）

def fetch_currency(url: str):
    meta_url = build_meta_json_url(url)
    api_data, status, proxy_used = api_fetch(meta_url, timeout=15, max_retries=2)

    if status == 200 and api_data:
        try:
            meta = json.loads(api_data["body"])
            currency = meta.get("currency", "USD")
            logger.info(f"禁忌知識已解封——此域之貨幣為「{currency}」！(使魔：{proxy_used} 已獻上忠誠)")
            return str(currency).upper() if currency else "USD"
        except:
            pass
    logger.info(f"可惡……貨幣的真名被結界封印了！此域恐非 Shopify 使魔……(代理{proxy_used}無能)")
    return ""

def get_json(url: str, page: int, max_retries: int = 3):
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    target_url = f"{url}/products.json?limit=200&page={page}"
    domain = urlparse(url).netloc.replace("www.", "")

    api_data, status, proxy_used = api_fetch(target_url, max_retries=max_retries)

    if status == 200 and api_data:
        try:
            json_content = json.loads(api_data["body"])
            logger.info(f"第{page}頁的封印已被吾之利爪撕裂！數據洪流湧入虛空庫存……(使魔{proxy_used}尚算可用)")
            return json_content, 200
        except json.JSONDecodeError:
            logger.error(f"第{page}頁的靈魂碎片解析失敗……次元崩壞！(使魔{proxy_used})")
            return None, 400

    logger.info(f"哼，區區{status}的魔力反噬？！使魔{proxy_used}，汝這廢物給吾記住了！")
    return None, status

# 以下 extract_ 函數保持原樣
def extract_images(images_column):
    match = re.search(r"'src'\s*:\s*'([^']+)'", str(images_column))
    return match.group(1).split("?")[0] if match else ''

def extract_prices(variants_column):
    m1 = re.search(r"'compare_at_price'\s*:\s*'([^']+)'", str(variants_column))
    m2 = re.search(r"'price'\s*:\s*'([^']+)'", str(variants_column))
    return m1.group(1) if m1 else '', m2.group(1) if m2 else ''

def extract_variants(options_column):
    try:
        options_list = ast.literal_eval(str(options_column))
        variant_str = ''
        for option in options_list:
            name = option.get('name')
            if not name or name == 'Title':
                continue
            values = option.get('values', [])
            if values:
                variant_str += f"{name}^{'#'.join(values)}|||"
        return variant_str.strip('|||')
    except:
        return ''

# crawl_site、worker、主程序保持原樣，只需確保 api_fetch 已替換

# ---------------------- 单站点爬取 ----------------------
# （以下內容與你原程式相同，僅貼出結構，實際執行時直接用上面的 api_fetch）

def crawl_site(task):
    url, category1, category2, CURRENCY_MAP = task
    domain = urlparse(url).netloc.replace("www.", "")
    task_id = url.strip().rstrip("/")

    phrases = [
        "黑暗的低語已響起……",
        "吾之利爪已飢渴難耐！",
        "讓吾來撕開這虛假的和平吧！",
        "又一個愚蠢的凡人領域……",
        "禁忌的爬取儀式，現在開始！",
        "汝等，準備迎接滅亡了嗎？"
    ]
    logger.info(f"{random.choice(phrases)} {domain}，吾來了！")

    with site_lock:
        if task_id in active_sites:
            return
        active_sites.add(task_id)

    try:
        logger.info(f"……暗黑之瞳已鎖定目標。{domain}，汝之命運由吾來收割！⚡")
        coll_name = category1 if category1 else (category2 or "uncategorized")

        with db_lock:
            coll = db[coll_name]

        currency = fetch_currency(url)
        if not currency:
            logger.error(f"非 Shopify 領域，無法解析貨幣：{domain}——吾之使魔也無能為力！")
            return
        currency_key = currency.upper()
        if currency_key not in CURRENCY_MAP:
            logger.error(f"未知的禁忌貨幣符號：{currency}……此領域已超越吾之認知！")
            return

        rate = CURRENCY_MAP[currency_key]
        all_products = []
        total_raw = 0
        total_dup_in_pages = 0
        retry_count = 0

        while retry_count < MAX_RETRY_PER_SITE:
            logger.info(f"{domain} → 嘗試第 {retry_count + 1}/{MAX_RETRY_PER_SITE} 次黑暗召喚")

            page = 1
            empty_pages = 0
            site_success = False

            while empty_pages < 2:
                json_data, status_code = get_json(url, page)

                if status_code != 200:
                    if status_code in (502, 503):
                        time.sleep(30 * (retry_count + 1))
                    break

                if not json_data or 'products' not in json_data:
                    empty_pages += 1
                    page += 1
                    continue

                df = pd.DataFrame(json_data['products'])
                if df.empty:
                    empty_pages += 1
                    page += 1
                    continue

                raw_products = []
                for _, row in df.iterrows():
                    title = row.get('title', '')
                    if not title:
                        continue
                    img = extract_images(row.get('images', ''))
                    ori_p, dis_p = extract_prices(row.get('variants', ''))
                    variants = extract_variants(row.get('options', ''))
                    category = row.get('product_type', '') or ''

                    def conv(p):
                        try:
                            return round(float(p) * rate, 2)
                        except:
                            return ''

                    ori = conv(ori_p)
                    dis = conv(dis_p)
                    price = dis if dis != '' else ori
                    if price == '' or price < 1:
                        continue
                    raw_products.append({
                        'SKU': '',
                        '標題': title,
                        '描述': row.get('body_html', ''),
                        '圖片': img,
                        '原價': ori,
                        '折扣價': dis,
                        '變體': variants,
                        '分類': category,
                        'currency': currency,
                        'category2': category2,
                    })

                raw_count = len(raw_products)
                total_raw += raw_count

                seen = set()
                unique_in_page = []
                dup_in_page = 0
                for p in raw_products:
                    key = p['標題']
                    if key in seen:
                        dup_in_page += 1
                    else:
                        seen.add(key)
                        unique_in_page.append(p)
                total_dup_in_pages += dup_in_page

                logger.info(f"第{page}頁獻祭了{raw_count}個靈魂……可惜其中{dup_in_page}個不過是鏡像的殘渣罷了。")

                all_products.extend(unique_in_page)
                empty_pages = 0
                page += 1
                site_success = True
                time.sleep(0.3)

            if site_success:
                break
            else:
                retry_count += 1

        if all_products:
            seen_global = set()
            final = []
            for p in all_products:
                if p['標題'] not in seen_global:
                    seen_global.add(p['標題'])
                    final.append(p)

            with db_lock:
                coll_new = db_new[coll_name]

                existing = {
                    d['標題']
                    for d in coll_new.find({"標題": {"$in": [p['標題'] for p in final]}}, {"標題": 1})
                }
                to_insert = [p for p in final if p['標題'] not in existing]

                if to_insert:
                    coll_new.insert_many(to_insert)
                    logger.info(
                        f"……{len(to_insert)}個新的靈魂已被刻入永恆水晶。累計吞噬{total_raw}，重複的幻影有{total_dup_in_pages}個，已被湮滅。"
                    )
                else:
                    logger.info(f"此領域已無新鮮靈魂可供收割……累計{total_raw}，{total_dup_in_pages}個不過是昨日的殘響。")
        else:
            logger.info(f"空無一物……連靈魂的殘渣都不剩。{domain}，汝已墜入真正的虛無。")

    except Exception as e:
        logger.error(f"【警報】次元裂隙失控！{domain}的反噬之力過於強大……(錯誤碎片：{str(e)[:120]}) 吾……暫且撤退！")
        logger.error(f"……可惡，此次討伐以失敗告終。{domain}，下次吾必將汝徹底吞噬！(╯°□°）╯︵ ┻━┻")

    finally:
        with site_lock:
            active_sites.discard(task_id)

# ---------------------- Worker 线程 ----------------------
def worker():
    while True:
        task = task_queue.get()
        if task is None:
            break
        crawl_site(task)
        task_queue.task_done()
        time.sleep(1.5)

# ---------------------- 主程序 ----------------------
if __name__ == "__main__":
    # 載入匯率
    if not os.path.exists(CURRENCY_CONFIG_PATH):
        raise FileNotFoundError(f"匯率文件不存在: {CURRENCY_CONFIG_PATH}")

    with open(CURRENCY_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    CURRENCY_MAP = {}
    if isinstance(data, list):
        for item in data:
            k = item.get("nation")
            v = item.get("exchange_rate_usd")
            if k and v is not None:
                CURRENCY_MAP[str(k).upper()] = float(v)
    elif isinstance(data, dict):
        for k, v in data.items():
            CURRENCY_MAP[str(k).upper()] = float(v)

    logger.info(f"虛空議會已讀取{len(CURRENCY_MAP)}種被詛咒的貨幣法則……準備就緒。")

    # 初始化 MongoDB
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    db_new = client["shopify_data_new"]

    # 全局鎖
    site_lock = Lock()
    db_lock = Lock()
    active_sites = set()
    task_queue = Queue()

    # 從 DB 載入任務
    filtered_suffix = "_Filtered_URLs"
    tasks = []

    try:
        coll_names = db.list_collection_names()
    except Exception as e:
        logger.error(f"虛空之門開啟失敗……無法窺探資料庫深淵：{e}")
        coll_names = []

    for coll in coll_names:
        if not coll.endswith(filtered_suffix):
            continue
        base_name = coll[:-len(filtered_suffix)] or "uncategorized"
        src_coll = db[coll]
        try:
            cursor = src_coll.find({}, {"URL": 1, "category1": 1, "category2": 1})
            count = 0
            for doc in cursor:
                url = doc.get("URL") or doc.get("url") or ""
                url = str(url).strip()
                if not url:
                    continue
                cat1 = base_name
                cat2 = str(doc.get("category2") or doc.get("category") or "").strip()
                tasks.append((url, cat1, cat2, CURRENCY_MAP))
                count += 1
            logger.info(f"從深淵藏書閣「{coll}」中召喚出{count}個待滅絕的靈魂，目標祭壇：{base_name}")
        except Exception as e:
            logger.error(f"讀取禁忌之卷「{coll}」時，觸發了古老詛咒：{e}")

    random.shuffle(tasks)

    for t in tasks:
        task_queue.put(t)

    total = task_queue.qsize()
    logger.info(f"虛空議會已下達{total}道討伐令！{MAX_WORKERS}具黑暗分身自深淵蘇醒，準備執行滅絕協議！")

    threads = []
    for i in range(MAX_WORKERS):
        t = Thread(target=worker, name=f"Dark-Clone-{i + 1}")
        t.start()
        threads.append(t)

    task_queue.join()

    for _ in range(MAX_WORKERS):
        task_queue.put(None)
    for t in threads:
        t.join()

    logger.info("……儀式結束。所有被選中的領域已臣服於永恆之暗。今天，吾允許自己小憩片刻。")

    # 清空所有 _Filtered_URLs 集合
    logger.info("現在，啟動「記憶抹除・大淨化」儀式……所有_Filtered_URLs將被獻祭給虛空！")
    for coll in coll_names:
        if coll.endswith(filtered_suffix):
            try:
                result = db[coll].delete_many({})
                logger.info(f"{coll}的殘魂已徹底湮滅，{result.deleted_count}道痕跡被無情抹去。")
            except Exception as e:
                logger.error(f"淨化儀式在{coll}處引發反噬……{e}")

    logger.info("淨化完成。世界再度歸於寂靜……直到下一次暗潮湧動。")