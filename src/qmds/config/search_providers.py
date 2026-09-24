"""统一搜索提供者管理 — 支持 BestProxy / SearchAPI / ScraperAPI / Crawlbase / Exa 多 key 轮换"""

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("search_providers")


# ── 配置 ──────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    name: str
    keys_file: str          # key 文件名
    base_url: str
    method: str = "GET"     # GET / POST
    timeout: int = 60
    enabled: bool = True


PROVIDER_CONFIGS = [
    ProviderConfig(
        name="scraperapi",
        keys_file="scraperapi_keys.txt",
        base_url="https://api.scraperapi.com/structured/google/search",
        method="GET",
        timeout=60,
    ),
    ProviderConfig(
        name="searchapi",
        keys_file="searchapi_keys.txt",
        base_url="https://www.searchapi.io/api/v1/search",
        method="GET",
        timeout=30,
    ),
    ProviderConfig(
        name="crawlbase",
        keys_file="crawlbase_keys.txt",
        base_url="https://api.crawlbase.com/",
        method="GET",
        timeout=120,
    ),
    ProviderConfig(
        name="bestproxy",
        keys_file="bestproxy_tokens.txt",
        base_url="https://scraper.bestproxy.com/v1/query",
        method="POST",
        timeout=90,
    ),
    ProviderConfig(
        name="exa",
        keys_file="exa_keys.txt",
        base_url="https://api.exa.ai/search",
        method="POST",
        timeout=30,
    ),
    ProviderConfig(
        name="serper",
        keys_file="serper_keys.txt",
        base_url="https://google.serper.dev/search",
        method="POST",
        timeout=30,
    ),
    ProviderConfig(
        name="brightdata",
        keys_file="brightdata_keys.txt",
        base_url="https://api.brightdata.com/request",
        method="POST",
        timeout=90,
    ),
]


# ── 并发策略 ──────────────────────────────────────────────

# 只有一个 key、或平台侧对并发敏感（实测并发易触发 429）的搜索平台：
# 调用方串行执行，并在 provider 内部加锁兜底，保证同一时刻只有一个请求。
# 需要把某个平台也改成单线程时，把它的 name 加进来即可。
SERIAL_SEARCH_PROVIDERS = {"brightdata"}


# ── Key 池 ────────────────────────────────────────────────

class KeyPool:
    """单个 provider 的 key 轮换池"""

    def __init__(self, name: str, keys: list[str], keys_file: Optional[Path] = None):
        self.name = name
        self._keys = keys
        self._exhausted: set[str] = set()
        self._index = 0
        self._keys_file = keys_file

    @property
    def available_count(self) -> int:
        return len([k for k in self._keys if k not in self._exhausted])

    @property
    def total_count(self) -> int:
        return len(self._keys)

    def get_key(self) -> Optional[str]:
        available = [k for k in self._keys if k not in self._exhausted]
        if not available:
            return None
        key = available[self._index % len(available)]
        self._index += 1
        return key

    def mark_exhausted(self, key: str):
        if key not in self._exhausted:
            self._exhausted.add(key)
            masked = key[:8] + "..." if len(key) > 8 else key
            log.warning(f"[{self.name}] key 额度用完: {masked} (剩余: {self.available_count})")
            self._comment_out_key_in_file(key)

    def _comment_out_key_in_file(self, key: str):
        """在文件中注释掉额度用完的key"""
        if not self._keys_file or not self._keys_file.exists():
            return
        try:
            lines = self._keys_file.read_text(encoding="utf-8").splitlines(keepends=True)
            modified = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped == key:
                    lines[i] = f"# {stripped}  # 额度用完\n"
                    modified = True
                    break
            if modified:
                self._keys_file.write_text("".join(lines), encoding="utf-8")
                log.info(f"[{self.name}] 已在文件中注释掉额度用完的key")
        except Exception as e:
            log.error(f"[{self.name}] 注释key失败: {e}")

    def reset(self):
        self._exhausted.clear()
        self._index = 0


