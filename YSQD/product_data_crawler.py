import json
import logging
import os
import random
import threading
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import cycle
from queue import Empty, Queue
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import BulkWriteError


MONGO_URI = "mongodb://localhost:27017/"
SRC_DB_NAME = "shopify_url"
DST_DB_NAME = "shopify_data_new"
CLEAN_DB_NAME = "shopify_data_new"

DEFAULT_MAX_WORKERS = 10
DEFAULT_MAX_RETRY_PER_SITE = 4
REQUEST_TIMEOUT = 25

API_RETRY_WAIT = [8, 20, 45, 90]

MAX_EMPTY_PAGES = 3
MAX_PAGE_LIMIT = 100
MAX_SAME_PAGE_LIMIT = 2
MAX_NO_NEW_DATA_PAGES = 2

PAGE_SLEEP_RANGE = (1.5, 3.5)
SITE_COOLDOWN_RANGE = (6, 12)

MIN_PRICE = 1
REUSE_COOLDOWN_DAYS = 30
DEFAULT_REQUEUE_PER_CATEGORY_LIMIT = 200
BLACKLIST_COLLECTION = "crawler_domain_blacklist"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("product_data_crawler")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def batch_id_str() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def to_datetime(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text.rstrip("/")


def build_meta_json_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return f"{parsed.scheme}://{parsed.netloc}/meta.json"


def get_domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "").lower()


def product_unique_key(domain: str, product_id: str, title: str, image: str) -> str:
    normalized_id = str(product_id or "").strip()
    if normalized_id:
        return f"{domain}|||id|||{normalized_id}"
    return f"{domain}|||fallback|||{str(title or '').strip().lower()}|||{str(image or '').strip().lower()}"


def convert_price(value, rate):
    try:
        if value in ("", None):
            return ""
        return round(float(value) * float(rate), 2)
    except Exception:
        return ""


def extract_images(images):
    if not isinstance(images, list):
        return ""
    for item in images:
        if isinstance(item, dict):
            src = str(item.get("src") or "").strip()
            if src:
                return src.split("?")[0]
    return ""


def extract_variant_info(variants, options):
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
    if not isinstance(variants, list) or not variants:
        return "", ""
    first_variant = variants[0]
    return first_variant.get("compare_at_price", ""), first_variant.get("price", "")


def build_language_sample(products, limit=5) -> str:
    chunks = []
    for product in products[:limit]:
        if not isinstance(product, dict):
            continue
        chunks.append(str(product.get("title") or ""))
        chunks.append(str(product.get("body_html") or ""))
    return " ".join(chunks).strip()


NON_ENGLISH_EUROPEAN_CHARS = set(
    "àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞß"
)


def has_european_non_english(text: str) -> bool:
    if not text:
        return False
    return any(ch in NON_ENGLISH_EUROPEAN_CHARS for ch in text)


def has_non_latin_script(text: str) -> bool:
    for ch in str(text or ""):
        code = ord(ch)
        if (
            0x0400 <= code <= 0x04FF
            or 0x0590 <= code <= 0x05FF
            or 0x0600 <= code <= 0x06FF
            or 0x0900 <= code <= 0x097F
            or 0x0E00 <= code <= 0x0E7F
            or 0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
        ):
            return True
    return False


def is_non_english_text(text: str) -> bool:
    sample = str(text or "").strip()
    if not sample:
        return False
    if has_non_latin_script(sample):
        return True
    if has_european_non_english(sample):
        return True

    all_letters = re.findall(r"[^\W\d_]", sample, re.UNICODE)
    latin_letters = re.findall(r"[A-Za-z]", sample)
    if len(all_letters) >= 30:
        latin_ratio = len(latin_letters) / max(len(all_letters), 1)
        if latin_ratio < 0.6:
            return True
    return False


def is_non_english_products(products, limit=5) -> Tuple[bool, str]:
    sample = build_language_sample(products, limit=limit)
    return is_non_english_text(sample), sample


