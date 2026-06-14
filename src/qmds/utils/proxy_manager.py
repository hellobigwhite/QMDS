import random
import time
from typing import Optional

from qmds.config import settings


class Proxy:
    def __init__(self, url: str):
        self.url = url
        self.bad_until: float = 0
        self.fail_count: int = 0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.bad_until

    def mark_bad(self, cooldown: float = 60.0):
        self.fail_count += 1
        self.bad_until = time.time() + cooldown * min(self.fail_count, 5)

    def reset(self):
        self.bad_until = 0
        self.fail_count = 0


class ProxyManager:
    """代理池管理器"""

    def __init__(self, proxies: Optional[list[str]] = None):
        self._proxies: list[Proxy] = [Proxy(p) for p in proxies] if proxies else []

    @classmethod
    def from_settings(cls) -> "ProxyManager":
        proxies = settings.load_proxies()
        return cls(proxies)

    def add_proxy(self, url: str):
        self._proxies.append(Proxy(url))

    def get_proxy(self) -> Optional[dict]:
        available = [p for p in self._proxies if p.is_available]
        if not available:
            return None
        proxy = random.choice(available)
        return {"http": proxy.url, "https": proxy.url}

    def mark_bad(self, proxy_dict: Optional[dict], cooldown: float = 60.0):
        if not proxy_dict:
            return
        url = proxy_dict.get("http") or proxy_dict.get("https")
        for p in self._proxies:
            if p.url == url:
                p.mark_bad(cooldown)
                break

    def mark_bad_long(self, proxy_dict: Optional[dict]):
        self.mark_bad(proxy_dict, cooldown=300.0)

    @property
    def available_count(self) -> int:
        return sum(1 for p in self._proxies if p.is_available)

    @property
    def total_count(self) -> int:
        return len(self._proxies)
