"""cloudscraper 共享客户端 — Cloudflare JS 挑战兜底通道

当前系统的主链路是「远程代理服务 → 本地代理池 → 直连」，遇到 Cloudflare
的 JS 挑战（meta.json / products.json 返回 403 挑战页）时只能被判为
inconclusive 后丢弃店铺。本模块提供一个懒加载、线程安全的 cloudscraper
会话，通过执行挑战拿到 clearance cookie，供以下位置复用：

- ProductCrawler.fetch_json / fetch_bytes 的最终降级
- PlatformDetector 的 meta.json 被拦截后的复检
- sitemap 通道的静态 XML 抓取

cloudscraper 为可选依赖：未安装时 is_available() 返回 False，
get() 一律返回 None，调用方按原有降级链继续，既有行为完全不变。
"""

import random
import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from qmds.utils.logger import get_logger

log = get_logger("cloudflare_client")

try:  # pragma: no cover - 依赖存在性由环境决定
    import cloudscraper

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    cloudscraper = None
    _IMPORT_ERROR = str(exc)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
]

# cloudscraper 实例按站点共享：挑战解出的 clearance cookie 会缓存在实例里，
# 复用同一实例可避免每个请求重新解一次挑战。
_lock = threading.Lock()
_scraper = None
_warned = False


def is_available() -> bool:
    """cloudscraper 是否可用（已安装且初始化未失败）"""
    return cloudscraper is not None and _scraper is not False


def get_scraper():
    """获取共享 cloudscraper 会话；不可用时返回 None"""
    global _scraper, _warned
    if cloudscraper is None or _scraper is False:
        if not _warned:
            _warned = True
            log.info(
                "cloudscraper 不可用，Cloudflare 兜底通道关闭"
                + (f"（{_IMPORT_ERROR}）" if _IMPORT_ERROR else "")
            )
        return None
    if _scraper is None:
        with _lock:
            if _scraper is None:
                try:
                    scraper = cloudscraper.create_scraper(
                        browser={"browser": "chrome", "platform": "windows", "mobile": False},
                        delay=10,
                    )
                    # 直连兜底不走本地 Clash（环境代理劫持会把连接池打满）
                    scraper.trust_env = False
                    scraper.headers.update({
                        "User-Agent": random.choice(USER_AGENTS),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Connection": "keep-alive",
                    })
                    # 高并发直连（sitemap 逐商品、平台检测）需要更大的连接池，
                    # 默认 pool_maxsize=10 会频繁弃连重建，拖慢批量请求
                    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
                    scraper.mount("https://", adapter)
                    scraper.mount("http://", adapter)
                    _scraper = scraper
                    log.info("cloudscraper 已启用（Cloudflare 挑战兜底通道）")
                except Exception as exc:  # pragma: no cover - 初始化异常
                    log.warning(f"cloudscraper 初始化失败，已停用: {exc}")
                    _scraper = False
                    return None
    return _scraper


def get(url: str, *, timeout: int = 20, **kwargs) -> Optional[requests.Response]:
    """用 cloudscraper 发起 GET

    与 requests 不同，本函数不抛异常：不可用或请求失败一律返回 None，
    调用方按降级链继续。
    """
    scraper = get_scraper()
    if scraper is None:
        return None
    try:
        return scraper.get(url, timeout=timeout, **kwargs)
    except Exception as exc:
        log.debug(f"cloudscraper 请求失败 {url}: {type(exc).__name__}: {exc}")
        return None
