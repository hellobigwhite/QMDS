import logging
import os
import random
import threading
import time
import argparse
import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd
import redis
import requests


REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_QUEUE_TTL = 1800

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
            _redis_client.ping()
        except Exception:
            _redis_client = False
            return None
    return _redis_client if _redis_client is not False else None


def _flush_redis_queue(redis_conn, queue_key, mongo_collection):
    if not redis_conn:
        return 0
    items = []
    while True:
        data = redis_conn.lpop(queue_key)
        if data is None:
            break
        try:
            items.append(json.loads(data))
        except Exception:
            continue
    if not items:
        return 0
    results = [
        (it["url"], it["platform"], it["product_count"], it["domain"], it["ts"])
        for it in items
    ]
    save_to_mongodb(results, mongo_collection)
    return len(results)


LOGGER = logging.getLogger("data_scraper")
if not LOGGER.handlers:
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler = logging.FileHandler("scraper.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "Data")
os.makedirs(DATA_DIR, exist_ok=True)

MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "shopify_url"

SEARCHAPI_BASE_URL = "https://www.searchapi.io/api/v1/search"
SCRAPERAPI_SEARCH_URL = "https://api.scraperapi.com/structured/google/search"
SCRAPERAPI_FETCH_URL = "http://api.scraperapi.com/"
BESTPROXY_BASE_URL = "https://scraper.bestproxy.com/v1/query"
CRAWLBASE_API_URL = "https://api.crawlbase.com/"
XCRAWL_SERP_URL = "https://run.xcrawl.com/v1/serp"

BESTPROXY_TOKEN_FILE = os.path.join(PROJECT_DIR, "bestproxy_tokens.txt")


class BestProxyPool:
    def __init__(self, tokens_file=None):
        self.tokens_file = tokens_file or BESTPROXY_TOKEN_FILE
        self.tokens = []
        self.index = 0
        self.lock = threading.Lock()
        self.reload()

    def reload(self, tokens_file=None):
        if tokens_file:
            self.tokens_file = tokens_file
        self.tokens = self._load_tokens()
        self.index = 0

    def _load_tokens(self):
        tokens = []
        if os.path.exists(self.tokens_file):
            with open(self.tokens_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # sanitize: remove characters that can't be encoded as latin-1 (HTTP headers)
                        tokens.append(line.encode("latin-1", errors="ignore").decode("latin-1"))
        return tokens

    @property
    def count(self):
        return len(self.tokens)

    def execute(self, func, *args, **kwargs):
        with self.lock:
            start_index = self.index
            for i in range(len(self.tokens)):
                token = self.tokens[self.index]
                self.index = (self.index + 1) % len(self.tokens)
                try:
                    return func(token, *args, **kwargs)
                except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as exc:
                    LOGGER.warning(f"BestProxy token [{self.index}] 失败: {exc}")
                    continue
        raise RuntimeError("所有 BestProxy token 均已失败")


_bestproxy_pool = None


def get_bestproxy_pool(tokens_file=None):
    global _bestproxy_pool
    if _bestproxy_pool is None:
        _bestproxy_pool = BestProxyPool(tokens_file)
    else:
        _bestproxy_pool.reload(tokens_file)
    return _bestproxy_pool


class ScraperStopRequested(Exception):
    pass


def _log(message, progress_callback=None):
    LOGGER.info(message)
    if progress_callback:
        progress_callback(message)


def _raise_if_stop_requested(stop_callback=None):
    if stop_callback and stop_callback():
        raise ScraperStopRequested("Stop requested")


def get_mongo_client():
    from pymongo import MongoClient

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB_NAME]
    return client, db


def save_to_mongodb(results, collection_name):
    if not results:
        return 0

    client, db = get_mongo_client()
    collection = db[collection_name]
    inserted_count = 0

    for url, platform, product_count, domain, timestamp in results:
        doc = {
            "URL": url,
            "E-commerce Platform": platform,
            "Product Count": product_count,
            "Domain": domain,
            "Category": collection_name.replace("_Unfiltered_URLs", ""),
            "Timestamp": timestamp,
        }
        result = collection.update_one({"Domain": doc["Domain"]}, {"$set": doc}, upsert=True)
        if result.upserted_id or result.modified_count > 0:
            inserted_count += 1

    client.close()
    return inserted_count


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)


