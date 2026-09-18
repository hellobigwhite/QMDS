import random
import re
import threading
import time
from typing import Optional

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("proxy_manager")

# 账号级故障特征：代理商在 CONNECT 建隧道阶段直接返回 402/407，
# 说明账号欠费/额度耗尽或认证失效，此时池内所有出口 IP 会同时失效。
# 这类故障逐个 mark_bad 只会白烧整个池子，应当一次性熔断整池。
_ACCOUNT_LEVEL_PHRASES = (
    "payment required",                # 402
    "proxy authentication required",   # 407
)
_ACCOUNT_LEVEL_RE = re.compile(r"tunnel connection failed:\s*(?:402|407)\b", re.IGNORECASE)


def is_account_level_failure(error: object) -> bool:
    """判断代理异常是否属于账号级故障（402 欠费 / 407 认证失败）"""
    text = str(error or "")
    if not text:
        return False
    low = text.lower()
    if any(phrase in low for phrase in _ACCOUNT_LEVEL_PHRASES):
        return True
    return bool(_ACCOUNT_LEVEL_RE.search(text))


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

    # 账号级故障（402/407）时整池熔断的默认时长（秒）
    ACCOUNT_DOWN_COOLDOWN = 900.0

    def __init__(self, proxies: Optional[list[str]] = None, account_cooldown: Optional[float] = None):
        self._proxies: list[Proxy] = [Proxy(p) for p in proxies] if proxies else []
        self._lock = threading.Lock()
        self._account_cooldown = account_cooldown or self.ACCOUNT_DOWN_COOLDOWN
        self._pool_down_until = 0.0

    @classmethod
    def from_settings(cls) -> "ProxyManager":
        proxies = settings.load_proxies()
        return cls(proxies)

    def add_proxy(self, url: str):
        with self._lock:
            self._proxies.append(Proxy(url))

    def get_proxy(self) -> Optional[dict]:
        with self._lock:
            # 账号级故障熔断期内不再分配任何代理，调用方据此降级直连
            if time.time() < self._pool_down_until:
                return None
            available = [p for p in self._proxies if p.is_available]
            if not available:
                return None
            proxy = random.choice(available)
            return {"http": proxy.url, "https": proxy.url}

    def mark_bad(self, proxy_dict: Optional[dict], cooldown: float = 60.0):
        if not proxy_dict:
            return
        url = proxy_dict.get("http") or proxy_dict.get("https")
        with self._lock:
            for p in self._proxies:
                if p.url == url:
                    p.mark_bad(cooldown)
                    break

    def mark_bad_long(self, proxy_dict: Optional[dict]):
        self.mark_bad(proxy_dict, cooldown=300.0)

    @property
    def is_pool_down(self) -> bool:
        """整池是否处于账号级故障熔断中"""
        with self._lock:
            return time.time() < self._pool_down_until

    def disable_all(self, cooldown: Optional[float] = None, reason: str = "") -> bool:
        """整池熔断（账号级故障：402 欠费 / 407 认证失败）

        账号级故障下池内所有出口 IP 会同时失效，逐个 mark_bad 需要烧穿整个池子
        才能轮到直连降级；这里一次性停用，get_proxy() 在熔断期内直接返回 None。
        返回 True 表示本次调用真正触发了熔断。
        """
        with self._lock:
            now = time.time()
            if now < self._pool_down_until:
                return False
            seconds = self._account_cooldown if cooldown is None else cooldown
            self._pool_down_until = now + seconds
        log.warning(
            f"代理池账号级故障，整池熔断 {seconds:.0f} 秒（期间直接降级直连）"
            + (f" | {reason}" if reason else "")
        )
        return True

    def reset_pool(self):
        """清除整池熔断（换号/续费后调用）"""
        with self._lock:
            self._pool_down_until = 0.0

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._proxies if p.is_available)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._proxies)