class KeyUsageTracker:
    """API key 调用次数记录（线程安全，原子持久化到本地 JSON）

    记录每个 key 实际发出的 API 请求次数，用于监控额度消耗
    （如 serper 免费额度通常 2500 credits/月，可据此判断剩余用量）。
    数据落盘在 {project_root}/serper_key_usage.json，进程重启不丢失。
    """

    def __init__(self, filepath: Optional[Path] = None):
        self._filepath = filepath or (settings.project_root / "serper_key_usage.json")
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self._filepath.exists():
                data = json.loads(self._filepath.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._counts = {
                        str(k): int(v) for k, v in data.items()
                        if str(k) and isinstance(v, (int, float))
                    }
        except Exception as e:
            log.warning(f"加载 key 调用记录失败: {e}")

    def record_call(self, key: str):
        """记录一次 API 调用（线程安全，立即原子持久化，进程崩溃不丢计数）"""
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._save_locked()

    def _save_locked(self):
        try:
            tmp = self._filepath.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._counts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._filepath)
        except Exception as e:
            log.warning(f"保存 key 调用记录失败: {e}")

    def summary(self) -> dict:
        """返回 {key: 累计调用次数}，按次数降序"""
        with self._lock:
            return dict(sorted(self._counts.items(), key=lambda kv: -kv[1]))


# ── 搜索结果 ──────────────────────────────────────────────

@dataclass
class SearchResult:
    urls: list[str]
    provider: str
    key_used: str
    query: str
    page: int


# ── 搜索提供者基类 ────────────────────────────────────────

class SearchProvider(ABC):
    """搜索提供者基类"""

    def __init__(self, config: ProviderConfig, key_pool: KeyPool):
        self.config = config
        self.key_pool = key_pool
        self.name = config.name

    @abstractmethod
    def search(self, query: str, page: int = 1) -> list[str]:
        """执行搜索，返回 URL 列表"""
        ...

    def is_available(self) -> bool:
        return self.key_pool.available_count > 0


# ── ScraperAPI ────────────────────────────────────────────

class ScraperAPIProvider(SearchProvider):
    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        params = {
            "api_key": key,
            "query": query,
            "start": (page - 1) * 10,
            "tld": "com",
            "country_code": "us",
        }
        try:
            resp = requests.get(self.config.base_url, params=params, timeout=self.config.timeout,
                                proxies={"http": None, "https": None})
            if resp.status_code == 403:
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError("403 额度用完")
            if resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            resp.raise_for_status()
            data = resp.json()
            return [item.get("link", "").rstrip("/") for item in data.get("organic_results", [])
                    if item.get("link", "").startswith("http")]
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")


# ── SearchAPI ─────────────────────────────────────────────

class SearchAPIProvider(SearchProvider):
    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        params = {
            "engine": "google",
            "q": query,
            "api_key": key,
            "page": page,
            "num": 10,
        }
        try:
            resp = requests.get(self.config.base_url, params=params, timeout=self.config.timeout,
                                proxies={"http": None, "https": None})
            if resp.status_code == 403:
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError("403 额度用完")
            if resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            resp.raise_for_status()
            data = resp.json()
            return [item.get("link", "").rstrip("/") for item in data.get("organic_results", [])
                    if item.get("link", "").startswith("http")]
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")


# ── Crawlbase ─────────────────────────────────────────────

class CrawlbaseProvider(SearchProvider):
    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        params = {
            "q": query,
            "start": (page - 1) * 10,
            "num": 10,
            "hl": "en",
            "gl": "us",
        }
        google_url = f"https://www.google.com/search?{urlencode(params)}"
        req_params = {"token": key, "url": google_url, "format": "json", "scraper": "google-serp"}
        try:
            resp = requests.get(self.config.base_url, params=req_params, timeout=self.config.timeout,
                                proxies={"http": None, "https": None})
            if resp.status_code == 403:
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError("403 额度用完")
            if resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            resp.raise_for_status()
            payload = resp.json() or {}
            body = payload.get("body", payload)
            if isinstance(body, dict):
                body = body.get("body") or body

            candidates = []
            if isinstance(body, dict):
                for k in ("searchResults", "organic_results", "results"):
                    v = body.get(k)
                    if isinstance(v, list):
                        candidates.extend(v)
            elif isinstance(body, list):
                candidates = body

            urls = []
            seen = set()
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                link = str(item.get("url") or item.get("link") or "").strip()
                if link.startswith("http") and link not in seen:
                    seen.add(link)
                    urls.append(link.rstrip("/"))
            return urls
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")


