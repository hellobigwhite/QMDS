"""
Standalone multi-provider Shopify/e-commerce scraper.

Supports:
1. SearchAPI
2. ScraperAPI
3. BestProxy
4. Crawlbase

Dependencies:
    pip install requests pandas openpyxl

Examples:
    python unified_multi_api_shopify_scraper.py --keywords "pet supplies, fishing gear" --api-mode 4 --api-key YOUR_TOKEN
    python unified_multi_api_shopify_scraper.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

import pandas as pd
import requests

# BestProxy token pool reused from data_scraper.py
from data_scraper import get_bestproxy_pool


SEARCHAPI_BASE_URL = "https://www.searchapi.io/api/v1/search"
SCRAPERAPI_SEARCH_URL = "https://api.scraperapi.com/structured/google/search"
SCRAPERAPI_FETCH_URL = "http://api.scraperapi.com/"
BESTPROXY_BASE_URL = "https://scraper.bestproxy.com/v1/query"
CRAWLBASE_API_URL = "https://api.crawlbase.com/"

DEFAULT_OUTPUT = os.path.abspath("standalone_scraper_results.xlsx")


LOGGER = logging.getLogger("standalone_multi_api_scraper")
if not LOGGER.handlers:
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)


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


def log(message: str):
    LOGGER.info(message)


def load_json_maybe(value):
    if isinstance(value, (dict, list)):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def crawlbase_request(url: str, api_key: str, scraper: Optional[str] = None, timeout: int = 120) -> ResponseAdapter:
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
    json_body = load_json_maybe(body)
    if isinstance(body, str):
        text_body = body
    else:
        text_body = json.dumps(body, ensure_ascii=False)

    original_status = (
        payload.get("original_status")
        or payload.get("pc_status")
        or response.headers.get("original_status")
        or response.headers.get("pc_status")
        or response.status_code
    )
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


USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
]

def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_browser_headers() -> dict:
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


def clean_url_to_domain(url: str) -> Tuple[Optional[str], Optional[str]]:
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


def get_domain(url: str) -> Optional[str]:
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


def filter_urls(urls: Iterable[str], existing_domains: Optional[set] = None):
    existing_domains = existing_domains or set()
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


def request_with_mode(
    url: str,
    api_mode: str = "1",
    api_key: Optional[str] = None,
    bestproxy_auth: Optional[str] = None,
    method: str = "GET",
    headers=None,
    timeout: int = 30,
    **kwargs,
):
    headers = headers or {}

    if api_mode == "2":
        params = {"api_key": api_key, "url": url, "keep_headers": "true"}
        if method.upper() == "GET":
            return requests.get(SCRAPERAPI_FETCH_URL, params=params, headers=headers, timeout=timeout, **kwargs)
        return requests.post(SCRAPERAPI_FETCH_URL, params=params, headers=headers, timeout=timeout, **kwargs)

    if api_mode == "4":
        if method.upper() != "GET":
            raise ValueError("Crawlbase only supports GET in this script")
        return crawlbase_request(url, api_key or "", timeout=max(timeout, 90))

    if method.upper() == "GET":
        return requests.get(url, headers=headers, timeout=timeout, **kwargs)
    return requests.post(url, headers=headers, timeout=timeout, **kwargs)


def _request_with_retry(
    url: str,
    api_mode: str = "1",
    api_key: Optional[str] = None,
    bestproxy_auth: Optional[str] = None,
    headers=None,
    timeout: int = 15,
    max_retries: int = 2,
):
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


def detect_ecommerce_platform(
    url: str,
    url_map: dict,
    api_mode: str = "1",
    api_key: Optional[str] = None,
    bestproxy_auth: Optional[str] = None,
    timeout: int = 15,
):
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


def get_shopify_product_count(
    url: str,
    url_map: dict,
    api_mode: str = "1",
    api_key: Optional[str] = None,
    bestproxy_auth: Optional[str] = None,
    timeout: int = 12,
):
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
            log(f"Product count request failed for {request_url}: {exc}")

    return 0, used_url


def google_search_searchapi(query: str, api_key: str, page=0, max_results_per_page=10):
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


def google_search_scraperapi(query: str, api_key: str, page=0, max_results_per_page=10):
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


def google_search_bestproxy(query: str, page=0, max_results_per_page=10, bestproxy_auth: Optional[str] = None):
    if bestproxy_auth:
        return _bestproxy_request(bestproxy_auth, query, page, max_results_per_page)
    pool = get_bestproxy_pool()
    if pool.count:
        return pool.execute(_bestproxy_request, query, page=page, max_results_per_page=max_results_per_page)
    raise RuntimeError("No BestProxy tokens available (bestproxy_tokens.txt 为空)")


def _bestproxy_request(token: str, query: str, page=0, max_results_per_page=10):
    headers = {"Authorization": token.encode("latin-1", errors="ignore").decode("latin-1"), "Content-Type": "application/json"}
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


def google_search_crawlbase(query: str, api_key: str, page=0, max_results_per_page=10):
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


def fetch_google_results(api_mode: str, query: str, api_key: Optional[str], page: int, page_size: int, bestproxy_auth=None):
    if api_mode == "1":
        return google_search_searchapi(query, api_key or "", page=page, max_results_per_page=page_size)
    if api_mode == "2":
        return google_search_scraperapi(query, api_key or "", page=page, max_results_per_page=page_size)
    if api_mode == "3":
        return google_search_bestproxy(query, page=page, max_results_per_page=page_size, bestproxy_auth=bestproxy_auth)
    if api_mode == "4":
        return google_search_crawlbase(query, api_key or "", page=page, max_results_per_page=page_size)
    return []


def export_to_xlsx(results: List[tuple], output_path: str):
    df = pd.DataFrame(results, columns=["URL", "E-commerce Platform", "Product Count", "Domain", "Timestamp"])
    if df.empty:
        df.to_excel(output_path, index=False, engine="openpyxl")
        return

    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df["DomainKey"] = df["Domain"].astype(str).str.lower()
    df = df.drop_duplicates(subset=["DomainKey"], keep="last").drop(columns=["DomainKey"])
    df = df.sort_values(by="Timestamp", na_position="last")
    df.to_excel(output_path, index=False, engine="openpyxl")


def run_job(
    keywords: List[str],
    api_mode: str,
    api_key: Optional[str],
    bestproxy_auth: Optional[str],
    max_results: int,
    min_product_count: int,
    output_path: str,
    max_workers: int,
):
    current_results: List[tuple] = []

    def save_single_result(result_tuple):
        current_results.append(result_tuple)
        log(f"Saved result -> {result_tuple[0]} ({result_tuple[1]})")

    def process_page_urls(urls, url_map):
        results = []
        ecommerce_urls = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
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
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    platform, used_url = future.result()
                    if platform and used_url:
                        ecommerce_urls.append((url, used_url, platform))
                        log(f"{url} -> {platform}")
                    else:
                        log(f"{url} filtered out")
                except Exception as exc:
                    log(f"Platform detection failed for {url}: {exc}")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {}
            for url, used_url, platform in ecommerce_urls:
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

            for future in as_completed(future_to_item):
                url, used_url, platform = future_to_item[future]
                try:
                    product_count, final_url = future.result()
                    if product_count >= min_product_count:
                        domain = get_domain(url)
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        result = (final_url, platform, product_count, domain, timestamp)
                        results.append(result)
                        save_single_result(result)
                    else:
                        log(f"{used_url} product count {product_count} < {min_product_count}")
                except Exception as exc:
                    log(f"Product count failed for {used_url}: {exc}")

        return results

    def perform_search(query):
        page_size = 10
        current_page = 0
        consecutive_small_pages = 0

        while len(current_results) < max_results:
            if current_page > 0:
                time.sleep(random.uniform(1.8, 3.8))

            try:
                page_urls = fetch_google_results(api_mode, query, api_key, current_page, page_size, bestproxy_auth=bestproxy_auth)
            except Exception as exc:
                log(f"Search request failed on page {current_page + 1}: {exc}")
                break

            if not page_urls:
                log(f"No results on page {current_page + 1}, stopping query")
                break

            if len(page_urls) <= 3:
                consecutive_small_pages += 1
                if consecutive_small_pages >= 2:
                    log("Two consecutive low-result pages, stopping query")
                    break
            else:
                consecutive_small_pages = 0

            existing_domains = {str(item[3]).lower() for item in current_results if item[3]}
            cleaned_urls, url_map = filter_urls(page_urls, existing_domains=existing_domains)
            log(f"Page {current_page + 1} cleaned URLs: {len(cleaned_urls)}")

            page_results = process_page_urls(cleaned_urls, url_map)
            log(f"Query page {current_page + 1} valid results: {len(page_results)} | total saved: {len(current_results)}")

            if len(current_results) >= max_results:
                break
            current_page += 1

    for keyword in keywords:
        log(f"Start keyword: {keyword}")
        variants = [
            f"{keyword} inurl:collections/all",
            f"{keyword} inurl:collections/all - page 123",
            f"{keyword} inurl:collections/all - page 88",
        ]
        if api_mode in {"3", "4"}:
            variants = [
                f"{keyword} inurl:collections/all",
                f"{keyword} inurl:collections/all - page 321",
                f"{keyword} inurl:collections/all - page 123",
            ]

        for query in variants:
            if len(current_results) >= max_results:
                break
            perform_search(query)

        if len(current_results) >= max_results:
            break

    final_results = current_results[:max_results]
    export_to_xlsx(final_results, output_path)
    return final_results


def prompt_if_empty(value: str, label: str, default: str = "") -> str:
    if value:
        return value
    prompt = f"{label}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    entered = input(prompt).strip()
    return entered or default


def normalize_keywords(raw: str) -> List[str]:
    return [part.strip() for part in raw.replace(";", "\n").replace(",", "\n").splitlines() if part.strip()]


def main():
    parser = argparse.ArgumentParser(description="Standalone multi-provider Shopify/e-commerce scraper")
    parser.add_argument("--keywords", default="", help="Comma or newline separated keywords")
    parser.add_argument("--api-mode", default="", choices=["", "1", "2", "3", "4"])
    parser.add_argument("--api-key", default="")
    parser.add_argument("--bestproxy-auth", default="")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--min-product-count", type=int, default=200)
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.keywords:
        print("Available providers:")
        print("1 = SearchAPI")
        print("2 = ScraperAPI")
        print("3 = BestProxy")
        print("4 = Crawlbase")

    keywords_raw = prompt_if_empty(args.keywords, "Enter keywords", "")
    api_mode = prompt_if_empty(args.api_mode, "Choose api mode", "1")
    api_key = args.api_key
    bestproxy_auth = args.bestproxy_auth

    if api_mode in {"1", "2", "4"}:
        api_key = prompt_if_empty(api_key, "Enter API key / token", "")
    if api_mode == "3":
        bestproxy_auth = prompt_if_empty(bestproxy_auth, "Enter BestProxy Authorization", "")

    keywords = normalize_keywords(keywords_raw)
    if not keywords:
        raise RuntimeError("At least one keyword is required")

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)

    results = run_job(
        keywords=keywords,
        api_mode=api_mode,
        api_key=api_key or None,
        bestproxy_auth=bestproxy_auth or None,
        max_results=int(args.max_results),
        min_product_count=int(args.min_product_count),
        output_path=os.path.abspath(args.output),
        max_workers=max(1, int(args.max_workers)),
    )
    print("")
    print(f"Done. Saved {len(results)} results to: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