def load_currency_map(currency_config_path: str) -> Dict[str, float]:
    if not os.path.exists(currency_config_path):
        raise FileNotFoundError(f"汇率文件不存在: {currency_config_path}")

    with open(currency_config_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    result = {}
    if isinstance(data, list):
        for item in data:
            key = item.get("nation")
            value = item.get("exchange_rate_usd")
            if key and value is not None:
                result[str(key).upper()] = float(value)
    elif isinstance(data, dict):
        for key, value in data.items():
            result[str(key).upper()] = float(value)

    if not result:
        raise ValueError("汇率配置为空或格式不正确")
    return result


@dataclass
class ProxyEndpoint:
    raw: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "http"

    @property
    def display(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def requests_proxy_dict(self):
        if self.username and self.password:
            auth_url = f"{self.scheme}://{self.username}:{self.password}@{self.host}:{self.port}"
        else:
            auth_url = f"{self.scheme}://{self.host}:{self.port}"
        return {"http": auth_url, "https": auth_url}


class ProxyPool:
    def __init__(self, proxies_file: str):
        self.proxies = self.load_proxies(proxies_file)
        if not self.proxies:
            raise ValueError("没有可用代理，请检查 proxies.txt")
        self.bad_until = {}
        self.index = 0
        self.lock = threading.Lock()
        self.pool = cycle(self.proxies)

    @staticmethod
    def parse_proxy_line(line: str) -> Optional[ProxyEndpoint]:
        text = (line or "").strip()
        if not text:
            return None
        try:
            if text.startswith(("http://", "https://")):
                parsed = urlparse(text)
                if parsed.hostname and parsed.port:
                    return ProxyEndpoint(
                        raw=text,
                        host=parsed.hostname,
                        port=int(parsed.port),
                        username=parsed.username,
                        password=parsed.password,
                        scheme=parsed.scheme or "http",
                    )
                return None

            parts = text.split(":")
            if len(parts) == 2:
                host, port = parts
                return ProxyEndpoint(raw=text, host=host, port=int(port))
            if len(parts) == 4:
                host, port, username, password = parts
                return ProxyEndpoint(raw=text, host=host, port=int(port), username=username, password=password)
            return None
        except Exception:
            return None

    def load_proxies(self, file_path: str) -> List[ProxyEndpoint]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"未找到代理文件: {file_path}")
        endpoints = []
        with open(file_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                proxy = self.parse_proxy_line(raw_line.strip())
                if proxy:
                    endpoints.append(proxy)
        return endpoints

    def get_proxy(self, exclude: Optional[set] = None) -> Optional[ProxyEndpoint]:
        exclude = exclude or set()
        with self.lock:
            now = time.time()
            available = [
                item
                for item in self.proxies
                if self.bad_until.get(item.raw, 0) <= now and item.raw not in exclude
            ]
            if not available:
                fallback = [item for item in self.proxies if item.raw not in exclude]
                if not fallback:
                    return None
                return random.choice(fallback)
            proxy = available[self.index % len(available)]
            self.index += 1
            return proxy

    def mark_bad(self, proxy: Optional[ProxyEndpoint], cooldown=120):
        if proxy is None:
            return
        with self.lock:
            self.bad_until[proxy.raw] = time.time() + cooldown

    def mark_bad_long(self, proxy: Optional[ProxyEndpoint], cooldown=300):
        self.mark_bad(proxy, cooldown=cooldown)


class ProductCrawlerService:
    def __init__(
        self,
        currency_config_path: Optional[str] = None,
        proxies_file: Optional[str] = None,
        task_source_mode: int = 2,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_retry_per_site: int = DEFAULT_MAX_RETRY_PER_SITE,
        min_price: float = MIN_PRICE,
        reuse_requeue_before_crawl: bool = True,
        reuse_per_category_limit: int = DEFAULT_REQUEUE_PER_CATEGORY_LIMIT,
        reuse_max_use_count: int = 0,
        reuse_max_mode2_count: int = 0,
        clear_source_after_crawl: bool = False,
        clear_only_consumed_urls: bool = False,
        skip_already_crawled_source_url: bool = False,
        single_collection: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        self.currency_map = load_currency_map(currency_config_path) if currency_config_path else {}
        self.proxy_pool = ProxyPool(proxies_file) if proxies_file else None
        self.task_source_mode = int(task_source_mode)
        self.max_workers = max(1, int(max_workers))
        self.max_retry_per_site = max(1, int(max_retry_per_site))
        self.min_price = float(min_price)
        self.reuse_requeue_before_crawl = bool(reuse_requeue_before_crawl)
        self.reuse_per_category_limit = max(0, int(reuse_per_category_limit))
        self.reuse_max_use_count = max(0, int(reuse_max_use_count))
        self.reuse_max_mode2_count = max(0, int(reuse_max_mode2_count))
        self.clear_source_after_crawl = bool(clear_source_after_crawl)
        self.clear_only_consumed_urls = bool(clear_only_consumed_urls)
        self.skip_already_crawled_source_url = bool(skip_already_crawled_source_url)
        self.single_collection = str(single_collection).strip() if single_collection else None
        self.progress_callback = progress_callback
        self.stop_callback = None
        self.skip_non_english_check = False
        self.log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler_direct_proxy_pool.log")
        self.logger = setup_logger(self.log_path)
        self.client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=30000,
            maxPoolSize=20,
            minPoolSize=1,
            maxIdleTimeMS=60000,
        )
        self.src_db = self.client[SRC_DB_NAME]
        self.dst_db = self.client[DST_DB_NAME]
        self.clean_db = self.client[CLEAN_DB_NAME]
        self.blacklist_collection = self.src_db[BLACKLIST_COLLECTION]
        self.thread_local = threading.local()
        self.active_sites = set()
        self.site_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.crawl_results = []
        self.blacklist_urls = set()

    def log(self, message: str):
        self.logger.info(message)
        if self.progress_callback:
            self.progress_callback(message)

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    def _db_retry(self, func, max_retries=3):
        last_err = None
        for attempt in range(max_retries):
            try:
                return func()
            except Exception as exc:
                last_err = exc
                if attempt < max_retries - 1:
                    wait_s = (attempt + 1) * 2
                    self.logger.warning("DB op retry %s/%s after %ss: %s", attempt + 1, max_retries, wait_s, str(exc)[:100])
                    time.sleep(wait_s)
        raise last_err

    def _ensure_db_alive(self):
        try:
            self.client.admin.command("ping")
        except Exception:
            self.logger.warning("DB ping failed, reconnecting...")
            try:
                self.client.close()
            except Exception:
                pass
            self.client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=30000,
                maxPoolSize=20,
                minPoolSize=1,
                maxIdleTimeMS=60000,
            )
            self.src_db = self.client[SRC_DB_NAME]
            self.dst_db = self.client[DST_DB_NAME]
            self.clean_db = self.client[CLEAN_DB_NAME]
            self.blacklist_collection = self.src_db[BLACKLIST_COLLECTION]

    def get_session(self):
        if not hasattr(self.thread_local, "session"):
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self.thread_local.session = session
        return self.thread_local.session

    def get_random_headers(self, referer="https://www.google.com/"):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": referer,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def get_source_suffix(self) -> str:
        return "_Filtered_URLs" if self.task_source_mode == 1 else "_Unfiltered_URLs"

    def get_source_mode_name(self) -> str:
        return "Filtered 模式" if self.task_source_mode == 1 else "Unfiltered 模式"

    def load_blacklist(self):
        self.blacklist_urls = {
            normalize_url(doc.get("URL") or "")
            for doc in self.blacklist_collection.find({}, {"URL": 1, "_id": 0})
            if normalize_url(doc.get("URL") or "")
        }

    def is_blacklisted(self, url: str) -> bool:
        return normalize_url(url) in self.blacklist_urls

    def add_to_blacklist(self, url: str, category: str, reason: str):
        normalized_url = normalize_url(url)
        domain = get_domain(normalized_url)
        now = datetime.now()
        self.blacklist_collection.update_one(
            {"URL": normalized_url},
            {
                "$set": {
                    "URL": normalized_url,
                    "Domain": domain,
                    "Category": category,
                    "Reason": reason,
                    "UpdatedAt": now,
                },
                "$setOnInsert": {"CreatedAt": now},
            },
            upsert=True,
        )
        self.blacklist_urls.add(normalized_url)

    def remove_from_source_collections(self, url: str):
        normalized_url = normalize_url(url)
        deleted = 0
        for coll_name in self.src_db.list_collection_names():
            if coll_name.endswith("_Filtered_URLs") or coll_name.endswith("_Unfiltered_URLs"):
                deleted += self.src_db[coll_name].delete_many({"$or": [{"URL": normalized_url}, {"url": normalized_url}]}).deleted_count
        for coll_name in self.dst_db.list_collection_names():
            deleted += self.dst_db[coll_name].delete_many({"source_url": normalized_url}).deleted_count
        return deleted

    def mark_reuse_queue_blacklisted(self, category: str, url: str, reason: str):
        if not category:
            return
        now = datetime.now()
        self.src_db[f"{category}_Reuse_Queue"].update_one(
            {"URL": normalize_url(url)},
            {
                "$set": {
                    "Category": category,
                    "Status": "blacklisted",
                    "LastMatchSource": reason,
                    "LastMatched": False,
                    "LastMatchedTitle": "",
                    "LastMatchedURL": normalize_url(url),
                    "UpdatedAt": now,
                },
                "$setOnInsert": {"CreatedAt": now},
            },
            upsert=True,
        )

    def local_fetch(self, url: str, timeout=REQUEST_TIMEOUT, max_retries=4, allow_direct_fallback=True):
        if self.proxy_pool is None:
            raise RuntimeError("当前服务未初始化代理池，无法执行网络抓取")
        session = self.get_session()
        last_status = 0
        last_proxy_display = "无代理"
        tried = set()

        for attempt in range(max_retries):
            proxy = self.proxy_pool.get_proxy(exclude=tried)
            if proxy is None:
                self.logger.error("无可用代理: %s", url)
                return None, 0, last_proxy_display

            tried.add(proxy.raw)
            last_proxy_display = proxy.display

            try:
                response = session.get(
                    url,
                    headers=self.get_random_headers(),
                    timeout=timeout,
                    allow_redirects=True,
                    proxies=proxy.requests_proxy_dict,
                )
                last_status = response.status_code

                if response.status_code == 200:
                    try:
                        return response.json(), 200, proxy.display
                    except Exception:
                        self.proxy_pool.mark_bad(proxy, cooldown=60)
                elif response.status_code in (403, 429):
                    self.proxy_pool.mark_bad_long(proxy, cooldown=300)
                elif response.status_code >= 500:
                    self.proxy_pool.mark_bad(proxy, cooldown=120)
                else:
                    self.proxy_pool.mark_bad(proxy, cooldown=60)
            except requests.exceptions.ProxyError:
                self.proxy_pool.mark_bad_long(proxy, cooldown=300)
            except requests.exceptions.RequestException:
                self.proxy_pool.mark_bad(proxy, cooldown=120)
            except Exception:
                self.proxy_pool.mark_bad(proxy, cooldown=120)

            wait_seconds = API_RETRY_WAIT[min(attempt, len(API_RETRY_WAIT) - 1)] + random.uniform(0.5, 2.0)
            time.sleep(wait_seconds)

        # 所有代理失败后，尝试直连兜底
        if allow_direct_fallback:
            self.logger.info("所有代理失败，尝试直连: %s", url)
            try:
                response = session.get(
                    url,
                    headers=self.get_random_headers(),
                    timeout=max(timeout, 15),
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    try:
                        return response.json(), 200, "直连"
                    except Exception:
                        pass
                last_status = response.status_code
            except Exception as exc:
                self.logger.warning("直连也失败: %s", exc)

        return None, last_status, last_proxy_display

    def fetch_currency(self, url: str) -> str:
        data, status, _proxy = self.local_fetch(build_meta_json_url(url), timeout=15, max_retries=2)
        if status == 200 and isinstance(data, dict):
            currency = data.get("currency", "USD")
            return str(currency).upper() if currency else "USD"
        return ""

    def get_json(self, url: str, page: int):
        target = f"{normalize_url(url)}/products.json?limit=200&page={page}"
        return self.local_fetch(target, timeout=REQUEST_TIMEOUT, max_retries=4)

    def ensure_indexes(self, dst_collection):
        existing = dst_collection.index_information()
        if "unique_key_1" in existing:
            dst_collection.drop_index("unique_key_1")
            existing = dst_collection.index_information()

        if "unique_key_normal_idx" not in existing:
            dst_collection.create_index([("unique_key", ASCENDING)], name="unique_key_normal_idx", unique=False)
        if "source_url_1" not in existing:
            dst_collection.create_index([("source_url", ASCENDING)])
        if "crawl_time_1" not in existing:
            dst_collection.create_index([("crawl_time", ASCENDING)])

    def already_crawled_source(self, dst_collection, source_url: str, coll_name: str = "") -> bool:
        if dst_collection.find_one({"source_url": source_url}, {"_id": 1}) is not None:
            return True
        if coll_name:
            clean_coll = self.clean_db[coll_name]
            return clean_coll.find_one({"source_url": source_url}, {"_id": 1}) is not None
        return False

    def save_products_realtime(self, dst_collection, products_batch: List[dict]) -> int:
        if not products_batch:
            return 0
        try:
            unique_map = {}
            for product in products_batch:
                unique_key = product.get("unique_key")
                if unique_key:
                    unique_map[unique_key] = product
            deduped_batch = list(unique_map.values())
            if not deduped_batch:
                return 0

            candidate_keys = list(unique_map.keys())
            existing_keys = {
                item["unique_key"]
                for item in dst_collection.find({"unique_key": {"$in": candidate_keys}}, {"unique_key": 1})
            }
            to_insert = [item for item in deduped_batch if item["unique_key"] not in existing_keys]
            if not to_insert:
                return 0

            try:
                self._ensure_db_alive()
                dst_collection.insert_many(to_insert, ordered=False)
                return len(to_insert)
            except BulkWriteError as exc:
                write_errors = exc.details.get("writeErrors", []) if exc.details else []
                return max(len(to_insert) - len(write_errors), 0)
        except Exception as exc:
            self.logger.error("实时入库失败: %s", str(exc)[:300])
            return 0

    def load_tasks(self) -> Tuple[List[Tuple[str, str, str]], List[str]]:
        self.load_blacklist()
        tasks = []
        collection_names = []
        suffix = self.get_source_suffix()

        if self.single_collection:
            coll_names_to_check = [self.single_collection]
        else:
            coll_names_to_check = self.src_db.list_collection_names()

        for coll_name in coll_names_to_check:
            if not coll_name.endswith(suffix):
                continue
            collection_names.append(coll_name)
            base_name = coll_name[: -len(suffix)] or "uncategorized"
            cursor = self.src_db[coll_name].find({}, {"URL": 1, "url": 1, "category2": 1, "category": 1})
            count = 0
            for doc in cursor:
                url = normalize_url(doc.get("URL") or doc.get("url") or "")
                if not url:
                    continue
                if self.is_blacklisted(url):
                    continue
                category2 = str(doc.get("category2") or doc.get("category") or "").strip()
                tasks.append((url, base_name, category2))
                count += 1
            self.log(f"读取任务: {coll_name} | 共 {count} 条")

        return tasks, collection_names
    def save_to_reuse_queue(self, tasks: List[Tuple[str, str, str]]) -> Dict[str, int]:
        grouped = {}
        for url, category1, _category2 in tasks:
            if not category1 or not url:
                continue
            grouped.setdefault(category1, set()).add(normalize_url(url))

        inserted_total = 0
        existing_total = 0
        now = datetime.now()

        for category, urls in grouped.items():
            collection_name = f"{category}_Reuse_Queue"
            collection = self.src_db[collection_name]
            operations = []
            for url in sorted(urls):
                operations.append(
                    UpdateOne(
                        {"URL": url},
                        {
                            "$setOnInsert": {
                                "URL": url,
                                "Category": category,
                                "CreatedAt": now,
                                "FirstUsedAt": None,
                                "LastBatchId": "",
                                "LastMatchSource": "NONE",
                                "LastMatched": False,
                                "LastMatchedTitle": "",
                                "LastMatchedURL": "",
                                "LastMode": "",
                                "LastMode2SelectedAt": None,
                                "LastUsedAt": None,
                                "Mode2SelectCount": 0,
                                "NextReusableAt": None,
                                "Status": "new",
                                "UpdatedAt": now,
                                "UseCount": 0,
                            }
                        },
                        upsert=True,
                    )
                )

            if not operations:
                continue

            self._ensure_db_alive()
            result = collection.bulk_write(operations, ordered=False)
            inserted_total += int(result.upserted_count)
            existing_total += max(len(operations) - int(result.upserted_count), 0)
            self.log(
                f"已写入 Reuse_Queue: {collection_name} | 新增 {result.upserted_count} | 已存在 {len(operations) - result.upserted_count}"
            )

        return {"inserted": inserted_total, "existing": existing_total, "collections": len(grouped)}

    def is_reuse_record_eligible(self, doc: dict, now: datetime) -> bool:
        status = str(doc.get("Status") or "").strip().lower()
        if status in {"disabled", "deleted"}:
            return False

        next_reusable_at = to_datetime(doc.get("NextReusableAt"))
        if next_reusable_at and next_reusable_at > now:
            return False

        if self.reuse_max_use_count > 0 and int(doc.get("UseCount") or 0) >= self.reuse_max_use_count:
            return False

        if self.task_source_mode == 2 and self.reuse_max_mode2_count > 0:
            if int(doc.get("Mode2SelectCount") or 0) >= self.reuse_max_mode2_count:
                return False

        return True

    def requeue_reusable_urls(self) -> Dict[str, int]:
        now = datetime.now()
        current_batch_id = batch_id_str()
        suffix = self.get_source_suffix()
        total_released = 0
        total_existing = 0
        collections = 0

        if self.single_collection:
            single_category = self.single_collection
            for s in ("_Unfiltered_URLs", "_Filtered_URLs"):
                if single_category.endswith(s):
                    single_category = single_category[: -len(s)]
                    break
            coll_names_to_check = [f"{single_category}_Reuse_Queue"]
        else:
            coll_names_to_check = self.src_db.list_collection_names()

        for coll_name in coll_names_to_check:
            if not coll_name.endswith("_Reuse_Queue"):
                continue

            category = coll_name[: -len("_Reuse_Queue")] or "uncategorized"
            reuse_collection = self.src_db[coll_name]
            source_collection = self.src_db[f"{category}{suffix}"]
            released_for_category = 0
            existing_for_category = 0
            collections += 1

            cursor = reuse_collection.find({}, {"_id": 0}).sort("UpdatedAt", ASCENDING)
            for doc in cursor:
                if self.reuse_per_category_limit and released_for_category >= self.reuse_per_category_limit:
                    break

                url = normalize_url(doc.get("URL") or "")
                if not url or not self.is_reuse_record_eligible(doc, now):
                    continue

                result = source_collection.update_one(
                    {"$or": [{"URL": url}, {"url": url}]},
                    {
                        "$setOnInsert": {
                            "URL": url,
                            "url": url,
                            "category1": category,
                            "category2": "",
                            "category": category,
                            "ReuseQueuedAt": now,
                            "ReuseSource": "ReuseQueue",
                            "ReuseBatchId": current_batch_id,
                        }
                    },
                    upsert=True,
                )

                reuse_collection.update_one(
                    {"URL": url},
                    {
                        "$set": {
                            "Category": category,
                            "Status": "queued",
                            "UpdatedAt": now,
                            "LastBatchId": current_batch_id,
                        }
                    },
                )

                if result.upserted_id:
                    released_for_category += 1
                    total_released += 1
                else:
                    existing_for_category += 1
                    total_existing += 1

            self.log(
                f"复用池回投: {coll_name} -> {category}{suffix} | 新投放 {released_for_category} | 已在任务池 {existing_for_category}"
            )

        return {
            "released": total_released,
            "already_in_queue": total_existing,
            "collections": collections,
            "batch_id": current_batch_id,
        }

    def mark_reuse_queue_consumed(self, category: str, url: str, processed: bool):
        if not category or not url:
            return
        collection = self.src_db[f"{category}_Reuse_Queue"]
        now = datetime.now()
        existing = collection.find_one({"URL": url}, {"FirstUsedAt": 1, "Mode2SelectCount": 1}) or {}
        first_used_at = existing.get("FirstUsedAt")
        mode2_count = int(existing.get("Mode2SelectCount") or 0)

        update_doc = {
            "Category": category,
            "LastBatchId": "",
            "LastMatchSource": "CRAWLER",
            "LastMatched": bool(processed),
            "LastMatchedTitle": "",
            "LastMatchedURL": url,
            "LastMode": "mode1" if self.task_source_mode == 1 else "mode2",
            "LastMode2SelectedAt": now if self.task_source_mode == 2 else None,
            "LastUsedAt": now,
            "NextReusableAt": now + timedelta(days=REUSE_COOLDOWN_DAYS),
            "Status": "cooldown",
            "UpdatedAt": now,
        }
        if not first_used_at:
            update_doc["FirstUsedAt"] = now
        if self.task_source_mode == 2:
            update_doc["Mode2SelectCount"] = mode2_count + 1

        collection.update_one(
            {"URL": url},
            {
                "$set": update_doc,
                "$setOnInsert": {"URL": url, "CreatedAt": now},
            },
            upsert=True,
        )
        collection.update_one(
            {"URL": url},
            {"$inc": {"UseCount": 1}},
        )

    def crawl_site(self, task: Tuple[str, str, str]) -> dict:
        url, category1, category2 = task
        url = normalize_url(url)
        domain = get_domain(url)

        if self.should_stop():
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0, "stopped": True}
        if self.is_blacklisted(url):
            self.log(f"[{domain}] 命中黑名单，跳过")
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0, "blacklisted": True}

        with self.site_lock:
            if url in self.active_sites:
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            self.active_sites.add(url)

        try:
            coll_name = category1 if category1 else (category2 or "uncategorized")
            dst_collection = self.dst_db[coll_name]

            if self.skip_already_crawled_source_url and self.already_crawled_source(dst_collection, url, coll_name):
                self.log(f"[{domain}] 跳过已爬来源")
                return {"source_url": url, "source_collection": category1, "success": True, "processed": False, "inserted": 0}

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 开始爬取")

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 获取货币…")
            currency_raw = self.fetch_currency(url)
            if not currency_raw:
                self.log(f"[{domain}] 非 Shopify 站点（meta.json 无响应），跳过")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 非 Shopify 站点，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            currency = currency_raw.upper()
            rate = self.currency_map.get(currency)
            if rate is None:
                self.log(f"[{domain}] 未找到汇率: {currency}")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 无汇率配置，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 汇率 OK: {currency}，开始翻页")

            # 快速探针：检测 /products.json 是否存在
            probe_data, probe_code, _ = self.local_fetch(
                f"{normalize_url(url)}/products.json?limit=200&page=1",
                timeout=15, max_retries=2,
            )
            if probe_code != 200 or not isinstance(probe_data, dict):
                self.log(f"[{domain}] 非 Shopify 站点（products.json 无响应），跳过")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 非 Shopify 站点，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            products_count = len(probe_data.get("products", [])) if isinstance(probe_data.get("products"), list) else 0
            if products_count == 0:
                self.log(f"[{domain}] products.json 返回空列表，跳过")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 无商品，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            if self.progress_callback:
                self.progress_callback(f"[{domain}] products.json 探针通过 ({products_count} 商品)")

            seen_unique_keys = set()
            total_valid = 0
            total_saved = 0
            first_page_non_english_suspected = False

            for _retry in range(self.max_retry_per_site):
                if self.should_stop():
                    self.log(f"[{domain}] 收到停止请求，停止当前站点")
                    return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": total_saved, "stopped": True}
                page = 1
                empty_pages = 0
                same_page_count = 0
                no_new_data_pages = 0
                retry_added_count = 0
                last_page_signature = None

                while empty_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                    if self.should_stop():
                        self.log(f"[{domain}] 收到停止请求，结束翻页")
                        return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": total_saved, "stopped": True}
                    data, code, _proxy = self.get_json(url, page)
                    if code != 200:
                        break

                    products = data.get("products", []) if isinstance(data, dict) else []
                    if not products:
                        if page == 2 and first_page_non_english_suspected:
                            self.log(f"[{domain}] 第1页疑似非英文，但第2页无数据，暂不拉黑，继续后续流程")
                            first_page_non_english_suspected = False
                        empty_pages += 1
                        page += 1
                        time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
                        continue

                    if page == 1:
                        if self.skip_non_english_check:
                            pass
                        else:
                            suspected_non_english, _language_sample = is_non_english_products(products)
                            if suspected_non_english:
                                first_page_non_english_suspected = True
                                self.log(f"[{domain}] 第1页疑似非英文，继续抓取第2页做复核")
                    elif page == 2 and first_page_non_english_suspected:
                        confirmed_non_english, _language_sample = is_non_english_products(products)
                        if confirmed_non_english:
                            deleted_from_sources = self.remove_from_source_collections(url)
                            self.add_to_blacklist(url, category1, "non_english")
                            self.mark_reuse_queue_blacklisted(category1, url, "NON_ENGLISH")
                            self.log(f"[{domain}] 第1页和第2页均判定为非英文，已删除任务池 {deleted_from_sources} 条并加入黑名单")
                            return {
                                "source_url": url,
                                "source_collection": category1,
                                "success": False,
                                "processed": False,
                                "inserted": 0,
                                "blacklisted": True,
                            }
                        self.log(f"[{domain}] 第1页疑似非英文，但第2页复核通过，继续正常爬取")
                        first_page_non_english_suspected = False

                    page_signature = tuple(str(item.get("id", "")) for item in products if isinstance(item, dict))
                    if page_signature and page_signature == last_page_signature:
                        same_page_count += 1
                        if same_page_count >= MAX_SAME_PAGE_LIMIT:
                            break
                    else:
                        same_page_count = 0
                        last_page_signature = page_signature

                    empty_pages = 0
                    page_seen_unique_keys = set()
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
                        if price_value == "" or float(price_value) < self.min_price:
                            continue

                        product_id = str(product.get("id") or "").strip()
                        unique_key = product_unique_key(domain, product_id, title, image)
                        if unique_key in page_seen_unique_keys or unique_key in seen_unique_keys:
                            continue
                        page_seen_unique_keys.add(unique_key)
                        seen_unique_keys.add(unique_key)

                        page_products.append(
                            {
                                "product_id": product_id,
                                "SKU": sku,
                                "标题": title,
                                "描述": desc,
                                "子描述": str(product.get("tags") or "").strip(),
                                "图片": image,
                                "原价": original_price,
                                "折扣价": discount_price,
                                "变体": variant_str,
                                "分类": product_type if product_type else (category1 or category2 or ""),
                                "currency": currency,
                                "category2": category2,
                                "source_url": url,
                                "source_domain": domain,
                                "source_category": category1,
                                "source_subcategory": category2,
                                "crawl_time": now_str(),
                                "unique_key": unique_key,
                            }
                        )

                    total_valid += len(page_products)
                    if page_products:
                        saved_count = self.save_products_realtime(dst_collection, page_products)
                        total_saved += saved_count
                        retry_added_count += saved_count
                        no_new_data_pages = 0
                        if self.progress_callback and page % 5 == 0:
                            self.progress_callback(f"[{domain}] 第{page}页: 新增{saved_count}件(累计{total_saved})")
                    else:
                        no_new_data_pages += 1
                        if no_new_data_pages >= MAX_NO_NEW_DATA_PAGES:
                            break

                    page += 1
                    time.sleep(random.uniform(*PAGE_SLEEP_RANGE))

                if retry_added_count > 0:
                    break

            self.mark_reuse_queue_consumed(category1, url, processed=total_valid > 0)
            self.log(f"[{domain}] 完成 | 有效 {total_valid} | 新增 {total_saved}")
            if self.progress_callback:
                self.progress_callback(f"[{domain}] 爬取完成: 有效{total_valid}件 新增{total_saved}件")
            return {"source_url": url, "source_collection": category1, "success": True, "processed": True, "inserted": total_saved}
        except Exception as exc:
            self.log(f"[{domain}] 异常: {str(exc)[:200]}")
            if self.progress_callback:
                self.progress_callback(f"[{domain}] 异常: {str(exc)[:100]}")
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
        finally:
            time.sleep(random.uniform(*SITE_COOLDOWN_RANGE))
            with self.site_lock:
                self.active_sites.discard(url)

    def crawl_site_with_nav(self, task: Tuple[str, str, str]) -> dict:
        url, category1, category2 = task
        url = normalize_url(url)
        domain = get_domain(url)

        if self.should_stop():
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0, "stopped": True}
        if self.is_blacklisted(url):
            self.log(f"[{domain}] 命中黑名单，跳过")
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0, "blacklisted": True}

        with self.site_lock:
            if url in self.active_sites:
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            self.active_sites.add(url)

        try:
            coll_name = category1 if category1 else (category2 or "uncategorized")
            dst_collection = self.dst_db[coll_name]

            if self.skip_already_crawled_source_url and self.already_crawled_source(dst_collection, url, coll_name):
                self.log(f"[{domain}] 跳过已爬来源")
                return {"source_url": url, "source_collection": category1, "success": True, "processed": False, "inserted": 0}

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 开始导航分类爬取")

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 获取货币…")
            currency_raw = self.fetch_currency(url)
            if not currency_raw:
                self.log(f"[{domain}] 非 Shopify 站点（meta.json 无响应），跳过")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 非 Shopify 站点，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
            currency = currency_raw.upper()
            rate = self.currency_map.get(currency)
            if rate is None:
                self.log(f"[{domain}] 未找到汇率: {currency}")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 无汇率配置，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}

            if self.progress_callback:
                self.progress_callback(f"[{domain}] 汇率 OK: {currency}，开始解析导航分类")

            from shopify_nav_parser import parse_navigation

            nav_items = parse_navigation(url)
            if not nav_items:
                self.log(f"[{domain}] 导航解析为空，无分类可爬")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 导航解析为空，跳过")
                return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}

            total_saved = 0
            total_valid = 0
            seen_unique_keys = set()

            for level1, level2, coll_url, handle in nav_items:
                if not handle:
                    self.log(f"[{domain}] 导航项 {level1}>{level2} 无集合 handle，跳过")
                    continue
                if self.should_stop():
                    self.log(f"[{domain}] 收到停止请求，结束导航爬取")
                    return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": total_saved, "stopped": True}

                self.log(f"[{domain}] 爬取导航分类: {level1} > {level2} (/{handle})")
                if self.progress_callback:
                    self.progress_callback(f"[{domain}] 分类: {level1} > {level2}")

                for _retry in range(self.max_retry_per_site):
                    page = 1
                    empty_pages = 0
                    retry_saved_count = 0

                    while empty_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                        if self.should_stop():
                            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": total_saved, "stopped": True}

                        data, code, _proxy = self.local_fetch(
                            f"{url}/collections/{handle}/products.json?limit=200&page={page}",
                            timeout=REQUEST_TIMEOUT,
                            max_retries=4,
                        )
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
                            if price_value == "" or float(price_value) < self.min_price:
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
                                "分类": level2,
                                "currency": currency,
                                "category2": level2,
                                "source_url": url,
                                "source_domain": domain,
                                "source_category": level1,
                                "source_subcategory": level2,
                                "crawl_time": now_str(),
                                "unique_key": unique_key,
                            })

                        total_valid += len(page_products)
                        if page_products:
                            saved_count = self.save_products_realtime(dst_collection, page_products)
                            total_saved += saved_count
                            retry_saved_count += saved_count
                            if self.progress_callback and page % 5 == 0:
                                self.progress_callback(f"[{domain}] {level1}>{level2} 第{page}页: 新增{saved_count}件(累计{total_saved})")

                        page += 1
                        time.sleep(random.uniform(*PAGE_SLEEP_RANGE))

                    if retry_saved_count > 0:
                        break

            self.mark_reuse_queue_consumed(category1, url, processed=total_valid > 0)
            self.log(f"[{domain}] 导航爬取完成 | 有效 {total_valid} | 新增 {total_saved}")
            if self.progress_callback:
                self.progress_callback(f"[{domain}] 导航爬取完成: 有效{total_valid}件 新增{total_saved}件")
            return {"source_url": url, "source_collection": category1, "success": True, "processed": True, "inserted": total_saved}
        except Exception as exc:
            self.log(f"[{domain}] 导航爬取异常: {str(exc)[:200]}")
            if self.progress_callback:
                self.progress_callback(f"[{domain}] 异常: {str(exc)[:100]}")
            return {"source_url": url, "source_collection": category1, "success": False, "processed": False, "inserted": 0}
        finally:
            time.sleep(random.uniform(*SITE_COOLDOWN_RANGE))
            with self.site_lock:
                self.active_sites.discard(url)

    def worker(self, task_queue: Queue):
        while True:
            if self.should_stop():
                break
            try:
                task = task_queue.get(timeout=2)
            except Empty:
                if self.should_stop():
                    break
                continue

            if task is None:
                task_queue.task_done()
                break

            try:
                result = self.crawl_site(task)
                with self.result_lock:
                    self.crawl_results.append(result)
            finally:
                task_queue.task_done()

    def worker_with_nav(self, task_queue: Queue):
        while True:
            if self.should_stop():
                break
            try:
                task = task_queue.get(timeout=2)
            except Empty:
                if self.should_stop():
                    break
                continue

            if task is None:
                task_queue.task_done()
                break

            try:
                result = self.crawl_site_with_nav(task)
                with self.result_lock:
                    self.crawl_results.append(result)
            finally:
                task_queue.task_done()

    def run_nav(self) -> dict:
        """与 run() 相同，但使用导航分类爬取 (crawl_site_with_nav)"""
        requeue_result = {"released": 0, "already_in_queue": 0, "collections": 0, "batch_id": ""}
        if self.reuse_requeue_before_crawl:
            requeue_result = self.requeue_reusable_urls()

        tasks, coll_names = self.load_tasks()
        if not tasks:
            self.log("没有可执行的导航分类爬取任务")
            return {
                "task_count": 0,
                "success_sites": 0,
                "failed_sites": 0,
                "inserted_products": 0,
                "reuse_queue_inserted": 0,
                "reuse_queue_existing": 0,
                "reuse_released": requeue_result["released"],
                "reuse_already_in_queue": requeue_result["already_in_queue"],
                "source_mode": self.get_source_mode_name(),
                "log_file": self.log_path,
            }

        reuse_result = self.save_to_reuse_queue(tasks)

        target_collections = set()
        for _url, category1, category2 in tasks:
            target_collections.add(category1 if category1 else (category2 or "uncategorized"))
        for coll_name in target_collections:
            self.ensure_indexes(self.dst_db[coll_name])

        random.shuffle(tasks)
        task_queue = Queue()
        for item in tasks:
            task_queue.put(item)

        self.log("=" * 80)
        self.log("Shopify 导航分类商品爬取开始")
        self.log(f"来源模式: {self.get_source_mode_name()} | 任务数: {len(tasks)} | 线程数: {self.max_workers}")
        self.log(f"复用池自动回投 | 集合 {requeue_result['collections']} 个 | 新投放 {requeue_result['released']} | 已在任务池 {requeue_result['already_in_queue']}")
        self.log(f"Reuse_Queue 预存完成 | 集合 {reuse_result['collections']} 个 | 新增 {reuse_result['inserted']} | 已存在 {reuse_result['existing']}")
        self.log("=" * 80)

        threads = []
        for index in range(self.max_workers):
            thread = threading.Thread(target=self.worker_with_nav, args=(task_queue,), name=f"NavCrawler-{index + 1}", daemon=True)
            thread.start()
            threads.append(thread)

        stop_triggered = False
        while True:
            if self.should_stop():
                stop_triggered = True
                self.log("收到任务停止请求，正在停止剩余导航爬取任务")
                while True:
                    try:
                        pending_task = task_queue.get_nowait()
                    except Empty:
                        break
                    if pending_task is None:
                        task_queue.task_done()
                        continue
                    task_queue.task_done()
                break
            if task_queue.unfinished_tasks == 0:
                break
            time.sleep(0.5)

        for _index in range(self.max_workers):
            task_queue.put(None)
        for thread in threads:
            thread.join()

        self.clear_sources(coll_names)

        success_sites = sum(1 for item in self.crawl_results if item.get("success"))
        failed_sites = sum(1 for item in self.crawl_results if not item.get("success"))
        inserted_products = sum(int(item.get("inserted") or 0) for item in self.crawl_results)

        if stop_triggered:
            self.log(f"任务已停止 | 成功站点 {success_sites} | 失败站点 {failed_sites} | 新增商品 {inserted_products}")
        else:
            self.log(f"全部结束 | 成功站点 {success_sites} | 失败站点 {failed_sites} | 新增商品 {inserted_products}")

        return {
            "task_count": len(tasks),
            "success_sites": success_sites,
            "failed_sites": failed_sites,
            "inserted_products": inserted_products,
            "stopped": stop_triggered,
            "reuse_queue_inserted": reuse_result["inserted"],
            "reuse_queue_existing": reuse_result["existing"],
            "reuse_released": requeue_result["released"],
            "reuse_already_in_queue": requeue_result["already_in_queue"],
            "reuse_batch_id": requeue_result["batch_id"],
            "source_mode": self.get_source_mode_name(),
            "log_file": self.log_path,
        }

    def clear_sources(self, coll_names: List[str]):
        suffix = self.get_source_suffix()

        if self.clear_source_after_crawl:
            self.log(f"开始清空所有来源集合: {suffix}")
            for coll_name in coll_names:
                if coll_name.endswith(suffix):
                    result = self.src_db[coll_name].delete_many({})
                    self.log(f"已清空: {coll_name} | 删除 {result.deleted_count} 条")
            return

        if self.clear_only_consumed_urls:
            self.log("开始删除本次成功消费过的来源 URL")
            delete_map = {}
            for item in self.crawl_results:
                if not item.get("success"):
                    continue
                source_url = item.get("source_url")
                source_coll_base = item.get("source_collection")
                if not source_url or not source_coll_base:
                    continue
                src_coll_name = f"{source_coll_base}{suffix}"
                delete_map.setdefault(src_coll_name, set()).add(source_url)

            for src_coll_name, urls in delete_map.items():
                result = self.src_db[src_coll_name].delete_many(
                    {"$or": [{"URL": {"$in": list(urls)}}, {"url": {"$in": list(urls)}}]}
                )
                self.log(f"已删除来源任务: {src_coll_name} | 删除 {result.deleted_count} 条")

    def run(self) -> dict:
        requeue_result = {"released": 0, "already_in_queue": 0, "collections": 0, "batch_id": ""}
        if self.reuse_requeue_before_crawl:
            requeue_result = self.requeue_reusable_urls()

        tasks, coll_names = self.load_tasks()
        if not tasks:
            self.log("没有可执行的商品爬取任务")
            return {
                "task_count": 0,
                "success_sites": 0,
                "failed_sites": 0,
                "inserted_products": 0,
                "reuse_queue_inserted": 0,
                "reuse_queue_existing": 0,
                "reuse_released": requeue_result["released"],
                "reuse_already_in_queue": requeue_result["already_in_queue"],
                "source_mode": self.get_source_mode_name(),
                "log_file": self.log_path,
            }

        reuse_result = self.save_to_reuse_queue(tasks)

        target_collections = set()
        for _url, category1, category2 in tasks:
            target_collections.add(category1 if category1 else (category2 or "uncategorized"))
        for coll_name in target_collections:
            self.ensure_indexes(self.dst_db[coll_name])

        random.shuffle(tasks)
        task_queue = Queue()
        for item in tasks:
            task_queue.put(item)

        self.log("=" * 80)
        self.log("Shopify 商品数据爬取开始")
        self.log(f"来源模式: {self.get_source_mode_name()} | 任务数: {len(tasks)} | 线程数: {self.max_workers}")
        self.log(
            f"复用池自动回投 | 集合 {requeue_result['collections']} 个 | 新投放 {requeue_result['released']} | 已在任务池 {requeue_result['already_in_queue']}"
        )
        self.log(
            f"Reuse_Queue 预存完成 | 集合 {reuse_result['collections']} 个 | 新增 {reuse_result['inserted']} | 已存在 {reuse_result['existing']}"
        )
        self.log("=" * 80)

        threads = []
        for index in range(self.max_workers):
            thread = threading.Thread(target=self.worker, args=(task_queue,), name=f"Crawler-{index + 1}", daemon=True)
            thread.start()
            threads.append(thread)

        stop_triggered = False
        while True:
            if self.should_stop():
                stop_triggered = True
                self.log("收到任务停止请求，正在停止剩余抓取任务")
                while True:
                    try:
                        pending_task = task_queue.get_nowait()
                    except Empty:
                        break
                    if pending_task is None:
                        task_queue.task_done()
                        continue
                    task_queue.task_done()
                break
            if task_queue.unfinished_tasks == 0:
                break
            time.sleep(0.5)

        for _index in range(self.max_workers):
            task_queue.put(None)
        for thread in threads:
            thread.join()

        self.clear_sources(coll_names)

        success_sites = sum(1 for item in self.crawl_results if item.get("success"))
        failed_sites = sum(1 for item in self.crawl_results if not item.get("success"))
        inserted_products = sum(int(item.get("inserted") or 0) for item in self.crawl_results)

        if stop_triggered:
            self.log(f"任务已停止 | 成功站点 {success_sites} | 失败站点 {failed_sites} | 新增商品 {inserted_products}")
        else:
            self.log(f"全部结束 | 成功站点 {success_sites} | 失败站点 {failed_sites} | 新增商品 {inserted_products}")

        return {
            "task_count": len(tasks),
            "success_sites": success_sites,
            "failed_sites": failed_sites,
            "inserted_products": inserted_products,
            "stopped": stop_triggered,
            "reuse_queue_inserted": reuse_result["inserted"],
            "reuse_queue_existing": reuse_result["existing"],
            "reuse_released": requeue_result["released"],
            "reuse_already_in_queue": requeue_result["already_in_queue"],
            "reuse_batch_id": requeue_result["batch_id"],
            "source_mode": self.get_source_mode_name(),
            "log_file": self.log_path,
        }

    def close(self):
        self.client.close()