# ── BestProxy ─────────────────────────────────────────────

class BestProxyProvider(SearchProvider):
    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        headers = {
            "Authorization": key.encode("latin-1", errors="ignore").decode("latin-1"),
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
                "start_page": page,
                "end_page": page,
            },
        }
        try:
            resp = requests.post(self.config.base_url, headers=headers, json=payload, timeout=self.config.timeout,
                                 proxies={"http": None, "https": None})
            if resp.status_code == 403:
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError("403 额度用完")
            if resp.status_code == 500:
                data = resp.json() if resp.text else {}
                if "empty" in data.get("message", "").lower():
                    return []
                raise ScrapeProviderError(f"500 {data.get('message', '')}")
            resp.raise_for_status()
            data = resp.json()
            urls = []
            for item in data.get("result", []):
                for content in item.get("contents", []):
                    link = content.get("link")
                    if str(link).startswith("http"):
                        urls.append(link.rstrip("/"))
            return urls
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")


# ── Exa ───────────────────────────────────────────────────

class ExaProvider(SearchProvider):
    """Exa AI 语义搜索引擎

    与 Google 系 API 不同，Exa 通过自然语言查询发现网站，
    适合搜索特定品类的 Shopify 店铺。

    Exa /search 不支持 offset 分页，但支持 excludeDomains 参数。
    本 provider 利用该参数实现"翻页"：同一查询的后续调用将该查询
    之前已返回的域名加入排除列表，使 Exa 返回不重复的新结果。

    排除列表必须按查询独立维护，绝不能全局共享：曾用单个全局集合
    实现，并发关键词互相污染、集合只增不减，累计约 1500 个域名后
    Exa 对所有请求返回 400 Bad Request，此后每个关键词都搜到 0 个
    URL（2026-09-05 实测：51 次调用累积 1474 个域名后开始全量 400）。
    按查询隔离后，单个列表最多约 numResults × max_pages（30 × 15
    = 450）个域名，稳定在 API 可接受范围内。

    传入的 query 若含 Google 运算符（inurl:/site:）会被自动清理。
    """

    # 单个查询的排除域名上限：30 × 15 页 = 450，500 留余量；
    # 超出时只发送最近的 500 个，防止 excludeDomains 过大被 400 拒绝
    _MAX_EXCLUDE_PER_QUERY = 500
    # 最多保留多少个查询的排除集合，防止长驻进程内存无限增长
    _MAX_QUERY_CACHE = 64

    # Google 搜索运算符清理：Exa 是语义搜索，不识别 inurl: 等语法
    _GOOGLE_OPS = [
        re.compile(r"inurl:\S+"),
        re.compile(r"site:\S+"),
        re.compile(r"-\s*page\s+\d+", re.IGNORECASE),
        re.compile(r'"[^"]*"'),
    ]

    def __init__(self, config: ProviderConfig, key_pool: KeyPool):
        super().__init__(config, key_pool)
        # 按（清洗后的）查询独立累积已返回域名，用于 excludeDomains 去重翻页
        self._seen_by_query: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []

        cleaned = self._clean_query(query)

        # 只取该查询自己的已排除域名快照（线程安全），
        # 不包含其他关键词/其他任务返回过的域名
        with self._lock:
            seen_set = self._seen_by_query.setdefault(cleaned, set())
            exclude_domains = list(seen_set)
        if len(exclude_domains) > self._MAX_EXCLUDE_PER_QUERY:
            exclude_domains = exclude_domains[-self._MAX_EXCLUDE_PER_QUERY:]

        headers = {
            "x-api-key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Exa REST API 使用 camelCase（SDK 的 snake_case 会被自动转换）
        payload: dict = {
            "query": cleaned,
            "numResults": 30,
            "type": "auto",
            "userLocation": "US",
            "contents": {"highlights": True},
        }
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        try:
            resp = self._post_search(headers, payload)
            if resp.status_code in (401, 402, 403):
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError(f"{resp.status_code} key 无效或额度用完")
            if resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            if resp.status_code >= 400:
                # 带上 Exa 返回的错误详情和排除列表大小，便于定位问题
                raise ScrapeProviderError(
                    f"{resp.status_code} 请求被拒（excludeDomains={len(exclude_domains)}）: {resp.text[:200]}"
                )
            data = resp.json()
            urls = []
            new_domains = []
            seen = set()
            for item in data.get("results", []):
                link = str(item.get("url") or "").strip()
                if link.startswith("http") and link not in seen:
                    seen.add(link)
                    urls.append(link.rstrip("/"))
                    # 提取域名用于后续排除
                    domain = self._extract_domain(link)
                    if domain:
                        new_domains.append(domain)

            # 累积本页新域名到该查询自己的排除集合（线程安全），
            # 并淘汰过旧查询，防止长驻进程内存无限增长
            if new_domains:
                with self._lock:
                    self._seen_by_query.setdefault(cleaned, set()).update(new_domains)
                    if len(self._seen_by_query) > self._MAX_QUERY_CACHE:
                        overflow = len(self._seen_by_query) - self._MAX_QUERY_CACHE
                        # dict 保持插入顺序，淘汰最早使用的查询
                        for old_query in list(self._seen_by_query.keys())[:overflow]:
                            if old_query != cleaned:
                                self._seen_by_query.pop(old_query, None)

            return urls
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")

    def _post_search(self, headers: dict, payload: dict) -> "requests.Response":
        """发起搜索请求；连接类错误（并发下偶发连接重置）重试一次"""
        for attempt in range(2):
            try:
                return requests.post(
                    self.config.base_url, headers=headers, json=payload,
                    timeout=self.config.timeout,
                    proxies={"http": None, "https": None},
                )
            except requests.exceptions.ConnectionError:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise

    @classmethod
    def _clean_query(cls, query: str) -> str:
        """清理 Google 搜索运算符，适配 Exa 语义搜索。

        例如 "toy inurl:collections/all - page 123" -> "toy"
        """
        q = query
        for pattern in cls._GOOGLE_OPS:
            q = pattern.sub("", q)
        q = " ".join(q.split())
        return q.strip() or query.strip()

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取规范化域名（去掉 www. 前缀，小写）"""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""


# ── 异常 ──────────────────────────────────────────────────

class ScrapeProviderError(Exception):
    pass


# ── BrightData ────────────────────────────────────────────

class BrightDataProvider(SearchProvider):
    """BrightData SERP API

    通过 BrightData 的 SERP API zone 抓取 Google 搜索结果。
    key 文件每行格式为 `zone:token`（zone 在 BrightData 控制台创建，
    token 为 API token），通过 "Bearer {token}" 鉴权，zone 写入请求体。

    响应结构：外层 JSON 含 status_code/headers/body，body 为 JSON 字符串，
    解析后 organic 数组的每项含 link 字段即搜索结果 URL。
    分页通过 Google 的 &start=N 参数实现（每页 10 条）。
    """

    def _parse_key(self, key: str) -> tuple[str, str]:
        """将 `zone:token` 拆分为 (zone, token)

        兼容只填 token 的旧格式（此时使用默认 zone "serp_api"）。
        """
        if ":" in key:
            zone, token = key.split(":", 1)
            return zone.strip(), token.strip()
        return "serp_api", key.strip()

    # 单线程保证：关键词线程池 / 变体搜索线程池共用这一把锁，
    # 同一时刻只有一个 BrightData 请求在途，避免并发打同一个 key 触发 429。
    _request_lock = threading.Lock()

    def search(self, query: str, page: int = 1) -> list[str]:
        with self._request_lock:
            return self._search_serial(query, page)

    def _search_serial(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        zone, token = self._parse_key(key)

        from urllib.parse import quote
        start = (page - 1) * 10
        google_url = f"https://www.google.com/search?q={quote(query)}"
        if start > 0:
            google_url += f"&start={start}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "zone": zone,
            "url": google_url,
            "format": "json",
            "data_format": "parsed",
        }

        try:
            resp = requests.post(
                self.config.base_url, headers=headers, json=payload,
                timeout=self.config.timeout,
                proxies={"http": None, "https": None},
            )
            # BrightData 错误以 status_code 字段返回（HTTP 仍可能为 200）
            try:
                outer = resp.json()
            except ValueError:
                outer = {}

            inner_status = outer.get("status_code") if isinstance(outer, dict) else None
            brd_error = (outer.get("headers", {}) or {}).get("x-brd-error-code", "") if isinstance(outer, dict) else ""

            if inner_status in (401, 402, 403) or "auth" in str(brd_error).lower():
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError(f"{inner_status} key 无效或额度用完 ({brd_error})")
            if inner_status == 429 or resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            if inner_status in (500, 502, 504) or resp.status_code >= 500:
                raise ScrapeProviderError(f"{inner_status or resp.status_code} 服务端错误 ({brd_error})")

            resp.raise_for_status()

            body = outer.get("body")
            if isinstance(body, str):
                body = json.loads(body)
            if not isinstance(body, dict):
                return []

            urls = []
            seen: set[str] = set()
            for item in body.get("organic", []):
                if not isinstance(item, dict):
                    continue
                link = str(item.get("url") or item.get("link") or "").strip()
                if link.startswith("http") and link not in seen:
                    seen.add(link)
                    urls.append(link.rstrip("/"))
            return urls
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")



# ── Serper ────────────────────────────────────────────────

# serper key 调用次数记录（模块级单例，多个 SerperProvider 实例共享同一计数）
_serper_usage_tracker: Optional[KeyUsageTracker] = None


def _get_serper_usage_tracker() -> KeyUsageTracker:
    global _serper_usage_tracker
    if _serper_usage_tracker is None:
        _serper_usage_tracker = KeyUsageTracker()
    return _serper_usage_tracker


def get_serper_key_usage() -> dict:
    """返回 serper 各 key 的累计调用次数（按次数降序），供监控/展示"""
    return _get_serper_usage_tracker().summary()


class SerperProvider(SearchProvider):
    """Serper Google SERP API

    通过 https://google.serper.dev/search POST 请求获取 Google 搜索结果。
    key 通过 X-API-KEY 请求头鉴权，请求体 {"q": query, "page": page}，
    响应 organic 数组的每项含 link 字段即搜索结果 URL。
    每次发起 API 请求都会记录到 KeyUsageTracker（serper_key_usage.json），
    便于监控各 key 的额度消耗。
    """

    def __init__(self, config: ProviderConfig, key_pool: KeyPool):
        super().__init__(config, key_pool)
        self._usage_tracker = _get_serper_usage_tracker()

    def search(self, query: str, page: int = 1) -> list[str]:
        key = self.key_pool.get_key()
        if not key:
            return []
        self._usage_tracker.record_call(key)  # 记录一次真实 API 调用
        headers = {
            "X-API-KEY": key,
            "Content-Type": "application/json",
        }
        payload = {"q": query, "page": page}
        try:
            resp = requests.post(self.config.base_url, headers=headers, json=payload,
                                 timeout=self.config.timeout,
                                 proxies={"http": None, "https": None})
            if resp.status_code in (401, 402, 403):
                self.key_pool.mark_exhausted(key)
                raise ScrapeProviderError(f"{resp.status_code} key 无效或额度用完")
            if resp.status_code == 429:
                time.sleep(3)
                raise ScrapeProviderError("429 限速")
            if resp.status_code == 400:
                # Serper 额度用完/超限也返回 400，错误信息常见形如
                # {"message":"Not enough credits","statusCode":400} 或含 quota/limit。
                # 识别到"额度/积分不足"即视为 key 耗尽自动注释并轮换；
                # 参数类 400 则正常抛出便于排查。
                body = (resp.text or "")[:200]
                low = body.lower()
                if any(t in low for t in ("credit", "quota", "limit", "exceed", "paid plan", "not enough")):
                    self.key_pool.mark_exhausted(key)
                    raise ScrapeProviderError("400 key 额度用完")
                raise ScrapeProviderError(f"400 请求错误: {body}")
            resp.raise_for_status()
            data = resp.json()
            urls = []
            seen: set[str] = set()
            for item in data.get("organic", []):
                if not isinstance(item, dict):
                    continue
                link = str(item.get("link") or item.get("url") or "").strip()
                if link.startswith("http") and link not in seen:
                    seen.add(link)
                    urls.append(link.rstrip("/"))
            return urls
        except requests.exceptions.RequestException as e:
            raise ScrapeProviderError(f"请求失败: {e}")


# ── 提供者工厂 ────────────────────────────────────────────

PROVIDER_CLASSES = {
    "scraperapi": ScraperAPIProvider,
    "searchapi": SearchAPIProvider,
    "crawlbase": CrawlbaseProvider,
    "bestproxy": BestProxyProvider,
    "exa": ExaProvider,
    "serper": SerperProvider,
    "brightdata": BrightDataProvider,
}


def _load_keys_from_file(filepath: Path) -> list[str]:
    if not filepath.exists():
        return []
    keys = []
    for line in filepath.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keys.append(line)
    return keys


# ── 统一搜索管理器 ────────────────────────────────────────

class SearchManager:
    """统一搜索管理器 — 多 provider + 多 key 自动轮换"""

    def __init__(self):
        self._providers: list[SearchProvider] = []
        self._provider_index = 0
        self._load_all_providers()

    def _load_all_providers(self):
        for cfg in PROVIDER_CONFIGS:
            filepath = settings.project_root / cfg.keys_file
            keys = _load_keys_from_file(filepath)
            if not keys:
                log.debug(f"[{cfg.name}] 无 key，跳过")
                continue
            pool = KeyPool(cfg.name, keys, keys_file=filepath)
            cls = PROVIDER_CLASSES.get(cfg.name)
            if cls:
                provider = cls(cfg, pool)
                self._providers.append(provider)
                log.info(f"[{cfg.name}] 加载 {len(keys)} 个 key")

    @property
    def available_providers(self) -> list[str]:
        return [p.name for p in self._providers if p.is_available()]

    def search(self, query: str, page: int = 1, provider_name: str = "") -> SearchResult:
        """搜索，可指定 provider 或自动切换"""
        if not self._providers:
            raise ScrapeProviderError("没有可用的搜索 API，请在配置文件中添加 key")

        # 指定 provider
        if provider_name:
            for p in self._providers:
                if p.name == provider_name:
                    if not p.is_available():
                        raise ScrapeProviderError(f"[{provider_name}] 无可用 key")
                    key = p.key_pool._keys[0] if p.key_pool._keys else "?"
                    masked = key[:8] + "..." if len(key) > 8 else key
                    urls = p.search(query, page)
                    t_name = threading.current_thread().name
                    log.info(f"[{t_name}] [{p.name}] query={query!r} page={page} found={len(urls)}")
                    return SearchResult(urls=urls, provider=p.name, key_used=masked, query=query, page=page)
            raise ScrapeProviderError(f"未找到 provider: {provider_name}")

        # 自动切换
        tried = 0
        while tried < len(self._providers):
            provider = self._providers[self._provider_index % len(self._providers)]
            self._provider_index += 1
            tried += 1

            if not provider.is_available():
                continue

            try:
                key = provider.key_pool._keys[0] if provider.key_pool._keys else "?"
                masked = key[:8] + "..." if len(key) > 8 else key
                urls = provider.search(query, page)
                t_name = threading.current_thread().name
                log.info(f"[{t_name}] [{provider.name}] query={query!r} page={page} found={len(urls)}")
                return SearchResult(urls=urls, provider=provider.name, key_used=masked, query=query, page=page)
            except ScrapeProviderError as e:
                log.warning(f"[{provider.name}] 失败: {e}")
                continue

        raise ScrapeProviderError("所有搜索 API 均不可用（额度用完或无 key）")

    def get_status(self) -> list[dict]:
        """获取所有 provider 状态（serper 额外附带各 key 累计调用次数）"""
        result = []
        for p in self._providers:
            entry = {
                "name": p.name,
                "available_keys": p.key_pool.available_count,
                "total_keys": p.key_pool.total_count,
                "enabled": p.is_available(),
            }
            if p.name == "serper":
                usage = get_serper_key_usage()
                entry["total_calls"] = sum(usage.values())
                entry["calls_per_key"] = usage
            result.append(entry)
        return result
