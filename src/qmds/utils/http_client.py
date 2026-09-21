import random
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from qmds.config import settings
from qmds.core.exceptions import ProxyError, RateLimitError
from qmds.utils.proxy_manager import ProxyManager, is_account_level_failure
from qmds.utils.proxy_probe import ban_local_pool, is_local_pool_banned

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HttpClient:
    """统一的 HTTP 客户端，支持代理、重试、限流处理"""

    BROWSER_HEADERS = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ]

    def __init__(self, proxy_manager: Optional[ProxyManager] = None):
        self.proxy_manager = proxy_manager
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=1,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=60)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def get_headers(self) -> dict:
        return random.choice(self.BROWSER_HEADERS).copy()

    def request(
        self,
        url: str,
        method: str = "GET",
        *,
        timeout=None,
        verify: bool = True,
        **kwargs,
    ) -> requests.Response:
        headers = self.get_headers()
        headers.update(kwargs.pop("headers", {}))
        # 本地代理池已被禁止（进程级，检测到不可用后直接禁止）：即使持有
        # proxy_manager 也直接走直连，不再取代理、不再重试
        if self.proxy_manager and is_local_pool_banned():
            proxy = None
        else:
            proxy = self.proxy_manager.get_proxy() if self.proxy_manager else None
        effective_timeout = timeout or settings.request_timeout

        try:
            resp = self._session.request(
                method=method,
                url=url,
                headers=headers,
                timeout=effective_timeout,
                proxies=proxy,
                verify=verify,
                **kwargs,
            )
            if resp.status_code == 429:
                if proxy and self.proxy_manager:
                    self.proxy_manager.mark_bad(proxy)
                # 429 说明本地代理对目标不可用：直接禁止整池，不进入冷却
                if proxy:
                    ban_local_pool(f"HTTP 429 Too Many Requests: {url}")
                raise RateLimitError(f"429 Too Many Requests: {url}")
            resp.raise_for_status()
            return resp
        except requests.exceptions.ProxyError as e:
            if self.proxy_manager:
                if is_account_level_failure(e):
                    # 402 欠费 / 407 认证失败是账号级故障，池内所有 IP 同时失效
                    ban_local_pool(f"账号级故障: {str(e)[:120]}")
                elif proxy:
                    self.proxy_manager.mark_bad(proxy)
            raise ProxyError(f"Proxy failed: {e}") from e
        except requests.exceptions.RequestException as e:
            msg = str(e)
            # urllib3 重试耗尽（如 "too many 429 error responses"）说明本地代理
            # 对目标不可用：直接禁止整池，不进入冷却
            if proxy and "429" in msg:
                ban_local_pool(f"429 重试耗尽: {msg[:120]}")
            raise ProxyError(f"Request failed: {e}") from e

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request(url, "GET", **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request(url, "POST", **kwargs)

    def close(self):
        self._session.close()