def run_product_crawler_job(
    currency_config_path: str,
    proxies_file: str,
    task_source_mode: int = 2,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_retry_per_site: int = DEFAULT_MAX_RETRY_PER_SITE,
    min_price: float = MIN_PRICE,
    reuse_requeue_before_crawl: bool = True,
    reuse_per_category_limit: int = DEFAULT_REQUEUE_PER_CATEGORY_LIMIT,
    reuse_max_use_count: int = 0,
    reuse_max_mode2_count: int = 0,
    clear_source_after_crawl: bool = False,
    clear_only_consumed_urls: bool = False,
    skip_already_crawled_source_url: bool = False,
    single_collection: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    stop_callback: Optional[Callable[[], bool]] = None,
) -> dict:
    service = ProductCrawlerService(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=task_source_mode,
        max_workers=max_workers,
        max_retry_per_site=max_retry_per_site,
        min_price=min_price,
        reuse_requeue_before_crawl=reuse_requeue_before_crawl,
        reuse_per_category_limit=reuse_per_category_limit,
        reuse_max_use_count=reuse_max_use_count,
        reuse_max_mode2_count=reuse_max_mode2_count,
        clear_source_after_crawl=clear_source_after_crawl,
        clear_only_consumed_urls=clear_only_consumed_urls,
        skip_already_crawled_source_url=skip_already_crawled_source_url,
        single_collection=single_collection,
        progress_callback=progress_callback,
    )
    service.stop_callback = stop_callback
    try:
        return service.run()
    finally:
        service.close()


