"""统一搜索提供者管理 — 支持 BestProxy / SearchAPI / ScraperAPI / Crawlbase 多 key 轮换"""

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
]


# ── Key 池 ────────────────────────────────────────────────

class KeyPool:
    """单个 provider 的 key 轮换池"""

    def __init__(self, name: str, keys: list[str]):
        self.name = name
        self._keys = keys
        self._exhausted: set[str] = set()
        self._index = 0

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

    def reset(self):
        self._exhausted.clear()
        self._index = 0


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


# ── 异常 ──────────────────────────────────────────────────

class ScrapeProviderError(Exception):
    pass


# ── 提供者工厂 ────────────────────────────────────────────

PROVIDER_CLASSES = {
    "scraperapi": ScraperAPIProvider,
    "searchapi": SearchAPIProvider,
    "crawlbase": CrawlbaseProvider,
    "bestproxy": BestProxyProvider,
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
            pool = KeyPool(cfg.name, keys)
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
        """获取所有 provider 状态"""
        result = []
        for p in self._providers:
            result.append({
                "name": p.name,
                "available_keys": p.key_pool.available_count,
                "total_keys": p.key_pool.total_count,
                "enabled": p.is_available(),
            })
        return result