def get_browser_headers():
    return {
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


def clean_url_to_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            return None, None

        if ".myshopify.com" in domain:
            main_domain = domain.replace(".myshopify.com", ".com")
            myshopify_url = urlunparse(("https", domain, "", "", "", ""))
        else:
            main_domain = domain
            myshopify_url = None

        if main_domain.startswith("www."):
            main_domain = main_domain[4:]

        cleaned = urlunparse(("https", main_domain, "", "", "", ""))
        return cleaned, myshopify_url
    except Exception:
        return None, None


def get_domain(url):
    try:
        if not url:
            return None
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith("www."):
            domain = domain[4:]
        if domain.endswith(".myshopify.com"):
            domain = domain.replace(".myshopify.com", ".com")
        return domain
    except Exception:
        return None


def filter_urls(urls, category_file=None, mongo_collection=None):
    existing_domains = set()

    if category_file and os.path.exists(category_file):
        try:
            df = pd.read_excel(category_file)
            if "Domain" in df.columns:
                existing_domains = set(df["Domain"].dropna().astype(str).str.lower())
        except Exception as exc:
            LOGGER.error("Failed reading %s: %s", category_file, exc)

    if mongo_collection:
        try:
            client, db = get_mongo_client()
            cursor = db[mongo_collection].find({}, {"Domain": 1})
            for doc in cursor:
                if doc.get("Domain"):
                    existing_domains.add(str(doc["Domain"]).lower())
            client.close()
        except Exception as exc:
            LOGGER.error("Failed reading MongoDB domains: %s", exc)

    cleaned_urls = set()
    seen_domains = set()
    url_map = {}

    for url in urls:
        cleaned, myshopify_url = clean_url_to_domain(url)
        domain = get_domain(cleaned)
        if not cleaned or not domain:
            continue
        if "translate.google.com" in cleaned:
            continue
        if domain.lower() in existing_domains or domain.lower() in seen_domains:
            continue
        cleaned_urls.add(cleaned)
        seen_domains.add(domain.lower())
        if myshopify_url:
            url_map[cleaned] = myshopify_url

    return list(cleaned_urls), url_map


class ResponseAdapter:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = int(status_code)
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}: {self.text[:200]}")


