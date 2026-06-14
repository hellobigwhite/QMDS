#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shopify 領域過濾・聖裁之刃 Ver.2
- 所有請求皆經由禁忌之門 http://194.195.86.228:8899/fetch 穿越
- 刻印 proxy_used 於戰報之中
- 內建 API 重生機制 & 502 領域停滯對抗
- 唯有真正斬獲連結，方可抹除源表存在
- 此刻，吾等將以燃燒的意志，撕裂虛偽之網！
"""

import os
import time
import re
import json
import logging
import requests
import difflib
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from pymongo import MongoClient
from src.utils.proxy_manager import get_random_proxy, get_valid_proxies

# ================== 燃燒吧！中二戰吼日誌引擎 ==================
from src.utils.logger import setup_logger

class ChuunibyouFlameFormatter(logging.Formatter):
    def format(self, record):
        level = record.levelno
        emblem = {
            logging.DEBUG:    "⚡",
            logging.INFO:     "炎",
            logging.WARNING:  "焔",
            logging.ERROR:    "滅",
        }.get(level, "黒")

        prefix = {
            logging.INFO:     "【戰況報告】",
            logging.WARNING:  "【警戒領域展開】",
            logging.ERROR:    "【崩壞警報！！】",
        }.get(level, "【次元裂隙】")

        msg = super().format(record)
        return f"〔{emblem}〕 {prefix} {msg} 〔{emblem}〕"

# 设置日志
logger = setup_logger('shopify_category', os.path.join(os.path.dirname(__file__), 'data', 'logs', 'shopify_category.log'))
# 应用自定义格式化器
for handler in logger.handlers:
    handler.setFormatter(ChuunibyouFlameFormatter())


# ================== 基本領域設定 ==================
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "shopify_url"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_JSON_PATH = os.path.join(PROJECT_DIR, 'config', 'bigdata.json')

try:
    with open(MASTER_JSON_PATH, encoding="utf-8") as f:
        MASTER_DATA = json.load(f)
    logger.info(f"禁忌之書《bigdata.json》已解封！內封印著 {len(MASTER_DATA)} 個終末大分類！吾之力量正在覺醒！")
except Exception as e:
    logger.error(f"《bigdata.json》封印破解失敗……！ {e} 這份屈辱，吾銘記於心！")
    raise SystemExit(1)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)


# ================== 禁忌代理通道・開啟 ==================
# 使用统一的代理池管理模块
valid_proxies = get_valid_proxies()
logger.info(f"代理池已裝填 {len(valid_proxies)} 個暗影通道……準備撕裂虛空！")

# ================== 取代原本的 api_fetch ==================
def direct_fetch(target_url: str, method: str = "GET", headers: dict = None, timeout: int = 15, max_retries: int = 4, allow_direct_fallback=True):
    if not valid_proxies:
        logger.error("代理池空無一物……吾之力量被封印了！")
        return None, 0, "無代理可用"

    if headers is None:
        headers = {
            "User-Agent": get_random_user_agent(),
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

    original_timeout = timeout

    for attempt in range(max_retries):
        proxy_dict, proxy_display = get_random_proxy()

        if proxy_dict is None:
            logger.error(f"第{attempt+1}次召喚暗影失敗……無暗影可用")
            break

        try:
            logger.info(f"突入結界 → {target_url} （暗影：{proxy_display}）")

            if method.upper() == "GET":
                resp = requests.get(
                    target_url,
                    headers=headers,
                    proxies=proxy_dict,
                    timeout=timeout
                )
            elif method.upper() == "POST":
                raise NotImplementedError("暫只支援 GET")
            else:
                raise ValueError("未知方法")

            status = resp.status_code
            body = resp.text
            proxy_used = proxy_display

            if status == 200:
                logger.info(f"結界攻略成功！ status=200 （暗影：{proxy_used}）")
                return {"status": status, "body": body}, status, proxy_used
            else:
                logger.info(f"結界回擊 status={status} （暗影：{proxy_used}）")
                time.sleep(random.uniform(2, 5))
                continue

        except (requests.Timeout, requests.ConnectionError, requests.ProxyError) as e:
            logger.warning(f"暗影受創！ {proxy_display} → {type(e).__name__} （第{attempt+1}次）")
            time.sleep(random.uniform(4, 12))
            timeout = min(original_timeout + 5, 30)
            continue
        except Exception as e:
            logger.error(f"次元崩壞！ {type(e).__name__} （暗影：{proxy_display}）")
            time.sleep(3)
            continue

    # 所有代理失败后尝试直连兜底
    if allow_direct_fallback:
        logger.info("所有暗影皆滅……啟動直連禁術！")
        try:
            resp = requests.get(target_url, headers=headers, timeout=max(timeout, 15))
            if resp.status_code == 200:
                logger.info("直連成功！ status=200")
                return {"status": 200, "body": resp.text}, 200, "直連"
        except Exception as e:
            logger.error(f"直連禁術也失敗…… {e}")

    logger.error(f"已連續挑戰 {max_retries} 次……所有暗影皆被擊破，此路不通！")
    return None, 0, "全部暗影失效"

# ================== 全局替換：把原本的 api_fetch 改成 direct_fetch ==================
# 在程式中搜尋所有 api_fetch( 並改成 direct_fetch(
# 回傳格式相同：(api_data_dict, status, proxy_used)
# 所以其他地方的判斷邏輯（如 if status == 200）可以保持不變


# ================== 魔導資料庫接続 ==================
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
all_collections = db.list_collection_names()
BIG_CATEGORIES = [name.replace("_Unfiltered_URLs", "") for name in all_collections if name.endswith("_Unfiltered_URLs")]

if not BIG_CATEGORIES:
    logger.error("連一個未淨化領域都沒發現……這片虛無，吾無法接受！")
    raise SystemExit(1)

logger.info(f"已鎖定 {len(BIG_CATEGORIES)} 個終末領域！今日，吾等將親手開啟審判！")


# ================== 聖裁判定函式 ==================
def smart_normalize(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^\w\s&'-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def extract_all_phrases(data, result=None):
    if result is None:
        result = set()
    if isinstance(data, dict):
        for k, v in data.items():
            norm = smart_normalize(k)
            if norm: result.add(norm)
            extract_all_phrases(v, result)
    elif isinstance(data, list):
        for item in data:
            extract_all_phrases(item, result)
    elif isinstance(data, str):
        norm = smart_normalize(data)
        if norm: result.add(norm)
    return result


def is_garbage_collection(title: str, handle: str) -> bool:
    h = (handle or "").lower()
    blacklist = {"all", "new", "sale", "gift", "brand", "accessories", "apparel", "collection", "t-shirt", "hoodie",
                 "shirt", "bundle", "parts", "tools", "supplies"}
    if any(x in h for x in blacklist) or len((title or "").strip()) <= 4:
        return True
    return False


DOMAIN_KEYWORDS = []


def domain_matches_manual(domain: str) -> bool:
    d = re.sub(r"^https?://", "", (domain or "").lower())
    d = re.sub(r"^www\.", "", d).split("/")[0].split("?")[0]
    d = re.sub(r"\.(com|net|org|co\.uk|shop|store|ca|au|us|io).*", "", d)
    d = re.sub(r"[^\w]", "", d)
    for kw in DOMAIN_KEYWORDS:
        kw = kw.lower()
        if kw in d or (kw.endswith("s") and kw[:-1] in d):
            return True
    return False


def strong_match(title: str, phrases_set) -> bool:
    t = smart_normalize(title)
    for phrase in phrases_set:
        if len(phrase) >= 3 and phrase in t:
            return True
    for phrase in phrases_set:
        if len(phrase) >= 5:
            ratio = difflib.SequenceMatcher(None, t, phrase).ratio()
            if ratio >= 0.88:
                return True
    return False


def description_matches(domain: str, phrases_set):
    url = f"{domain.rstrip('/')}/meta.json"
    logger.info(f"正在侵入終末檔案 → {url}")
    api_data, status, proxy_used = direct_fetch(url, timeout=15, max_retries=2)

    if status == 200 and api_data:
        try:
            meta = json.loads(api_data["body"])
            desc = smart_normalize(meta.get("description", "") or "")
            matched = any(p in desc for p in phrases_set if len(p) >= 4)
            if matched:
                logger.info(f"禁忌記述確認命中！ {url} 已落入吾等掌控！（代理：{proxy_used}）")
            else:
                logger.info(f"記述之中並無吾等之刻印……（代理：{proxy_used}）")
            return matched
        except Exception as e:
            logger.warning(f"終末檔案解讀崩壞 → {e} （代理：{proxy_used}）")
    else:
        logger.info(f"無法觸及終末檔案… status={status} （代理：{proxy_used}）")
    return False


def fetch_collections_with_pagination(base_url, max_pages=60):
    all_cols = []
    for page in range(1, max_pages + 1):
        url = f"{base_url}/collections.json?page={page}"
        logger.info(f"突入第 {page} 結界 → {url}")
        api_data, status, proxy_used = direct_fetch(url, timeout=15, max_retries=3)

        if status != 200:
            if page == 1:
                fallback = f"{base_url}/collections.json"
                logger.info(f"初回結界即遭反噬… 嘗試無頁碼禁術 → {fallback}")
                api_data, status, proxy_used = direct_fetch(fallback, timeout=15, max_retries=2)
            if status != 200:
                logger.info(f"結界完全封鎖… status={status} （代理：{proxy_used}）")
                break

        if api_data and status == 200:
            try:
                cols = json.loads(api_data["body"]).get("collections", [])
                if not cols:
                    break
                all_cols.extend(cols)
                logger.info(f"第{page}結界攻略成功！ 捕獲 {len(cols)} 個次級領域（代理：{proxy_used}）")
                time.sleep(random.uniform(0.3, 0.8))
            except:
                logger.warning(f"結界內部情報解析崩壞（代理：{proxy_used}）")
                break
        else:
            break
    return all_cols


def process_domain(args):
    domain, phrases_set = args
    domain = (domain or "").strip()
    if not domain:
        return []
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    base_url = domain.rstrip("/")

    logger.info(f"目標領域鎖定 → {base_url}  開始執行聖裁！")

    results = []

    try:
        if domain_matches_manual(base_url):
            logger.info(f"域名刻印直擊要害！ {base_url} 已無抵抗之力！")
            results.append(base_url)
            return results

        if description_matches(base_url, phrases_set):
            results.append(base_url)
            return results

        cols = fetch_collections_with_pagination(base_url)
        for c in cols:
            title = (c.get("title") or "").strip()
            handle = c.get("handle") or ""
            if not title or not handle:
                continue
            if is_garbage_collection(title, handle):
                continue
            if strong_match(title, phrases_set):
                url = f"{base_url}/collections/{handle}"
                logger.info(f"強力共鳴確認！ 次級領域 {url} 已納入吾之支配！")
                results.append(url)

        return results

    except Exception as e:
        logger.error(f"領域攻略中發生致命扭曲！ → {e}")
        return []
    finally:
        time.sleep(random.uniform(0.5, 1.5))


# ================== 最終審判・開幕 ==================
def main():
    logger.info("全領域解放！ 此刻，吾等將以不滅之焰，焚盡一切虛偽！")

    for BIG_CATEGORY in BIG_CATEGORIES:
        logger.info(f"第 {BIG_CATEGORY} 終末領域・審判開始！")

        cat_config = MASTER_DATA.get(BIG_CATEGORY)
        if not cat_config:
            logger.warning(f"[{BIG_CATEGORY}] 之刻印已佚失……暫時放過此領域！")
            continue

        SRC_COL_NAME = f"{BIG_CATEGORY}_Unfiltered_URLs"
        DST_COL_NAME = f"{BIG_CATEGORY}_Filtered_URLs"
        col_src = db[SRC_COL_NAME]
        col_dst = db[DST_COL_NAME]

        # domain_keywords
        domain_kw_data = cat_config.get("domain_keywords", [])
        global DOMAIN_KEYWORDS
        if isinstance(domain_kw_data, dict):
            DOMAIN_KEYWORDS = [str(x).strip() for v in domain_kw_data.values() for x in (v if isinstance(v, list) else [v]) if x]
        elif isinstance(domain_kw_data, list):
            DOMAIN_KEYWORDS = [str(x).strip() for x in domain_kw_data if x]
        else:
            DOMAIN_KEYWORDS = []
        logger.info(f"[{BIG_CATEGORY}] 已裝填 {len(DOMAIN_KEYWORDS)} 枚禁忌刻印！")

        # phrases
        raw_categories = cat_config.get("categories", {})
        sub_data = raw_categories.get(BIG_CATEGORY, raw_categories)
        selected_phrases = extract_all_phrases(sub_data)
        logger.info(f"[{BIG_CATEGORY}] 共解析出 {len(selected_phrases)} 道真言！")

        domains = [d.get("URL") for d in col_src.find({}, {"URL": 1}) if d.get("URL")]
        if not domains:
            logger.info(f"[{BIG_CATEGORY}] 領域內空無一物……此次戰鬥無意義，撤退！")
            continue

        logger.info(f"[{BIG_CATEGORY}] 目標數量：{len(domains)}  第三十二幻影突擊隊・出擊！")

        all_links = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(process_domain, (dom, selected_phrases)) for dom in domains]
            for f in tqdm(as_completed(futures), total=len(domains), desc=f"〔{BIG_CATEGORY}・聖戰進行中〕"):
                try:
                    links = f.result()
                    all_links.extend(links)
                except Exception as e:
                    logger.debug(f"幻影突擊隊成員斷線 → {e}")

        final_links = sorted(set(all_links))
        logger.info(f"[{BIG_CATEGORY}] 去蕪存菁後獲得 {len(final_links)} 個真實刻印！")

        try:
            col_dst.delete_many({})
            logger.info(f"[{BIG_CATEGORY}] 已將舊有領域 {DST_COL_NAME} 完全焚燒殆盡！")
        except Exception as e:
            logger.error(f"焚燒舊領域失敗 → {e}")

        saved = 0
        for url in final_links:
            try:
                col_dst.update_one(
                    {"URL": url},
                    {"$set": {
                        "URL": url,
                        "Category": BIG_CATEGORY,
                        "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }},
                    upsert=True
                )
                saved += 1
            except:
                pass
        logger.info(f"[{BIG_CATEGORY}] 成功刻印 {saved} 個新領域至 {DST_COL_NAME}！")

        # ─── 最終抹消 ───────────────────────────────
        if saved > 0:
            try:
                delete_result = col_src.delete_many({})
                logger.info(
                    f"[{BIG_CATEGORY}] 聖裁完成！ 源領域 {SRC_COL_NAME} 內 {delete_result.deleted_count} 個虛像已徹底抹消！"
                )
            except Exception as e:
                logger.error(f"抹消源領域時發生扭曲 → {e}")
        else:
            logger.warning(
                f"[{BIG_CATEGORY}] 未獲得任何真實刻印……源領域暫時苟延殘喘，待下次審判！"
            )
        # ────────────────────────────────────────────────

    logger.info("全領域審判完結！ 吾等之意志，已在這片虛空中刻下永恆的火焰！ 今日之戰，無比輝煌！（ •̀ ω •́ )✧ 歸還現實……Zzz")


if __name__ == "__main__":
    main()