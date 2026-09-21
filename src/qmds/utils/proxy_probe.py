"""本地代理池可用性探测

在启用本地代理池（LOCAL_PROXY_POOL_ENABLED=1）时，先对 proxies.txt 里的
每个代理发起一次轻量探测请求，确认其真实可用后再交给各调用方使用；
若全部不可用则返回空列表，调用方（Google 搜索 / 平台检测 / HttpClient 等）
自动关闭本地代理，改走各自的降级链（远程代理服务 → 直连 / cloudscraper）。

探测结果带进程级缓存（默认 300 秒），避免每次 load_proxies() 都阻塞等待。
"""

import random
import threading
import time
from typing import Optional

import requests
import urllib3

from qmds.utils.logger import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = get_logger("proxy_probe")

# 与 scripts/check_proxies.py 一致的项目实际抓取目标（轻量 Shopify JSON 端点）
DEFAULT_PROBE_TARGET = "https://www.allbirds.com/products.json?limit=1"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
]

# ── 进程级缓存 ──────────────────────────────────────────────
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"ts": 0.0, "ok": []}  # ts=探测时间戳, ok=可用代理列表

# ── 进程级永久禁用（检测到本地代理不可用后直接禁止，不进入冷却）──
# 一旦触发，本次进程内所有链路都不再使用本地代理（load_proxies 返回空、
# HttpClient/爬虫直接走直连），重启进程后才可能恢复。
_BAN_LOCK = threading.Lock()
_BAN: dict = {"banned": False, "reason": ""}


def ban_local_pool(reason: str = ""):
    """永久禁用本地代理池（进程级，不设冷却，不自动恢复）"""
    with _BAN_LOCK:
        if _BAN["banned"]:
            return
        _BAN["banned"] = True
        _BAN["reason"] = reason
    log.warning(
        "本地代理池检测到不可用，已直接禁止（本次进程内不再使用本地代理）"
        + (f": {reason}" if reason else "")
    )


def is_local_pool_banned() -> bool:
    """本地代理池是否已被禁止（进程级）"""
    with _BAN_LOCK:
        return bool(_BAN["banned"])


def reset_pool_ban():
    """解除禁止（仅测试/手动恢复用）"""
    with _BAN_LOCK:
        _BAN["banned"] = False
        _BAN["reason"] = ""


def test_proxy(proxy_url: str, target: str = DEFAULT_PROBE_TARGET, timeout: int = 10) -> tuple[bool, str]:
    """测试单个代理是否可用（能返回 HTTP 200 即视为可用）"""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            target, headers=headers, proxies=proxies,
            timeout=timeout, verify=False, allow_redirects=True,
        )
        if resp.status_code == 200:
            return True, f"OK (HTTP 200)"
        return False, f"HTTP {resp.status_code}"
    except requests.exceptions.ProxyError as e:
        return False, f"ProxyError: {e}"
    except requests.exceptions.SSLError as e:
        return False, f"SSLError: {e}"
    except requests.exceptions.ConnectTimeout:
        return False, "ConnectTimeout"
    except requests.exceptions.ReadTimeout:
        return False, "ReadTimeout"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except requests.exceptions.ConnectionError as e:
        return False, f"ConnectionError: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe_proxies(
    proxies: list[str],
    target: str = DEFAULT_PROBE_TARGET,
    workers: int = 20,
    timeout: int = 10,
) -> list[str]:
    """并发探测所有代理，返回可用代理列表（顺序与输入一致）"""
    if not proxies:
        return []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(test_proxy, p, target, timeout): p for p in proxies}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                ok, _ = fut.result()
            except Exception:
                ok = False
            results[p] = ok

    ok_list = [p for p in proxies if results.get(p)]
    bad_count = len(proxies) - len(ok_list)
    if ok_list:
        log.info(
            f"本地代理池探测完成: 可用 {len(ok_list)}/{len(proxies)}，"
            f"不可用 {bad_count}（目标 {target}）"
        )
    else:
        # 全部不可用：直接禁止，不进入冷却（本次进程内不再尝试）
        ban_local_pool(f"探测 {len(proxies)} 个代理全部不可用（目标 {target}）")
    return ok_list


def cached_probe(
    proxies: list[str],
    target: str = DEFAULT_PROBE_TARGET,
    ttl: float = 300.0,
    workers: int = 20,
    timeout: int = 10,
) -> list[str]:
    """带缓存的探测：ttl 秒内复用上次结果，避免反复阻塞请求"""
    if not proxies:
        return []
    if is_local_pool_banned():
        # 已被禁止：不再重复探测，直接返回空
        return []
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE["ts"] and now - _CACHE["ts"] < ttl:
            return list(_CACHE["ok"])
    ok = probe_proxies(proxies, target=target, workers=workers, timeout=timeout)
    with _CACHE_LOCK:
        _CACHE["ts"] = time.time()
        _CACHE["ok"] = ok
    return list(ok)


def clear_cache():
    """清空探测缓存（Web 界面手动复检时可用）"""
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["ok"] = []