def _load_json_maybe(value):
    if isinstance(value, (dict, list)):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def crawlbase_request(url, api_key, scraper=None, timeout=120):
    if not api_key:
        raise ValueError("Crawlbase token is required")

    params = {
        "token": api_key,
        "url": url,
        "format": "json",
    }
    if scraper:
        params["scraper"] = scraper

    response = requests.get(CRAWLBASE_API_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()

    body = payload.get("body", payload)
    json_body = _load_json_maybe(body)
    if isinstance(body, str):
        text_body = body
    else:
        text_body = json.dumps(body, ensure_ascii=False)

    original_status = payload.get("original_status") or payload.get("pc_status") or response.headers.get("original_status") or response.headers.get("pc_status") or response.status_code
    try:
        original_status = int(str(original_status))
    except Exception:
        original_status = response.status_code

    return ResponseAdapter(
        status_code=original_status,
        text=text_body,
        json_data=json_body,
        headers=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
    )


def request_with_mode(url, api_mode="1", api_key=None, bestproxy_auth=None, method="GET", headers=None, timeout=30, **kwargs):
    headers = headers or {}

    if api_mode == "2":
        params = {"api_key": api_key, "url": url, "keep_headers": "true"}
        if method.upper() == "GET":
            return requests.get(SCRAPERAPI_FETCH_URL, params=params, headers=headers, timeout=timeout, **kwargs)
        return requests.post(SCRAPERAPI_FETCH_URL, params=params, headers=headers, timeout=timeout, **kwargs)

    if api_mode == "4":
        if method.upper() != "GET":
            raise ValueError("Crawlbase only supports GET in this tool")
        return crawlbase_request(url, api_key, timeout=max(timeout, 90))

    if method.upper() == "GET":
        return requests.get(url, headers=headers, timeout=timeout, **kwargs)
    return requests.post(url, headers=headers, timeout=timeout, **kwargs)


def _request_with_retry(url, api_mode="1", api_key=None, bestproxy_auth=None, headers=None, timeout=15, max_retries=2):
    for attempt in range(max_retries):
        try:
            response = request_with_mode(
                url,
                api_mode=api_mode,
                api_key=api_key,
                bestproxy_auth=bestproxy_auth,
                method="GET",
                headers=headers,
                timeout=timeout,
            )
            if response.status_code != 0:
                return response
        except requests.exceptions.SSLError:
            try:
                response = request_with_mode(
                    url,
                    api_mode=api_mode,
                    api_key=api_key,
                    bestproxy_auth=bestproxy_auth,
                    method="GET",
                    headers=headers,
                    timeout=timeout,
                    verify=False,
                )
                if response.status_code != 0:
                    return response
            except Exception:
                pass
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
    return None


def detect_ecommerce_platform(url, url_map, api_mode="1", api_key=None, bestproxy_auth=None, timeout=15):
    try:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        if not url.endswith("/"):
            url += "/"

        headers = get_browser_headers()

        checks = [
            ("Shopify", f"{url}meta.json", lambda r: "published_products_count" in r.json()),
            ("WooCommerce", f"{url}wp-json/wc/v3/products?per_page=1", lambda r: isinstance(r.json(), list)),
            ("Magento", f"{url}magento_version", lambda r: "Magento" in r.text),
            ("Magento", f"{url}static/version", lambda r: r.status_code == 200),
            ("BigCommerce", url, lambda r: "BigCommerce" in r.text),
        ]

        for platform, check_url, predicate in checks:
            try:
                response = _request_with_retry(
                    check_url,
                    api_mode=api_mode,
                    api_key=api_key,
                    bestproxy_auth=bestproxy_auth,
                    headers=headers,
                    timeout=timeout,
                )
                if response and response.status_code == 200 and predicate(response):
                    return platform, url
            except Exception:
                pass

        try:
            response = _request_with_retry(
                url,
                api_mode=api_mode,
                api_key=api_key,
                bestproxy_auth=bestproxy_auth,
                headers=headers,
                timeout=timeout,
            )
            if response and response.status_code == 200:
                html_content = response.text.lower()
                indicators = [
                    "js.stripe.com",
                    "stripe.js",
                    "paypal.com/sdk",
                    "paypalobjects.com",
                    "klarna.com",
                    "squareup.com",
                    "shopify_payments",
                    "afterpay.com",
                    '<meta name="generator" content="prestashop">',
                    "opencart",
                ]
                if any(indicator in html_content for indicator in indicators):
                    return "Generic E-commerce", url
        except Exception:
            pass

        myshopify_url = url_map.get(url.rstrip("/"))
        if myshopify_url:
            if not myshopify_url.endswith("/"):
                myshopify_url += "/"
            try:
                response = _request_with_retry(
                    f"{myshopify_url}meta.json",
                    api_mode=api_mode,
                    api_key=api_key,
                    bestproxy_auth=bestproxy_auth,
                    headers=headers,
                    timeout=timeout,
                )
                if response and response.status_code == 200 and "published_products_count" in response.json():
                    return "Shopify", myshopify_url
            except Exception:
                pass
        return None, None
    except Exception:
        return None, None


def get_shopify_product_count(url, url_map, api_mode="1", api_key=None, bestproxy_auth=None, timeout=12):
    used_url = url
    if not used_url.startswith(("http://", "https://")):
        used_url = f"https://{used_url}"
    if not used_url.endswith("/"):
        used_url += "/"

    headers = get_browser_headers()

    for candidate in [used_url, url_map.get(url.rstrip("/"))]:
        if not candidate:
            continue
        if not candidate.endswith("/"):
            candidate += "/"
        request_url = f"{candidate}meta.json"
        try:
            response = _request_with_retry(
                request_url,
                api_mode=api_mode,
                api_key=api_key,
                bestproxy_auth=bestproxy_auth,
                headers=headers,
                timeout=timeout,
            )
            if response:
                response.raise_for_status()
                meta_json = response.json()
                return int(meta_json.get("published_products_count", 0)), candidate
        except Exception as exc:
            LOGGER.error("Product count request failed for %s: %s", request_url, exc)

    return 0, used_url


def export_to_xlsx(results, category_file):
    if not results:
        return

    df = pd.DataFrame(results, columns=["URL", "E-commerce Platform", "Product Count", "Domain", "Timestamp"])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")

    if os.path.exists(category_file):
        try:
            df_old = pd.read_excel(category_file)
            if "Timestamp" in df_old.columns:
                df_old["Timestamp"] = pd.to_datetime(df_old["Timestamp"], errors="coerce")
            df = pd.concat([df_old, df], ignore_index=True)
        except Exception as exc:
            LOGGER.error("Failed merging old Excel data: %s", exc)

    df["Standardized_Domain"] = df["Domain"].astype(str).str.lower()
    df = df.drop_duplicates(subset=["Standardized_Domain"], keep="last")
    df = df.drop(columns=["Standardized_Domain"])
    df = df.sort_values(by="Timestamp", na_position="last")
    df.to_excel(category_file, index=False, engine="openpyxl")


def google_search_searchapi(query, api_key, page=0, max_results_per_page=10):
    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "page": page + 1,
        "num": max_results_per_page,
    }
    response = requests.get(SEARCHAPI_BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    return [item.get("link") for item in data.get("organic_results", []) if item.get("link", "").startswith("http")]


def google_search_scraperapi(query, api_key, page=0, max_results_per_page=10):
    params = {
        "api_key": api_key,
        "query": query,
        "start": page * max_results_per_page,
        "tld": "com",
        "country_code": "us",
    }
    response = requests.get(SCRAPERAPI_SEARCH_URL, params=params, timeout=120)
    response.raise_for_status()
    data = response.json()
    return [item.get("link") for item in data.get("organic_results", []) if item.get("link", "").startswith("http")]


def google_search_bestproxy_with_token(token, query, page=0, max_results_per_page=10):
    headers = {
        "Authorization": token.encode("latin-1", errors="ignore").decode("latin-1"),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "close",
    }
    payload = {
        "source": "google_search_web",
        "geo": "US",
        "locale": "en-US",
        "context": {
            "keywords_list": [{"keyword": query}],
            "start_page": page + 1,
            "end_page": page + 1,
        },
    }
    response = requests.post(BESTPROXY_BASE_URL, headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    data = response.json()
    page_urls = []
    for item in data.get("result", []):
        for content in item.get("contents", []):
            link = content.get("link")
            if str(link).startswith("http"):
                page_urls.append(link)
    return page_urls


def google_search_bestproxy(query, page=0, max_results_per_page=10, bestproxy_auth=None):
    if bestproxy_auth:
        return google_search_bestproxy_with_token(bestproxy_auth, query, page, max_results_per_page)
    pool = get_bestproxy_pool()
    if pool.count:
        return pool.execute(google_search_bestproxy_with_token, query, page=page, max_results_per_page=max_results_per_page)
    raise RuntimeError("No BestProxy tokens available (bestproxy_tokens.txt 为空)")


def google_search_crawlbase(query, api_key, page=0, max_results_per_page=10):
    params = {
        "q": query,
        "start": page * max_results_per_page,
        "num": max_results_per_page,
        "hl": "en",
        "gl": "us",
    }
    google_url = f"https://www.google.com/search?{urlencode(params)}"
    response = crawlbase_request(google_url, api_key, scraper="google-serp", timeout=120)
    data = response.json() or {}
    if isinstance(data, dict) and isinstance(data.get("body"), dict):
        data = data.get("body") or {}

    candidates = []
    if isinstance(data, dict):
        for key in ("searchResults", "organic_results", "results"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(value)
    elif isinstance(data, list):
        candidates = data

    page_urls = []
    seen = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        link = item.get("url") or item.get("link")
        link = str(link or "").strip()
        if not link.startswith("http"):
            continue
        if link in seen:
            continue
        seen.add(link)
        page_urls.append(link)
    return page_urls


def google_search_xcrawl(query, api_key, page=0, max_results_per_page=10):
    page_data = google_search_xcrawl_page(
        query,
        api_key,
        start=page * max_results_per_page,
        max_results_per_page=max_results_per_page,
    )
    return page_data["urls"]


def _extract_xcrawl_next_start(next_url):
    try:
        parsed = urlparse(str(next_url or "").strip())
        values = parse_qs(parsed.query or "").get("start") or []
        if not values:
            return None
        return int(values[0])
    except Exception:
        return None


def google_search_xcrawl_page(query, api_key, start=0, max_results_per_page=10):
    if not api_key:
        raise ValueError("XCrawl API key is required")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "engine": "google_search",
        "q": query,
        "start": start,
        "location": "US",
        "hl": "en",
    }
    response = requests.post(XCRAWL_SERP_URL, headers=headers, json=payload, timeout=90)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        snippet = (response.text or "")[:500]
        raise requests.HTTPError(f"{exc} | XCrawl body: {snippet}") from exc
    data = response.json()

    organic_results = data.get("organic_results", []) if isinstance(data, dict) else []
    page_urls = []
    seen = set()
    for item in organic_results:
        if not isinstance(item, dict):
            continue
        link = item.get("url") or item.get("link")
        link = str(link or "").strip()
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        page_urls.append(link)

    pagination = data.get("pagination") if isinstance(data, dict) else None
    has_next = None
    next_start = None
    if isinstance(pagination, dict):
        next_url = pagination.get("next")
        if isinstance(next_url, str) and next_url.strip():
            has_next = True
            next_start = _extract_xcrawl_next_start(next_url)
        else:
            has_next = False

    if has_next is None and len(organic_results) < max_results_per_page:
        has_next = False

    return {
        "urls": page_urls,
        "raw_result_count": len(organic_results),
        "has_next": has_next,
        "next_start": next_start,
    }


def fetch_google_results(api_mode, query, api_key, page, page_size, bestproxy_auth=None):
    if api_mode == "1":
        return google_search_searchapi(query, api_key, page=page, max_results_per_page=page_size)
    if api_mode == "2":
        return google_search_scraperapi(query, api_key, page=page, max_results_per_page=page_size)
    if api_mode == "3":
        return google_search_bestproxy(query, page=page, max_results_per_page=page_size, bestproxy_auth=bestproxy_auth)
    if api_mode == "4":
        return google_search_crawlbase(query, api_key, page=page, max_results_per_page=page_size)
    if api_mode == "5":
        return google_search_xcrawl(query, api_key, page=page, max_results_per_page=page_size)
    return []


def run_scraper_job(api_mode, query, api_key, page, page_size, bestproxy_auth=None):
    if api_mode == "1":
        return google_search_searchapi(query, api_key, page=page, max_results_per_page=page_size)
    if api_mode == "2":
        return google_search_scraperapi(query, api_key, page=page, max_results_per_page=page_size)
    if api_mode == "3":
        return google_search_bestproxy(query, page=page, max_results_per_page=page_size, bestproxy_auth=bestproxy_auth)
    return []


def run_scraper_job(
    keywords,
    max_results=100,
    min_product_count=200,
    api_mode="1",
    api_key=None,
    bestproxy_auth=None,
    save_mode="excel",
    category="default",
    mongo_collection=None,
    progress_callback=None,
    stop_callback=None,
):
    current_results = []
    keywords = [item.strip() for item in keywords if str(item).strip()]
    category = (category or "default").strip() or "default"
    redis_conn = None
    queue_key = None

    category_dir = os.path.join(DATA_DIR, category)
    os.makedirs(category_dir, exist_ok=True)
    category_file = os.path.join(category_dir, f"{category}.xlsx")

    if save_mode == "mongo":
        mongo_collection = (mongo_collection or f"{category}_Unfiltered_URLs").strip()
        redis_conn = _get_redis()
        queue_key = f"scraper:queue:{mongo_collection}"
        if redis_conn:
            redis_conn.expire(queue_key, REDIS_QUEUE_TTL)

    def save_single_result(result_tuple):
        _raise_if_stop_requested(stop_callback)
        current_results.append(result_tuple)
        if save_mode == "excel":
            export_to_xlsx([result_tuple], category_file)
        elif redis_conn:
            data = json.dumps({
                "url": result_tuple[0],
                "platform": result_tuple[1],
                "product_count": result_tuple[2],
                "domain": result_tuple[3],
                "ts": result_tuple[4],
            })
            redis_conn.rpush(queue_key, data)
            redis_conn.expire(queue_key, REDIS_QUEUE_TTL)
        else:
            save_to_mongodb([result_tuple], mongo_collection)
        _log(f"Saved result -> {result_tuple[0]} ({result_tuple[1]})", progress_callback)

    def process_page_urls(urls, url_map):
        results = []
        ecommerce_urls = []

        executor = ThreadPoolExecutor(max_workers=10)
        stop_now = False
        try:
            future_to_url = {
                executor.submit(
                    detect_ecommerce_platform,
                    url,
                    url_map,
                    api_mode,
                    api_key,
                    bestproxy_auth,
                ): url
                for url in urls
            }
            while future_to_url:
                try:
                    _raise_if_stop_requested(stop_callback)
                except ScraperStopRequested:
                    stop_now = True
                    raise
                done, _ = wait(list(future_to_url.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    url = future_to_url.pop(future, None)
                    if not url:
                        continue
                    try:
                        platform, used_url = future.result()
                        if platform and used_url:
                            ecommerce_urls.append((url, used_url, platform))
                            _log(f"{url} -> {platform}", progress_callback)
                        else:
                            _log(f"{url} filtered out", progress_callback)
                    except Exception as exc:
                        _log(f"Platform detection failed for {url}: {exc}", progress_callback)
        finally:
            executor.shutdown(wait=not stop_now, cancel_futures=stop_now)

        executor = ThreadPoolExecutor(max_workers=10)
        stop_now = False
        try:
            future_to_item = {}
            for url, used_url, platform in ecommerce_urls:
                _raise_if_stop_requested(stop_callback)
                if platform.lower() == "shopify":
                    future_to_item[
                        executor.submit(
                            get_shopify_product_count,
                            url,
                            url_map,
                            api_mode,
                            api_key,
                            bestproxy_auth,
                        )
                    ] = (url, used_url, platform)
                else:
                    domain = get_domain(url)
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    result = (used_url, platform, None, domain, timestamp)
                    results.append(result)
                    save_single_result(result)

            while future_to_item:
                try:
                    _raise_if_stop_requested(stop_callback)
                except ScraperStopRequested:
                    stop_now = True
                    raise
                done, _ = wait(list(future_to_item.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    item = future_to_item.pop(future, None)
                    if not item:
                        continue
                    url, used_url, platform = item
                    try:
                        product_count, final_url = future.result()
                        if product_count >= min_product_count:
                            domain = get_domain(url)
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            result = (final_url, platform, product_count, domain, timestamp)
                            results.append(result)
                            save_single_result(result)
                        else:
                            _log(f"{used_url} product count {product_count} < {min_product_count}", progress_callback)
                    except Exception as exc:
                        _log(f"Product count failed for {used_url}: {exc}", progress_callback)
        finally:
            executor.shutdown(wait=not stop_now, cancel_futures=stop_now)

        return results

    def perform_search(query):
        page_size = 10
        current_page = 0
        current_start = 0
        page_error_retries = 0
        consecutive_small_pages = 0
        consecutive_empty_cleaned_pages = 0
        all_results = []
        seen_page_signatures = set()

        while len(current_results) < max_results:
            _raise_if_stop_requested(stop_callback)
            if current_page > 0:
                time.sleep(random.uniform(1.8, 3.8))
                _raise_if_stop_requested(stop_callback)

            try:
                if api_mode == "5":
                    page_data = google_search_xcrawl_page(
                        query,
                        api_key,
                        start=current_start,
                        max_results_per_page=page_size,
                    )
                    page_urls = page_data["urls"]
                    raw_result_count = page_data["raw_result_count"]
                    has_next = page_data["has_next"]
                    next_start = page_data["next_start"]
                else:
                    page_urls = fetch_google_results(
                        api_mode,
                        query,
                        api_key,
                        current_page,
                        page_size,
                        bestproxy_auth=bestproxy_auth,
                    )
                    raw_result_count = len(page_urls)
                    has_next = None
                    next_start = None
            except Exception as exc:
                _log(f"Search request failed on page {current_page + 1}: {exc}", progress_callback)
                page_error_retries += 1
                if page_error_retries >= 3:
                    _log(
                        f"Page {current_page + 1} failed {page_error_retries} times, stopping query",
                        progress_callback,
                    )
                    break
                wait_seconds = min(3 * page_error_retries, 10)
                _log(
                    f"Retrying page {current_page + 1} after {wait_seconds}s "
                    f"(attempt {page_error_retries + 1}/3)",
                    progress_callback,
                )
                time.sleep(wait_seconds)
                continue

            page_error_retries = 0

            if not page_urls:
                _log(f"No results on page {current_page + 1}, stopping query", progress_callback)
                break

            page_signature = tuple(page_urls[:5])
            if page_signature and page_signature in seen_page_signatures:
                _log(f"Detected repeated results on page {current_page + 1}, stopping query", progress_callback)
                break
            if page_signature:
                seen_page_signatures.add(page_signature)

            if raw_result_count <= 3:
                consecutive_small_pages += 1
                if consecutive_small_pages >= 2:
                    _log("Two consecutive low-result pages, stopping query", progress_callback)
                    break
            else:
                consecutive_small_pages = 0

            cleaned_urls, url_map = filter_urls(
                page_urls,
                category_file=category_file if save_mode == "excel" else None,
                mongo_collection=mongo_collection if save_mode == "mongo" else None,
            )
            _log(f"Page {current_page + 1} cleaned URLs: {len(cleaned_urls)}", progress_callback)

            if not cleaned_urls:
                consecutive_empty_cleaned_pages += 1
                if consecutive_empty_cleaned_pages >= 2:
                    _log("Two consecutive empty cleaned pages, stopping query", progress_callback)
                    break
            else:
                consecutive_empty_cleaned_pages = 0

            page_results = process_page_urls(cleaned_urls, url_map)
            all_results.extend(page_results)
            _log(f"Query page {current_page + 1} valid results: {len(page_results)} | total saved: {len(current_results)}", progress_callback)

            if len(current_results) >= max_results:
                break
            if has_next is False:
                _log(f"XCrawl reported no next page after page {current_page + 1}, stopping query", progress_callback)
                break

            if api_mode == "5":
                if next_start is None:
                    next_start = current_start + max(raw_result_count, page_size)
                if next_start <= current_start:
                    _log(f"XCrawl returned an invalid next start after page {current_page + 1}, stopping query", progress_callback)
                    break
                current_start = next_start
            current_page += 1

        return all_results

    all_results = []
    try:
        for keyword in keywords:
            _raise_if_stop_requested(stop_callback)
            _log(f"Start keyword: {keyword}", progress_callback)
            variants = [
                f"{keyword} inurl:collections/all",
                f"{keyword} inurl:collections/all - page 123",
                f"{keyword} inurl:collections/all - page 88",
            ]
            if api_mode in {"3", "4", "5"}:
                variants = [
                    f"{keyword} inurl:collections/all",
                    f"{keyword} inurl:collections/all - page 321",
                    f"{keyword} inurl:collections/all - page 123",
                ]

            for query in variants:
                _raise_if_stop_requested(stop_callback)
                if len(current_results) >= max_results:
                    break
                all_results.extend(perform_search(query))

            if len(current_results) >= max_results:
                break
    except ScraperStopRequested:
        _log("收到停止请求，正在结束数据爬取任务", progress_callback)
        final_results = current_results[:max_results]
        if save_mode == "excel":
            export_to_xlsx(final_results, category_file)
            saved_to = category_file
        else:
            flushed = _flush_redis_queue(redis_conn, queue_key, mongo_collection)
            _log(f"Flushed {flushed} results from Redis to MongoDB", progress_callback)
            saved_to = mongo_collection
        return {
            "results": final_results,
            "count": len(final_results),
            "save_mode": save_mode,
            "saved_to": saved_to,
            "category": category,
            "stopped": True,
        }

    final_results = current_results[:max_results]
    if save_mode == "excel":
        export_to_xlsx(final_results, category_file)
        saved_to = category_file
    else:
        flushed = _flush_redis_queue(redis_conn, queue_key, mongo_collection)
        _log(f"Flushed {flushed} results from Redis to MongoDB", progress_callback)
        saved_to = mongo_collection

    return {
        "results": final_results,
        "count": len(final_results),
        "save_mode": save_mode,
        "saved_to": saved_to,
        "category": category,
        "stopped": False,
    }


def main():
    parser = argparse.ArgumentParser(description="Run Shopify/e-commerce URL scraping job")
    parser.add_argument("--keywords", required=True, help="Comma or newline separated keywords")
    parser.add_argument("--api-mode", default="1", choices=["1", "2", "3", "4", "5"])
    parser.add_argument("--api-key", default="")
    parser.add_argument("--bestproxy-auth", default="")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--min-product-count", type=int, default=200)
    parser.add_argument("--save-mode", default="excel", choices=["excel", "mongo"])
    parser.add_argument("--category", default="default")
    parser.add_argument("--mongo-collection", default="")
    args = parser.parse_args()

    keywords = [part.strip() for part in args.keywords.replace(";", "\n").replace(",", "\n").splitlines() if part.strip()]
    result = run_scraper_job(
        keywords=keywords,
        max_results=args.max_results,
        min_product_count=args.min_product_count,
        api_mode=args.api_mode,
        api_key=args.api_key or None,
        bestproxy_auth=args.bestproxy_auth or None,
        save_mode=args.save_mode,
        category=args.category,
        mongo_collection=args.mongo_collection or None,
        progress_callback=lambda message: print(message),
    )
    print(f"done: {result['count']} results, saved_to={result['saved_to']}")


if __name__ == "__main__":
    main()