def run_reuse_queue_requeue_job(
    task_source_mode: int = 2,
    reuse_per_category_limit: int = DEFAULT_REQUEUE_PER_CATEGORY_LIMIT,
    reuse_max_use_count: int = 0,
    reuse_max_mode2_count: int = 0,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    service = ProductCrawlerService(
        task_source_mode=task_source_mode,
        max_workers=1,
        max_retry_per_site=1,
        reuse_requeue_before_crawl=False,
        reuse_per_category_limit=reuse_per_category_limit,
        reuse_max_use_count=reuse_max_use_count,
        reuse_max_mode2_count=reuse_max_mode2_count,
        progress_callback=progress_callback,
    )
    try:
        return service.requeue_reusable_urls()
    finally:
        service.close()


def run_nav_category_crawler_job(
    currency_config_path: str,
    proxies_file: str,
    task_source_mode: int = 2,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_retry_per_site: int = DEFAULT_MAX_RETRY_PER_SITE,
    min_price: float = MIN_PRICE,
    reuse_requeue_before_crawl: bool = False,
    reuse_per_category_limit: int = DEFAULT_REQUEUE_PER_CATEGORY_LIMIT,
    reuse_max_use_count: int = 0,
    reuse_max_mode2_count: int = 0,
    clear_source_after_crawl: bool = False,
    clear_only_consumed_urls: bool = False,
    skip_already_crawled_source_url: bool = False,
    single_collection: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    stop_callback: Optional[Callable[[], bool]] = None,
) -> dict:
    """导航分类商品爬取入口 - 解析店铺导航栏，按集合逐类爬取"""
    service = ProductCrawlerService(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=task_source_mode,
        max_workers=max_workers,
        max_retry_per_site=max_retry_per_site,
        min_price=min_price,
        reuse_requeue_before_crawl=reuse_requeue_before_crawl,
        reuse_per_category_limit=reuse_per_category_limit,
        reuse_max_use_count=reuse_max_use_count,
        reuse_max_mode2_count=reuse_max_mode2_count,
        clear_source_after_crawl=clear_source_after_crawl,
        clear_only_consumed_urls=clear_only_consumed_urls,
        skip_already_crawled_source_url=skip_already_crawled_source_url,
        single_collection=single_collection,
        progress_callback=progress_callback,
    )
    service.stop_callback = stop_callback
    try:
        return service.run_nav()
    finally:
        service.close()
