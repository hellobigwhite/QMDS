"""产品数据爬取模块 - 基于导航的深度爬取"""

import json
import re
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from qmds.config import settings
from qmds.config.categories import normalize_subcategory
from qmds.db.mongodb import MongoDBClient
from qmds.db.product_db import ProductDBClient
from qmds.modules.data_scraper import sitemap_fetcher
from qmds.modules.data_scraper.shopify_nav_parser import parse_navigation
from qmds.utils import cloudflare_client
from qmds.utils.logger import get_logger
from qmds.utils.proxy_manager import ProxyManager, is_account_level_failure
from qmds.utils.proxy_probe import ban_local_pool, is_local_pool_banned

log = get_logger("product_crawler")

# 请求配置
REQUEST_TIMEOUT = 25
# 代理服务专用超时：该服务为浏览器级抓取，实测单请求 1~40 秒（负载高时
# 更长）。给它 25 秒会杀掉一半在途请求，给 90 秒又让线程挂太久——
# 60 秒是"既然决定用它，就给它足够时间完成"的预算（并发名额已在客户端限死）
PROXY_SERVICE_FETCH_TIMEOUT = 60

# 本地代理池开关（settings.local_proxy_pool_enabled，默认关闭）
# 关闭时降级链不再经过本地代理池：
#   fetch_json  -> 代理服务 -> 直连 -> cloudscraper 兜底
#   fetch_bytes -> cloudscraper 直连 -> 代理服务 -> 直连
_LOCAL_PROXY_POOL_ENABLED = settings.local_proxy_pool_enabled
MAX_PAGE_LIMIT = 100
MAX_EMPTY_PAGES = 5
PAGE_SLEEP_RANGE = (1.5, 3.5)
SITE_COOLDOWN_RANGE = (6, 12)

# ── Sitemap 兜底通道配置 ──────────────────────────────────
# products.json 与 meta.json 的商品数上限：达到该值说明分页接口已无法
# 覆盖全店商品（250 条/页 × 100 页），必须改用 sitemap 通道枚举商品。
PRODUCTS_JSON_MAX_PRODUCTS = 25000
# sitemap 通道逐商品取数时的并发数（每个商品一次请求，远高于分页模式）。
# 8 是对单店的安全值：16 并发实测会频繁触发目标站 429（历史日志 4.7 万次
# 按域退避、其中 1 万次顶到上限），退避期间整站线程全部冻结，得不偿失
SITEMAP_FETCH_WORKERS = 8
# 429 限流按域退避（指数退避，秒）：商店对单站并发敏感，命中 429 后暂停
# 该域所有取数请求，等冷却结束再继续，避免全部线程空耗整条降级链
SITEMAP_429_COOLDOWN = 15.0
SITEMAP_429_MAX_COOLDOWN = 180.0
# 同域请求平滑限速：命中 429 后的最小请求间隔（秒，约 2 rps）。
# 8 线程自由并发对单店可达 40 rps 突发，极易触发 429 后整域冻结；
# 预约排队让请求按间隔错峰出发，连续成功后间隔逐步减半放宽
SITEMAP_DOMAIN_PACE = 0.5
# 单站 sitemap 通道时间预算（秒）：超时后放弃剩余商品提前收尾（边爬边写
# 已入库的部分不受影响）。历史日志出现过单站爬 37 小时的僵尸任务，长期
# 占用站点并发名额
SITEMAP_TIME_BUDGET = 90 * 60
# 进程级站点并发上限：多个分类任务同时跑时（每任务最多 10 站），限制
# 全局同时在爬的站点总数，避免把目标站点/出口 IP 打到 429
MAX_CONCURRENT_SITES = 12
# 边爬边写：商品攒满该数量即调用 flush_callback 落库一次，避免整站几万条
# 堆在内存里、爬完才一次性写入导致长时间看不到数据库增长
SAVE_FLUSH_BATCH_SIZE = 500

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def normalize_url(url: str) -> str:
    """标准化URL"""
    text = str(url or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text.rstrip("/")


def get_domain(url: str) -> str:
    """获取域名"""
    return urlparse(url).netloc.replace("www.", "").lower()


def product_unique_key(title: str) -> str:
    """生成商品唯一标识 - 以商品标题为唯一主键（全局去重）"""
    return str(title or "").strip().lower()


def convert_price(value, rate):
    """转换价格"""
    try:
        if value in ("", None):
            return ""
        return round(float(value) * float(rate), 2)
    except Exception:
        return ""


def extract_images(images):
    """提取主图"""
    if not isinstance(images, list):
        return ""
    for item in images:
        if isinstance(item, dict):
            src = str(item.get("src") or "").strip()
            if src:
                return src.split("?")[0]
    return ""


def extract_variant_info(variants, options):
    """提取变体信息"""
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
    """提取价格"""
    if not isinstance(variants, list) or not variants:
        return "", ""
    first_variant = variants[0]
    return first_variant.get("compare_at_price", ""), first_variant.get("price", "")


class ProxyServiceClient:
    """代理服务客户端 - 通过代理服务接口请求目标URL

    地址与 key 来自 settings（环境变量 PROXY_SERVICE_URL / PROXY_SERVICE_KEY 可覆盖）。

    内置熔断：连续失败达到阈值后进入冷却期，期间所有 fetch/fetch_bytes 立即返回
    STATUS_SKIPPED 而不再等待 90 秒超时。sitemap 通道逐商品发起请求，没有熔断
    会让整站耗时不可接受。
    """

    TIMEOUT = 90
    # 熔断中未发起请求的返回码（区别于 HTTP 状态码，调用方据此跳过退避等待）
    STATUS_SKIPPED = -1
    # 并发名额已满未发起请求：调用方应立即走下一级降级，不要等待
    STATUS_BUSY = -2
    # 进程级并发名额（跨实例共享）。实测该服务为浏览器级抓取，仅能并行
    # 消化约 4 个请求（8 并发时延迟 2.6s→32.7s 线性堆叠）；超出的请求
    # 在服务端排队，最终在客户端超时——与其排队不如立刻失败走降级链
    INFLIGHT_LIMIT = 4
    # 等待名额的最长时间（秒）：吸收多任务瞬时突发；真饱和时快速失败
    QUEUE_WAIT = 5.0
    _inflight = threading.BoundedSemaphore(INFLIGHT_LIMIT)

    FAILURE_THRESHOLD = 3
    # 冷却固定 10 秒：服务恢复后很快就能重新走回远程代理，不再指数翻倍
    COOLDOWN = 10.0
    MAX_COOLDOWN = 10.0

    def __init__(self, breaker_enabled: bool = True):
        self.base_url = settings.proxy_service_url
        self.api_key = settings.proxy_service_key
        # 熔断开关：平台检测等场景传 False 关闭熔断（每个请求都真实发起，
        # 不再因连续失败进入冷却跳过）；商品抓取等保留默认开启。
        self._breaker_enabled = breaker_enabled
        # 连接复用：多任务并发下逐请求新建 TCP/TLS（requests.get 裸调用）
        # 会造成握手风暴与端口耗尽，是批量超时的帮凶之一
        self._http = requests.Session()
        _adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self._http.mount("http://", _adapter)
        self._http.mount("https://", _adapter)
        self._success = 0
        self._failure = 0
        # 熔断状态
        self._consecutive_failures = 0
        self._down_until = 0.0
        self._cooldown = self.COOLDOWN
        self._lock = threading.Lock()
        self._last_stats_log = 0.0

    @property
    def available(self) -> bool:
        """熔断冷却是否已结束；熔断关闭时恒为 True"""
        if not self._breaker_enabled:
            return True
        return time.time() >= self._down_until

    @staticmethod
    def _is_service_level_failure(status: int) -> bool:
        """判断是否属于代理服务自身的故障

        0=超时/异常，5xx=服务端故障，403/429=服务限流，这些才计入熔断；
        404/401 等是目标站点的响应，不应拖垮代理服务本身。
        """
        return status == 0 or status >= 500 or status in (403, 429)

    def _record_failure(self, breaker: bool = True):
        with self._lock:
            self._failure += 1
            if not breaker or not self._breaker_enabled:
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.FAILURE_THRESHOLD and time.time() >= self._down_until:
                self._down_until = time.time() + self._cooldown
                log.warning(
                    f"代理服务连续失败 {self._consecutive_failures} 次，熔断 {self._cooldown:.0f} 秒"
                )
                self._cooldown = min(self._cooldown * 2, self.MAX_COOLDOWN)

    def _record_success(self):
        with self._lock:
            self._success += 1
            self._consecutive_failures = 0
            self._cooldown = self.COOLDOWN

    def _get(self, target_url: str, timeout: Optional[int] = None) -> Tuple[Optional[requests.Response], int]:
        """发起一次代理服务请求，返回 (响应, 状态码)

        STATUS_SKIPPED 表示熔断中未发起；STATUS_BUSY 表示并发名额已满
        （短暂等待后仍拿不到名额），两者都未真正发出请求，不计入熔断统计。
        """
        if not self.available:
            return None, self.STATUS_SKIPPED
        if not ProxyServiceClient._inflight.acquire(timeout=self.QUEUE_WAIT):
            log.debug(f"代理服务并发已满（{self.INFLIGHT_LIMIT}），跳过 {target_url}")
            return None, self.STATUS_BUSY
        try:
            effective_timeout = timeout or self.TIMEOUT
            params = {"key": self.api_key, "url": target_url}
            try:
                resp = self._http.get(self.base_url, params=params, timeout=effective_timeout)
                return resp, resp.status_code
            except requests.exceptions.Timeout:
                self._record_failure()
                log.warning(f"代理服务超时 {target_url} | timeout={effective_timeout}s")
                return None, 0
            except Exception as e:
                self._record_failure()
                log.warning(f"代理服务异常 {target_url}: {type(e).__name__}: {e}")
                return None, 0
        finally:
            ProxyServiceClient._inflight.release()

    def fetch(self, target_url: str, timeout: Optional[int] = None) -> Tuple[Optional[dict], int]:
        """通过代理服务请求目标URL（期望 JSON 响应）

        timeout 可覆盖默认 90 秒：平台检测等高频批量场景传较短超时，
        避免单个超时站点拖慢整个批次。
        """
        resp, status = self._get(target_url, timeout=timeout)
        if resp is None:
            return None, status
        if status == 200:
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct.lower():
                try:
                    data = resp.json()
                except ValueError:
                    self._record_failure()
                    log.warning(f"代理服务返回的 JSON 无法解析 {target_url}")
                    return None, status
                self._record_success()
                return data, 200
        self._record_failure(breaker=self._is_service_level_failure(status))
        log.warning(f"代理服务请求失败 {target_url} | HTTP {status}")
        return None, status

    def fetch_bytes(self, target_url: str, timeout: Optional[int] = None) -> Tuple[Optional[bytes], int]:
        """通过代理服务获取原始响应体（sitemap XML 等非 JSON 目标）"""
        resp, status = self._get(target_url, timeout=timeout)
        if resp is None:
            return None, status
        if status == 200 and resp.content:
            self._record_success()
            return resp.content, 200
        self._record_failure(breaker=self._is_service_level_failure(status))
        log.warning(f"代理服务获取原始内容失败 {target_url} | HTTP {status}")
        return None, status

    def log_stats(self, force: bool = False, min_interval: float = 60.0):
        """输出统计信息（按时间节流，避免逐商品请求时刷屏）"""
        now = time.time()
        with self._lock:
            if not force and now - self._last_stats_log < min_interval:
                return
            self._last_stats_log = now
            total = self._success + self._failure
            rate = (self._success / total * 100) if total > 0 else 0
            log.info(f"代理服务统计: 成功={self._success}, 失败={self._failure}, 成功率={rate:.1f}%")


_shared_proxy_service: Optional[ProxyServiceClient] = None
_shared_proxy_service_lock = threading.Lock()


def get_shared_proxy_service() -> ProxyServiceClient:
    """进程内共享的代理服务客户端

    create_crawler() 会为每个站点新建 ProductCrawler，如果 ProxyServiceClient
    也跟着新建，它内部的连续失败熔断就无法跨站点生效——每个站点都要重吃一次
    90 秒超时。这里做成进程级共享实例，让熔断真正起作用。
    """
    global _shared_proxy_service
    if _shared_proxy_service is None:
        with _shared_proxy_service_lock:
            if _shared_proxy_service is None:
                _shared_proxy_service = ProxyServiceClient()
    return _shared_proxy_service


# 进程级站点并发名额：crawl_category 的每个站点在 _crawl_single_site 中
# acquire/release，多个分类任务（各自最多 workers 个站点）共享同一上限
_site_concurrency = threading.BoundedSemaphore(MAX_CONCURRENT_SITES)


class ProductCrawler:
    """产品数据爬取器"""
    
    def __init__(self, currency_map: Dict[str, float], proxy_manager=None, proxy_service=None):
        self.currency_map = currency_map
        self.session = requests.Session()
        # sitemap 通道会并发发起请求，放大连接池避免 "Connection pool is full" 告警
        # （连接池容量与 SITEMAP_FETCH_WORKERS 匹配：每线程可能同时占多个连接）
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
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
        })
        
        # 代理管理（支持标记坏代理 + 冷却轮换）
        self.proxy_manager = proxy_manager
        # 代理服务客户端（优先使用）；默认取进程级共享实例，让熔断跨站点生效
        self.proxy_service = proxy_service or get_shared_proxy_service()

        # 429 限流按域退避状态（sitemap 通道逐商品取数时使用）
        self._domain_throttled_until: Dict[str, float] = {}
        self._domain_429_count: Dict[str, int] = {}
        # 同域平滑限速状态：当前最小间隔 / 下一个可用时间片 / 连续成功计数
        self._domain_pace: Dict[str, float] = {}
        self._domain_next_slot: Dict[str, float] = {}
        self._domain_success_streak: Dict[str, int] = {}
        # 直连被拒（403/401，多为 CF 挑战）的域：后续 direct_first 请求
        # 跳过直连探测直接走代理服务，省掉每页一次的无效直连尝试
        self._direct_blocked: Dict[str, bool] = {}
        self._throttle_lock = threading.Lock()
    
    def close(self):
        """关闭会话释放资源"""
        self.proxy_service.log_stats(force=True)
        if self.session:
            self.session.close()
    
    def get_next_proxy(self) -> Optional[dict]:
        """获取下一个可用代理（ProxyManager 自动跳过冷却中的代理）"""
        if not self.proxy_manager:
            return None
        return self.proxy_manager.get_proxy()

    def mark_proxy_bad(self, proxy_dict: Optional[dict], cooldown: float = 60.0):
        """标记代理为不可用（触发冷却）"""
        if self.proxy_manager and proxy_dict:
            self.proxy_manager.mark_bad(proxy_dict, cooldown=cooldown)

    def _handle_proxy_failure(self, proxy: Optional[dict], error: Exception) -> bool:
        """记录一次代理失败；返回 True 表示本地代理池已被禁止，调用方应停止换代理重试

        402 欠费 / 407 认证失败是账号级故障，池内所有出口 IP 会同时失效，
        逐个 mark_bad 只会白烧整个池子；检测到不可用后直接禁止整池（进程级），
        不进入冷却，本次进程内不再使用本地代理。
        """
        if not self.proxy_manager:
            return False
        if is_account_level_failure(error):
            ban_local_pool(f"账号级故障: {str(error)[:120]}")
            return True
        self.mark_proxy_bad(proxy, cooldown=60.0)
        return is_local_pool_banned()

    # ── 429 限流按域退避（sitemap 通道）───────────────────

    def _throttle_domain(self, domain: str):
        """记录一次 429，按指数退避设置该域冷却期，并收紧平滑限速"""
        with self._throttle_lock:
            count = self._domain_429_count.get(domain, 0) + 1
            self._domain_429_count[domain] = count
            cooldown = min(SITEMAP_429_COOLDOWN * (2 ** (count - 1)), SITEMAP_429_MAX_COOLDOWN)
            self._domain_throttled_until[domain] = time.time() + cooldown
            self._domain_pace[domain] = SITEMAP_DOMAIN_PACE
        log.warning(f"[{domain}] 收到 429，退避 {cooldown:.0f} 秒（第 {count} 次），"
                    f"限速至 {1 / SITEMAP_DOMAIN_PACE:.0f} req/s")

    def _domain_unthrottle(self, domain: str):
        """一次成功取数后清零该域 429 计数；连续成功后逐步放宽平滑限速"""
        with self._throttle_lock:
            self._domain_429_count[domain] = 0
            streak = self._domain_success_streak.get(domain, 0) + 1
            self._domain_success_streak[domain] = streak
            if streak >= 30:
                pace = self._domain_pace.get(domain, 0.0)
                if pace > 0.05:
                    self._domain_pace[domain] = max(pace / 2, 0.05)
                    log.info(f"[{domain}] 连续 {streak} 次成功，"
                             f"限速放宽至 {1 / self._domain_pace[domain]:.0f} req/s")
                self._domain_success_streak[domain] = 0

    def _domain_pace_delay(self, domain: str) -> float:
        """预约一个同域请求时间片，返回需要等待的秒数（平滑限速）

        预约排队：多个线程各自领走间隔递增的时间片，避免全体睡同一时长
        后再次突发。默认无间隔（全速），命中 429 后由 _throttle_domain 收紧。
        """
        with self._throttle_lock:
            pace = self._domain_pace.get(domain, 0.0)
            now = time.time()
            slot = self._domain_next_slot.get(domain, 0.0)
            if slot < now:
                slot = now
            self._domain_next_slot[domain] = slot + max(pace, 0.05)
            return max(0.0, slot - now)

    def _wait_if_throttled(self, domain: str, stop_event: threading.Event = None,
                           abandon_event: threading.Event = None):
        """若该域处于 429 冷却期，等待冷却结束（分片睡眠，停止/放弃信号可打断）"""
        while True:
            with self._throttle_lock:
                until = self._domain_throttled_until.get(domain, 0.0)
            remaining = until - time.time()
            if remaining <= 0:
                return
            if stop_event is not None and stop_event.is_set():
                return
            if abandon_event is not None and abandon_event.is_set():
                return
            time.sleep(min(remaining, 1.0))

    @staticmethod
    def _acquire_site_slot(stop_event: threading.Event = None) -> bool:
        """等待一个站点并发名额（进程级，可被停止信号打断）；失败返回 False"""
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            if _site_concurrency.acquire(timeout=1.0):
                return True

    def fetch_json(self, url: str, timeout: int = REQUEST_TIMEOUT,
                   direct_first: bool = False) -> Tuple[Optional[dict], int]:
        """获取JSON数据

        降级链：代理服务 → 本地代理池(3次) → 直连(仅429/池不可用) → cloudscraper 兜底
        本地代理池关闭（settings.local_proxy_pool_enabled=False）时跳过第2步，
        等价于：代理服务 → 直连 → cloudscraper 兜底。

        Args:
            timeout: 各级降级统一超时；同样透传给代理服务——不传的话
                代理服务默认 90 秒，多任务并发下慢站点会拖垮整批线程
                （历史日志 6.8 万次 90 秒超时的主因）。
            direct_first: 先直连一次（sitemap 逐商品取数用）。静态 .json 端点
                直连可达率很高，省掉远程代理一跳可把单件耗时从秒级压到
                百毫秒级，同时避免多任务把代理服务打爆。直连 404/410/
                200 非 JSON 是源站结论，直接返回不再走代理。
        """
        direct_429 = False
        if direct_first and not self._direct_blocked.get(get_domain(url), False):
            response = cloudflare_client.get(url, timeout=timeout)
            if response is not None:
                dstat = response.status_code
                if dstat == 200:
                    ct = response.headers.get("Content-Type", "")
                    if "json" in ct.lower():
                        try:
                            return response.json(), 200
                        except ValueError:
                            pass  # 解析失败交由代理服务复核
                elif dstat in (404, 410):
                    return None, dstat  # 源站明确无此端点
                elif dstat == 200:
                    return None, 200    # 200 非 JSON：端点被主题禁用
                elif dstat == 429:
                    direct_429 = True   # 目标限流；仍尝试代理，但保留限流信号
                elif dstat in (401, 403):
                    # CF 挑战/拒绝：本域直连通道已废，后续请求直接走代理
                    self._direct_blocked[get_domain(url)] = True
                # 5xx/超时：可能瞬时故障，交由代理服务但不记住

        # 第一步：优先使用代理服务（超时用服务专用预算：它单次要 1~40 秒，
        # 沿用调用方的 15/25 秒会在服务正常工作时也大量超时）
        data, status = self.proxy_service.fetch(url, timeout=PROXY_SERVICE_FETCH_TIMEOUT)
        if status == 200 and data:
            return data, status
        if direct_429 and status != 200:
            return None, 429  # 直连已被目标限流且代理未救回：保留限流信号
        # 仅代理服务自身故障（超时/5xx/限流）才稍作等待，目标站结论（404 等）
        # 无需等待；旧版无条件 sleep(3) 在批量失败时冻结了大量取数线程
        if status != ProxyServiceClient.STATUS_SKIPPED and \
                ProxyServiceClient._is_service_level_failure(status):
            time.sleep(1)

        # 第二步：降级到本地代理池（3次尝试）
        last_status = 0
        cloudflare_blocked = False
        pool_unavailable = False
        for attempt in range(3):
            if not _LOCAL_PROXY_POOL_ENABLED:
                # 本地代理池已关闭：不取代理、不发请求，直接进入第三步直连
                pool_unavailable = True
                break
            if is_local_pool_banned():
                # 检测到本地代理不可用已被禁止（进程级）：不再取代理
                pool_unavailable = True
                break
            proxy = self.get_next_proxy()
            if proxy is None:
                # 代理池整体熔断（账号级故障）或已无可用代理，交给第三步直连
                pool_unavailable = True
                log.warning(f"本地代理池不可用，跳过代理重试 {url}")
                break
            try:
                response = self.session.get(url, timeout=timeout, proxies=proxy)
                status = response.status_code
                ct = response.headers.get("Content-Type", "")

                if status != 200:
                    body = response.text[:150].replace("\n", " ").strip() if response.text else ""
                    if status == 429:
                        self.mark_proxy_bad(proxy, cooldown=120.0)
                        last_status = 429
                        if attempt < 2:
                            continue  # 换下一个代理重试，不立即返回
                        # 3 个代理均被 429 限流：本地代理对目标不可用，直接禁止整池
                        ban_local_pool(f"{url} 连续 429 限流")
                        log.warning(f"429限流 {url} | 已尝试{attempt+1}个代理均被限流，已禁止本地代理 | body={body}")
                    elif status in (403, 401):
                        log.warning(f"{status}拒绝 {url} | proxy={proxy}")
                        last_status = status
                        # 403 多为 Cloudflare 挑战页，交由第四步 cloudscraper 兜底
                        cloudflare_blocked = status == 403
                        break
                    else:
                        log.warning(f"HTTP {status} {url} | proxy={proxy} | body={body}")
                        return None, status

                # 200但Content-Type不是JSON
                if status == 200 and "json" not in ct.lower():
                    body = response.text[:200].replace("\n", " ").strip() if response.text else ""
                    self.mark_proxy_bad(proxy, cooldown=60.0)
                    log.warning(f"非JSON响应 {url} | ct={ct} proxy={proxy} | body={body}")
                    return None, status

                if status == 200:
                    return response.json(), status

            except requests.exceptions.Timeout:
                last_status = 0
                if attempt == 2:
                    log.warning(f"请求超时 {url} | proxy={proxy} | timeout={timeout}s")
                    return None, 0
            except requests.exceptions.ProxyError as e:
                # 402 欠费 / 407 认证失败：整池熔断，不再逐个换代理重试
                if self._handle_proxy_failure(proxy, e):
                    pool_unavailable = True
                    log.warning(f"代理账号级故障，跳过剩余代理 {url} | {str(e)[:160]}")
                    break
                last_status = 0
                if attempt == 2:
                    log.warning(f"连接失败 {url} | proxy={proxy} | {e}")
                    return None, 0
            except requests.exceptions.ConnectionError as e:
                self.mark_proxy_bad(proxy, cooldown=60.0)
                last_status = 0
                if attempt == 2:
                    log.warning(f"连接失败 {url} | proxy={proxy} | {e}")
                    return None, 0
            except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
                log.warning(f"JSON解析失败 {url} | proxy={proxy}")
                return None, 0
            except Exception as e:
                last_status = 0
                if attempt == 2:
                    log.warning(f"请求失败 {url} | proxy={proxy} | {type(e).__name__}: {e}")
                    return None, 0
            time.sleep(1)

        # 第三步：直连降级（429 限流，或代理池整体不可用/已关闭时）
        if last_status == 429 or pool_unavailable:
            try:
                response = self.session.get(url, timeout=timeout)
                code = response.status_code
                if code == 200:
                    ct = response.headers.get("Content-Type", "")
                    if "json" in ct.lower():
                        return response.json(), 200
                    log.warning(f"直连降级非JSON响应 {url} | ct={ct}")
                else:
                    log.warning(f"直连降级失败 {url} | HTTP {code}")
                    if code == 403:
                        # 403 多为 Cloudflare 挑战页，交由第四步 cloudscraper 兜底
                        cloudflare_blocked = True
                    if code != 429:
                        # 记录真实状态码：404/410 等确定性结果直接返回，不再白跑一次 cloudscraper
                        last_status = code
            except Exception as e:
                log.warning(f"直连降级失败 {url} | {type(e).__name__}: {e}")

        # 第四步：cloudscraper 兜底（Cloudflare JS 挑战 / 代理通道全部被拒）
        if cloudflare_blocked or last_status in (429, 0):
            data = self._fetch_json_via_cloudscraper(url, timeout)
            if data is not None:
                return data, 200

        return None, last_status

    def _fetch_json_via_cloudscraper(self, url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[dict]:
        """用 cloudscraper 直连兜底，绕过 Cloudflare 的 JS 挑战"""
        if not cloudflare_client.is_available():
            return None
        response = cloudflare_client.get(url, timeout=timeout)
        if response is None:
            return None
        ct = response.headers.get("Content-Type", "")
        if response.status_code == 200 and "json" in ct.lower():
            try:
                data = response.json()
            except ValueError:
                log.warning(f"cloudscraper 响应无法解析为 JSON {url}")
                return None
            if isinstance(data, dict):
                log.info(f"cloudscraper 兜底成功: {url}")
                return data
        log.debug(f"cloudscraper 兜底未命中 {url} | HTTP {response.status_code}")
        return None

    def fetch_bytes(self, url: str, timeout: int = REQUEST_TIMEOUT,
                     status_holder: Optional[list] = None) -> Optional[bytes]:
        """获取原始字节（sitemap XML 等非 JSON 目标）

        顺序：cloudscraper 直连 → 代理服务 → 本地代理池。
        sitemap 是静态文件，直连优先，可省去代理服务的额外一跳。
        本地代理池关闭（settings.local_proxy_pool_enabled=False）时末级改为直连兜底。

        Args:
            status_holder: 可选列表，每级降级的 HTTP 状态码会追加进去（供调用方
                识别 429 限流等信号；sitemap 通道用它做按域退避）
        """
        def _note(status: int):
            if status_holder is not None:
                status_holder.append(status)

        # 1. cloudscraper 直连（同时可绕 Cloudflare）
        response = cloudflare_client.get(url, timeout=timeout)
        if response is not None:
            _note(response.status_code)
            if response.status_code == 200 and response.content:
                return response.content

        # 2. 代理服务（服务专用超时预算，见 PROXY_SERVICE_FETCH_TIMEOUT）
        content, status = self.proxy_service.fetch_bytes(url, timeout=PROXY_SERVICE_FETCH_TIMEOUT)
        _note(status)
        if status == 200 and content:
            return content

        # 3. 本地代理池（关闭或已被禁止时改为直连兜底，避免整级缺失）
        if not _LOCAL_PROXY_POOL_ENABLED or is_local_pool_banned():
            try:
                response = self.session.get(url, timeout=timeout)
                _note(response.status_code)
                if response.status_code == 200 and response.content:
                    return response.content
                log.warning(f"sitemap 直连获取失败 {url} | HTTP {response.status_code}")
            except Exception as e:
                log.warning(f"sitemap 直连获取异常 {url} | {type(e).__name__}: {e}")
            return None
        if self.proxy_manager:
            proxy = self.get_next_proxy()
            if proxy is None:
                log.warning(f"sitemap 本地代理池不可用，跳过代理 {url}")
                return None
            try:
                response = self.session.get(url, timeout=timeout, proxies=proxy)
                _note(response.status_code)
                if response.status_code == 200 and response.content:
                    return response.content
                log.warning(f"sitemap 获取失败 {url} | HTTP {response.status_code} | proxy={proxy}")
            except Exception as e:
                if self._handle_proxy_failure(proxy, e):
                    log.warning(f"sitemap 代理账号级故障 {url} | {str(e)[:160]}")
                    return None
                log.warning(f"sitemap 获取异常 {url} | {type(e).__name__}: {e}")
        return None

    def fetch_text(self, url: str, timeout: int = REQUEST_TIMEOUT,
                    status_holder: Optional[list] = None) -> str:
        """获取文本内容（商品页 HTML 等）"""
        content = self.fetch_bytes(url, timeout=timeout, status_holder=status_holder)
        if not content:
            return ""
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def fetch_meta(self, url: str) -> Optional[dict]:
        """获取 Shopify /meta.json 原始内容（同时给出货币与商品总数）"""
        meta_url = f"{normalize_url(url)}/meta.json"
        data, status = self.fetch_json(meta_url, timeout=15, direct_first=True)
        if status == 200 and isinstance(data, dict):
            return data
        return None
    
    def fetch_currency(self, url: str) -> str:
        """获取货币类型"""
        meta = self.fetch_meta(url)
        if meta:
            currency = meta.get("currency", "USD")
            return str(currency).upper() if currency else "USD"
        return ""

    @staticmethod
    def _published_product_count(meta: Optional[dict]) -> int:
        """读取 meta.json 中的全店商品数，缺失/非法时返回 0"""
        if not isinstance(meta, dict):
            return 0
        try:
            return int(meta.get("published_products_count") or 0)
        except (TypeError, ValueError):
            return 0

    def parse_product_record(self, product: dict, *, rate: float, currency: str, url: str,
                             domain: str, category_label: Optional[str], category_fallback: str,
                             source_category: str, subcategory_norm: str) -> Optional[Dict]:
        """把 Shopify 商品对象转成入库记录

        products.json 分页、导航集合、sitemap 三条通道共用同一套字段映射与过滤
        规则，保证不同通道产出的数据完全一致。

        Args:
            category_label: 分类名优先值（导航模式的 level2）；None 时取 product_type
            category_fallback: 分类名兜底值（一级分类名）

        Returns:
            记录字典；不符合过滤条件时返回 None
        """
        if not isinstance(product, dict):
            return None

        title = str(product.get("title") or "").strip()
        desc = str(product.get("body_html") or "").strip()
        if not title or not desc:
            return None

        image = extract_images(product.get("images", []) or [])
        if not image:
            return None

        variants = product.get("variants", []) or []
        sku, variant_str = extract_variant_info(variants, product.get("options", []) or [])
        compare_at_price, price = extract_prices(variants)
        original_price = convert_price(compare_at_price, rate)
        discount_price = convert_price(price, rate)
        price_value = discount_price if discount_price != "" else original_price
        if price_value == "" or float(price_value) < 1:
            return None

        product_type = str(product.get("product_type") or "").strip()
        return {
            "product_id": str(product.get("id") or "").strip(),
            "SKU": sku,
            "标题": title,
            "描述": desc,
            "子描述": "",
            "图片": image,
            "原价": str(original_price) if original_price != "" else "",
            "折扣价": discount_price,
            "变体": variant_str,
            "分类": category_label or product_type or category_fallback,
            "currency": currency,
            "source_url": url,
            "source_domain": domain,
            "source_category": source_category,
            "source_subcategory": subcategory_norm,
            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "unique_key": product_unique_key(title),
        }

    # ── Sitemap 兜底通道 ──────────────────────────────────

    def crawl_site_via_sitemap(self, url: str, category: str, currency: str, rate: float,
                               progress_callback=None, stop_event: threading.Event = None,
                               subcategory: str = "", reason: str = "",
                               flush_callback: Optional[Callable[[List[dict]], int]] = None) -> Dict:
        """Sitemap 兜底通道：从 /sitemap.xml 枚举商品链接并逐个取数

        仅在 products.json 不可用（被禁用/无响应）或全店商品数达到
        products.json 上限（25000）时启用，其余情况仍走 products.json 分页通道。

        Args:
            currency: 已由 meta.json 确认的货币
            rate: 该货币对 USD 的汇率
            reason: 触发兜底的原因（用于日志与进度展示）
            flush_callback: 可选落库回调 (batch) -> int；传入后每攒满一批商品
                立即调用一次入库（边爬边写），避免整站几万条堆在内存里、
                爬完才一次性写入导致长时间看不到数据库增长
        """
        url = normalize_url(url)
        domain = get_domain(url)
        subcategory_norm = normalize_subcategory(subcategory)

        def _progress(message: str):
            if progress_callback:
                progress_callback(message)

        try:
            log.info(f"[{domain}] 启用 sitemap 兜底通道（{reason}）")
            _progress(f"[{domain}] sitemap 兜底通道（{reason}）")

            product_urls = sitemap_fetcher.collect_product_urls(
                url, self.fetch_bytes, stop_event=stop_event, progress_callback=_progress,
                workers=sitemap_fetcher.SITEMAP_DISCOVER_WORKERS,
            )
            if not product_urls:
                log.warning(f"[{domain}] sitemap 未发现商品链接")
                return {"success": False, "products": [], "count": 0,
                        "error": "sitemap 未发现商品链接", "crawl_mode": "sitemap"}

            total_urls = len(product_urls)
            _progress(f"[{domain}] sitemap 发现 {total_urls} 个商品，开始取数")

            all_products: List[dict] = []   # 未落库缓冲（flush 模式下仅剩尾部残余）
            pending: List[dict] = []        # flush 模式攒批缓冲
            flushed_saved = 0               # flush 回调累计入库数
            total_valid = 0                 # 累计有效商品数（进度展示）
            seen_unique_keys = set()
            done_count = 0
            failed_count = 0
            # 时间预算与僵尸判定：超预算或长时间零产出时置 abandon，
            # 取数线程快速返回，释放站点并发名额（已入库数据不受影响）
            t_start = time.time()
            last_valid_time = t_start
            abandon_event = threading.Event()

            # 先探测一次 .json 端点：被禁用时全部改走商品页 JSON-LD，
            # 避免每个商品都白等一轮失败重试
            prefer_jsonld = False
            if stop_event is None or not stop_event.is_set():
                prefer_jsonld = self._json_endpoint_blocked(product_urls[0])

            workers = max(1, min(SITEMAP_FETCH_WORKERS, total_urls))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sitemap_fetch") as executor:
                futures = {
                    executor.submit(self._fetch_sitemap_product, pu, prefer_jsonld,
                                    stop_event, abandon_event): pu
                    for pu in product_urls
                }
                for future in as_completed(futures):
                    if stop_event is not None and stop_event.is_set():
                        log.info(f"[{domain}] 收到停止信号，已取 {total_valid} 件")
                        break
                    if not abandon_event.is_set():
                        now = time.time()
                        if now - t_start > SITEMAP_TIME_BUDGET:
                            abandon_event.set()
                            log.warning(
                                f"[{domain}] sitemap 通道超过时间预算"
                                f"（{SITEMAP_TIME_BUDGET // 60:.0f} 分钟），"
                                f"已取 {total_valid} 件，提前收尾"
                            )
                        elif now - t_start > 600 and now - last_valid_time > 600:
                            abandon_event.set()
                            log.warning(
                                f"[{domain}] sitemap 通道连续 "
                                f"{(now - last_valid_time) / 60:.0f} 分钟零产出"
                                f"（已取 {total_valid} 件），判定为僵尸爬取，提前收尾"
                            )
                    done_count += 1
                    try:
                        product = future.result()
                    except Exception:
                        product = None
                    if product is None:
                        failed_count += 1
                        continue

                    record = self.parse_product_record(
                        product, rate=rate, currency=currency, url=url, domain=domain,
                        category_label=None, category_fallback=category,
                        source_category=category, subcategory_norm=subcategory_norm,
                    )
                    if record is None:
                        continue
                    if record["unique_key"] in seen_unique_keys:
                        continue
                    seen_unique_keys.add(record["unique_key"])
                    total_valid += 1
                    last_valid_time = time.time()
                    if flush_callback is not None:
                        # 边爬边写：攒满一批立即落库
                        pending.append(record)
                        if len(pending) >= SAVE_FLUSH_BATCH_SIZE:
                            flushed_saved += flush_callback(pending)
                            pending = []
                            _progress(
                                f"[{domain}] sitemap 已入库 {flushed_saved} 件"
                                f"（进度 {done_count}/{total_urls}）"
                            )
                    else:
                        all_products.append(record)
                        if len(all_products) % 200 == 0:
                            _progress(
                                f"[{domain}] sitemap 已取 {len(all_products)} 件"
                                f"（进度 {done_count}/{total_urls}）"
                            )

            # flush 模式：收尾把剩余批次写入
            if flush_callback is not None and pending:
                flushed_saved += flush_callback(pending)
                pending = []

            # 全部商品链接都取数失败：属于通道失败而非"无有效商品"，
            # 否则会以 success=True 把站点标记为已爬取，永久丢失
            if not all_products and not flushed_saved and failed_count == total_urls:
                log.warning(f"[{domain}] sitemap 通道 {total_urls} 个商品全部取数失败")
                return {"success": False, "products": [], "count": 0,
                        "error": "sitemap 商品取数全部失败", "crawl_mode": "sitemap"}

            if abandon_event.is_set():
                log.info(f"[{domain}] sitemap 提前收尾: {total_valid}/{total_urls} 件有效商品"
                         f"（时间预算 {SITEMAP_TIME_BUDGET // 60:.0f} 分钟 / 僵尸判定）")
            else:
                log.info(f"[{domain}] sitemap 通道完成: {total_valid}/{total_urls} 件有效商品")
            _progress(f"[{domain}] sitemap 完成: {total_valid} 件")
            return {
                "success": True,
                "products": all_products,
                "count": total_valid,
                "domain": domain,
                "currency": currency,
                "crawl_mode": "sitemap",
            }
        except Exception as e:
            log.error(f"[{domain}] sitemap 通道异常: {e}")
            return {"success": False, "products": [], "count": 0,
                    "error": str(e), "crawl_mode": "sitemap"}

    def _fetch_sitemap_product(self, product_url: str, prefer_jsonld: bool = False,
                               stop_event: threading.Event = None,
                               abandon_event: threading.Event = None) -> Optional[dict]:
        """取单个商品的数据

        顺序：<商品链接>.json（直连优先） → 商品页 JSON-LD 兜底。
        部分店铺会同时禁用 /products.json 与 /products/<handle>.json，
        此时只能从商品页内嵌的 JSON-LD 取数。

        429 限流处理：请求前若该域处于冷却期则等待；命中 429 则记录指数退避、
        冷却结束后重试一次本商品（而非直接丢弃），连续两次 429 说明本出口 IP
        已被持续限流，改经代理服务（不同出口 IP）抢救该商品。

        Args:
            prefer_jsonld: 已探测到 .json 端点被禁用，直接走商品页解析
            stop_event: 可选停止信号（冷却等待期间可被打断）
            abandon_event: 可选放弃信号（站点超时间预算/僵尸判定后快速返回）
        """
        if abandon_event is not None and abandon_event.is_set():
            return None
        domain = get_domain(product_url)
        self._wait_if_throttled(domain, stop_event, abandon_event)
        # 同域平滑限速：预约时间片错峰出发，避免突发触发 429
        pace_wait = self._domain_pace_delay(domain)
        if pace_wait > 0:
            time.sleep(pace_wait)

        json_url = product_url.rstrip("/") + ".json"
        if not prefer_jsonld:
            for attempt in range(2):
                data, status = self.fetch_json(json_url, timeout=REQUEST_TIMEOUT, direct_first=True)
                if status != 429:
                    break
                self._throttle_domain(domain)
                if attempt == 0 and not (stop_event and stop_event.is_set()) \
                        and not (abandon_event and abandon_event.is_set()):
                    # 冷却结束后重试一次本商品
                    self._wait_if_throttled(domain, stop_event, abandon_event)
                    continue
                break
            if status == 429 and not (stop_event and stop_event.is_set()) \
                    and not (abandon_event and abandon_event.is_set()):
                # 连续 429：直连出口已被持续限流，换出口 IP 抢救本商品
                data, status = self.fetch_json(json_url, timeout=REQUEST_TIMEOUT)
                if status == 429:
                    return None
            if isinstance(data, dict):
                product = data.get("product")
                if isinstance(product, dict):
                    self._domain_unthrottle(domain)
                    return product
                # 少数主题直接返回商品对象本身
                if data.get("title"):
                    self._domain_unthrottle(domain)
                    return data

        status_holder: List[int] = []
        html = self.fetch_text(product_url, status_holder=status_holder)
        if 429 in status_holder:
            self._throttle_domain(domain)
        if html:
            self._domain_unthrottle(domain)
            return sitemap_fetcher.extract_jsonld_product(html)
        return None

    def _json_endpoint_blocked(self, product_url: str) -> bool:
        """探测单个商品的 .json 端点是否被禁用（直连优先，快速得出结论）"""
        data, status = self.fetch_json(product_url.rstrip("/") + ".json",
                                        timeout=REQUEST_TIMEOUT, direct_first=True)
        blocked = not (isinstance(data, dict) and (data.get("product") or data.get("title")))
        if blocked:
            log.info(f".json 端点不可用（HTTP {status}），改从商品页 JSON-LD 取数: {product_url}")
        return blocked
    
    def crawl_site(self, url: str, category: str, progress_callback=None,
                   stop_event: threading.Event = None, subcategory: str = "",
                   flush_callback: Optional[Callable[[List[dict]], int]] = None) -> Dict:
        """爬取单个站点的商品数据
        
        Args:
            url: 站点URL
            category: 一级分类名称
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选）
            subcategory: 二级分类名称（空字符串归入 "other"）
            flush_callback: 可选落库回调 (batch) -> int；传入后边爬边写，
                每攒满 SAVE_FLUSH_BATCH_SIZE 条商品立即入库一次
            
        Returns:
            {"success": bool, "products": list, "count": int,
             "crawl_mode": "sitemap"(仅兜底通道)}

        通道选择：
            - meta.json 商品数达到 products.json 上限 → sitemap 兜底
            - products.json 不可用（403/非 JSON/无响应）→ sitemap 兜底
            - 其余情况 → products.json 分页（含导航集合分页）
        """
        url = normalize_url(url)
        domain = get_domain(url)
        subcategory_norm = normalize_subcategory(subcategory)
        
        try:
            if progress_callback:
                progress_callback(f"[{domain}] 开始爬取")
            
            # 获取货币
            if progress_callback:
                progress_callback(f"[{domain}] 获取货币...")
            meta = self.fetch_meta(url)
            if not meta:
                log.warning(f"[{domain}] 非 Shopify 站点（meta.json 无响应）")
                return {"success": False, "products": [], "count": 0, "error": "非 Shopify 站点"}

            currency = str(meta.get("currency") or "USD").upper()
            
            rate = self.currency_map.get(currency)
            if rate is None:
                log.warning(f"[{domain}] 未找到汇率: {currency}")
                return {"success": False, "products": [], "count": 0, "error": f"无汇率配置: {currency}"}
            
            # 全店商品数达到 products.json 上限时，分页接口最多只能覆盖
            # 200×MAX_PAGE_LIMIT 条，必然截断，直接改走 sitemap 通道
            published_count = self._published_product_count(meta)
            if published_count >= PRODUCTS_JSON_MAX_PRODUCTS:
                return self.crawl_site_via_sitemap(
                    url, category, currency, rate, progress_callback,
                    stop_event=stop_event, subcategory=subcategory,
                    reason=f"商品数 {published_count} 达到 products.json 上限 {PRODUCTS_JSON_MAX_PRODUCTS}",
                    flush_callback=flush_callback,
                )

            if progress_callback:
                progress_callback(f"[{domain}] 汇率 OK: {currency}，开始爬取商品")
            
            # 探针检测（直连优先，快速判断通道可用性）
            probe_url = f"{url}/products.json?limit=200&page=1"
            probe_data, probe_code = self.fetch_json(probe_url, timeout=15, direct_first=True)
            
            if probe_code != 200 or not isinstance(probe_data, dict):
                log.warning(f"[{domain}] products.json 不可用，改用 sitemap 兜底通道")
                return self.crawl_site_via_sitemap(
                    url, category, currency, rate, progress_callback,
                    stop_event=stop_event, subcategory=subcategory,
                    reason=f"products.json 不可用 (HTTP {probe_code})",
                    flush_callback=flush_callback,
                )
            
            products_count = len(probe_data.get("products", []))
            if products_count == 0:
                log.warning(f"[{domain}] products.json 返回空列表")
                return {"success": False, "products": [], "count": 0, "error": "无商品数据"}
            
            if progress_callback:
                progress_callback(f"[{domain}] 探针通过 ({products_count} 商品)")
            
            # 爬取所有页面
            all_products = []
            pending = []            # flush 模式攒批缓冲
            flushed_saved = 0       # flush 回调累计入库数
            total_valid = 0         # 累计有效商品数（进度展示）
            seen_unique_keys = set()
            page = 1
            empty_pages = 0
            empty_saved_pages = 0

            while empty_pages < MAX_EMPTY_PAGES and empty_saved_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                if stop_event and stop_event.is_set():
                    if flush_callback is not None and pending:
                        flushed_saved += flush_callback(pending)
                        pending = []
                    log.info(f"[{domain}] 收到停止信号，已爬取 {total_valid} 件")
                    return {"success": True, "products": all_products, "count": total_valid,
                            "domain": domain, "currency": currency}

                products_url = f"{url}/products.json?limit=200&page={page}"
                data, code = self.fetch_json(products_url, direct_first=True)
                
                if code != 200:
                    break
                
                products = data.get("products", []) if isinstance(data, dict) else []
                if not products:
                    empty_pages += 1
                    empty_saved_pages += 1
                    page += 1
                    time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
                    continue

                empty_pages = 0
                page_products = []
                page_valid = 0
                
                for product in products:
                    record = self.parse_product_record(
                        product, rate=rate, currency=currency, url=url, domain=domain,
                        category_label=None, category_fallback=category,
                        source_category=category, subcategory_norm=subcategory_norm,
                    )
                    if record is None:
                        continue
                    if record["unique_key"] in seen_unique_keys:
                        continue
                    seen_unique_keys.add(record["unique_key"])
                    total_valid += 1
                    page_valid += 1
                    if flush_callback is not None:
                        # 边爬边写：攒满一批立即落库
                        pending.append(record)
                        if len(pending) >= SAVE_FLUSH_BATCH_SIZE:
                            flushed_saved += flush_callback(pending)
                            pending = []
                    else:
                        page_products.append(record)
                
                all_products.extend(page_products)

                if not page_valid:
                    empty_saved_pages += 1
                else:
                    empty_saved_pages = 0

                if progress_callback and page % 5 == 0:
                    progress_callback(f"[{domain}] 第{page}页: 累计{total_valid}件")

                if empty_saved_pages >= MAX_EMPTY_PAGES:
                    if progress_callback:
                        progress_callback(f"[{domain}] 连续{MAX_EMPTY_PAGES}页无有效商品，跳过该站点")
                    log.info(f"[{domain}] 连续{MAX_EMPTY_PAGES}页无有效商品，跳过")
                    break

                page += 1
                if stop_event and stop_event.is_set():
                    break
                time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
            
            # flush 模式：收尾把剩余批次写入
            if flush_callback is not None and pending:
                flushed_saved += flush_callback(pending)
                pending = []

            if progress_callback:
                progress_callback(f"[{domain}] 完成: {total_valid} 件商品")
            
            return {
                "success": True,
                "products": all_products,
                "count": total_valid,
                "domain": domain,
                "currency": currency
            }
            
        except Exception as e:
            log.error(f"[{domain}] 爬取异常: {e}")
            return {"success": False, "products": [], "count": 0, "error": str(e)}
    
    def crawl_site_with_nav(self, url: str, category: str, progress_callback=None, subcategory: str = "") -> Dict:
        """基于导航的深度爬取单个站点

        解析店铺导航栏获取分类结构，按集合逐类爬取商品数据。
        商品分类来自导航栏的两级结构（level1 > level2），而非 product_type。

        Args:
            url: 站点URL
            category: 一级分类名称（来源类目，用于数据库存储）
            progress_callback: 进度回调函数
            subcategory: 二级分类名称（空字符串归入 "other"，覆盖导航解析的 level2）

        Returns:
            {"success": bool, "products": list, "count": int, "collections": int}
        """
        url = normalize_url(url)
        domain = get_domain(url)
        override_sub = normalize_subcategory(subcategory) if subcategory else ""

        try:
            if progress_callback:
                progress_callback(f"[{domain}] 开始导航爬取")

            # 获取货币
            if progress_callback:
                progress_callback(f"[{domain}] 获取货币...")
            currency = self.fetch_currency(url)
            if not currency:
                log.warning(f"[{domain}] 非 Shopify 站点（meta.json 无响应）")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "非 Shopify 站点"}

            rate = self.currency_map.get(currency)
            if rate is None:
                log.warning(f"[{domain}] 未找到汇率: {currency}")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": f"无汇率配置: {currency}"}

            if progress_callback:
                progress_callback(f"[{domain}] 汇率 OK: {currency}，解析导航...")

            # 解析导航栏
            nav_items = parse_navigation(url)
            if not nav_items:
                log.warning(f"[{domain}] 导航解析为空")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "导航解析为空"}

            # 过滤出有 handle 的分类项
            valid_nav = [(l1, l2, cu, h) for l1, l2, cu, h in nav_items if h]
            if not valid_nav:
                log.warning(f"[{domain}] 无有效集合")
                return {"success": False, "products": [], "count": 0, "collections": 0, "error": "无有效集合"}

            if progress_callback:
                progress_callback(f"[{domain}] 发现 {len(valid_nav)} 个集合，开始爬取")

            all_products = []
            seen_unique_keys = set()
            crawled_collections = 0

            for level1, level2, coll_url, handle in valid_nav:
                if not handle:
                    continue

                if progress_callback:
                    progress_callback(f"[{domain}] 爬取分类: {level1} > {level2} (/{handle})")

                collection_saved = 0
                page = 1
                empty_pages = 0
                empty_saved_pages = 0

                while empty_pages < MAX_EMPTY_PAGES and empty_saved_pages < MAX_EMPTY_PAGES and page <= MAX_PAGE_LIMIT:
                    products_url = f"{url}/collections/{handle}/products.json?limit=200&page={page}"
                    data, code = self.fetch_json(products_url, direct_first=True)

                    if code != 200:
                        break

                    products = data.get("products", []) if isinstance(data, dict) else []
                    if not products:
                        empty_pages += 1
                        empty_saved_pages += 1
                        page += 1
                        time.sleep(random.uniform(*PAGE_SLEEP_RANGE))
                        continue

                    empty_pages = 0
                    page_products = []

                    for product in products:
                        record = self.parse_product_record(
                            product, rate=rate, currency=currency, url=url, domain=domain,
                            category_label=level2 or None, category_fallback=category,
                            source_category=level1,
                            subcategory_norm=override_sub or normalize_subcategory(level2),
                        )
                        if record is None:
                            continue
                        if record["unique_key"] in seen_unique_keys:
                            continue
                        seen_unique_keys.add(record["unique_key"])
                        page_products.append(record)

                    all_products.extend(page_products)
                    collection_saved += len(page_products)

                    if not page_products:
                        empty_saved_pages += 1
                    else:
                        empty_saved_pages = 0

                    if empty_saved_pages >= MAX_EMPTY_PAGES:
                        if progress_callback:
                            progress_callback(f"[{domain}] /{handle} 连续{MAX_EMPTY_PAGES}页无有效商品，跳过该集合")
                        log.info(f"[{domain}] /{handle} 连续{MAX_EMPTY_PAGES}页无有效商品，跳过")
                        break

                    page += 1
                    time.sleep(random.uniform(*PAGE_SLEEP_RANGE))

                crawled_collections += 1
                if progress_callback:
                    progress_callback(f"[{domain}] /{handle} 完成: {collection_saved} 件")

                # 集合间短暂休息
                time.sleep(random.uniform(1.0, 2.5))

            if progress_callback:
                progress_callback(f"[{domain}] 导航爬取完成: {crawled_collections} 个集合，{len(all_products)} 件商品")

            return {
                "success": True,
                "products": all_products,
                "count": len(all_products),
                "collections": crawled_collections,
                "domain": domain,
                "currency": currency,
            }

        except Exception as e:
            log.error(f"[{domain}] 导航爬取异常: {e}")
            return {"success": False, "products": [], "count": 0, "collections": 0, "error": str(e)}

    def _crawl_single_site(self, url_doc: dict, category: str, site_index: int,
                           total_sites: int, progress_callback=None,
                           stop_event: threading.Event = None, subcategory: str = "") -> Dict:
        """爬取单个站点的商品数据并保存（线程安全，每线程独立 crawler + db 实例）

        Args:
            url_doc: 包含 url 和 domain 的字典
            category: 一级分类名称
            site_index: 站点序号（用于日志）
            total_sites: 总站点数
            progress_callback: 进度回调函数
            stop_event: 停止信号事件
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        url = url_doc.get("url", "")
        domain = url_doc.get("domain", "")

        if stop_event and stop_event.is_set():
            log.info(f"[{site_index}/{total_sites}] 跳过（已停止）: {domain}")
            return {"success": False, "saved": 0, "url": url, "domain": domain, "error": "任务已停止", "stopped": True}

        # 进程级站点并发名额：多个分类任务同时跑时限制全局并发站点数
        if not self._acquire_site_slot(stop_event):
            log.info(f"[{site_index}/{total_sites}] 跳过（等待并发名额时被停止）: {domain}")
            return {"success": False, "saved": 0, "url": url, "domain": domain, "error": "任务已停止", "stopped": True}

        crawler = create_crawler()
        product_db = ProductDBClient()
        try:
            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 开始: {domain}")

            saved_total = 0

            # 边爬边写：crawl_site 每攒满一批商品调用一次本回调立即入库，
            # 长时爬取（尤其 sitemap 通道几万条）也能实时看到数据库增长
            def _flush(batch):
                nonlocal saved_total
                if not batch:
                    return 0
                n = product_db.save_raw_products(category, subcategory, batch)
                saved_total += n
                if progress_callback:
                    progress_callback(f"[{site_index}/{total_sites}] 已入库 {saved_total} 件: {domain}")
                return n

            result = crawler.crawl_site(url, category, progress_callback, stop_event=stop_event,
                                        subcategory=subcategory, flush_callback=_flush)

            # 收尾：把未走 flush 回调的剩余商品一次性入库（无 flush 的老路径也兼容）
            if result["success"] and result["products"]:
                saved_total += product_db.save_raw_products(category, subcategory, result["products"])

            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 完成: {domain} ({saved_total} 件)")

            return {
                "success": result["success"],
                "saved": saved_total,
                "url": url,
                "domain": domain,
                "error": result.get("error"),
                "stopped": False,
            }
        except Exception as e:
            if stop_event and stop_event.is_set():
                log.info(f"[{site_index}/{total_sites}] 停止: {domain}")
            else:
                log.error(f"[{site_index}/{total_sites}] 站点异常 {domain}: {e}")
            if progress_callback:
                progress_callback(f"[{site_index}/{total_sites}] 失败: {domain} ({e})")
            return {"success": False, "saved": 0, "url": url, "domain": domain, "error": str(e), "stopped": bool(stop_event and stop_event.is_set())}
        finally:
            _site_concurrency.release()
            crawler.close()
            product_db.close()

    def _mark_crawled(self, source_db, category: str, crawled_domains: dict, subcategory: str = ""):
        """将已处理域名的 collection URL 在同一集合中标记 crawl_status（不再跨集合移动）

        Args:
            source_db: MongoDBClient 实例
            category: 一级分类名称
            crawled_domains: {domain: {"products": int, "success": bool}, ...}
                             success=False 表示爬取失败的域名（同样会被标记，避免反复重试）
            subcategory: 二级分类名称（空字符串归入 "other"）
        """
        if not crawled_domains:
            return
        try:
            from qmds.db.mongodb import CRAWL_STATUS_CRAWLED, CRAWL_STATUS_FAILED
            filtered_col = source_db.filtered_col(category, subcategory)
            domains = list(crawled_domains.keys())
            # 查询这些域名的所有 collection URL 文档
            filtered_docs = list(filtered_col.find(
                {"domain": {"$in": domains}},
                {"url": 1, "domain": 1, "_id": 0}
            ))
            if not filtered_docs:
                return
            url_crawl_info_list = []
            for doc in filtered_docs:
                domain = doc.get("domain", "")
                info = crawled_domains.get(domain, {})
                url_crawl_info_list.append({
                    "url": doc["url"],
                    "products": info.get("products", 0),
                    "success": info.get("success", False),
                })
            if url_crawl_info_list:
                moved = source_db.move_to_crawled_batch(category, url_crawl_info_list, subcategory=subcategory)
                log.info(f"[{category}/{subcategory or 'other'}] 已标记 {moved} 条集合URL为已爬取")
        except Exception as e:
            log.error(f"[{category}/{subcategory or 'other'}] 标记爬取状态失败: {e}")

    def crawl_category(self, category: str, max_sites: int = 0, workers: int = 1,
                       progress_callback=None, stop_event: threading.Event = None,
                       subcategory: str = "") -> Dict:
        """爬取指定分类的商品数据（支持多线程并发）

        从数据库的 {prefix} 集合获取 filter_status=filtered 且 crawl_status=uncrawled 的店铺URL，通过 products.json API 爬取商品。
        workers=1 时串行执行（与旧行为一致），workers>1 时使用线程池并发爬取。
        每个线程拥有独立的 ProductCrawler 实例（独立 Session + 代理）和 ProductDBClient 实例。

        Args:
            category: 一级分类名称
            max_sites: 最大爬取站点数（0 表示不限制，爬取所有可用 URL）
            workers: 并发线程数（默认 1 串行）
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选，传入后可即时停止所有线程）
            subcategory: 二级分类名称（空字符串归入 "other"）

        Returns:
            {"total_sites": int, "success_sites": int, "total_products": int}
        """
        subcategory_norm = normalize_subcategory(subcategory)
        # 从MongoDB获取 {prefix} 集合中未爬取的 filtered URL（去重）
        source_db = MongoDBClient()
        filtered_col = source_db.filtered_col(category, subcategory_norm)

        # 获取去重后的店铺URL（优先用 store_url，回退到 url 的域名根路径）
        # 只查询 filter_status=filtered 且 crawl_status=uncrawled 的文档
        seen_domains = set()
        store_urls = []
        query = {"filter_status": "filtered", "crawl_status": "uncrawled"}
        for doc in filtered_col.find(query, {"url": 1, "domain": 1, "store_url": 1, "_id": 0}):
            domain = doc.get("domain", "")
            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)
            store_url = doc.get("store_url") or ""
            if not store_url:
                raw_url = doc.get("url", "")
                parsed = urlparse(raw_url)
                store_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
            if store_url:
                store_urls.append({"url": store_url, "domain": domain})

        if not store_urls:
            source_db.close()
            log.warning(f"分类 {category}/{subcategory_norm} 无可用URL")
            return {"total_sites": 0, "success_sites": 0, "total_products": 0, "error": "无可用URL"}

        if max_sites > 0:
            store_urls = store_urls[:max_sites]
        total_sites = len(store_urls)

        # 预创建索引（共享，MongoDB 索引操作本身是幂等的）
        product_db = ProductDBClient()
        product_db.ensure_product_indexes(category, subcategory_norm)
        product_db.close()

        log.info(f"开始爬取分类 {category}/{subcategory_norm}: {total_sites} 个站点, {workers} 线程")
        if progress_callback:
            progress_callback(f"开始爬取: {category}/{subcategory_norm} - {total_sites} 个站点, {workers} 线程")

        # ── 串行模式 ──
        if workers <= 1:
            success_sites = 0
            total_products = 0
            crawled_domains = {}
            for i, url_doc in enumerate(store_urls, 1):
                if stop_event and stop_event.is_set():
                    log.info(f"分类 {category}/{subcategory_norm}: 收到停止信号，已处理 {i-1}/{total_sites} 站点")
                    break
                result = self._crawl_single_site(url_doc, category, i, total_sites,
                                                 progress_callback, stop_event=stop_event,
                                                 subcategory=subcategory_norm)
                if result["success"]:
                    success_sites += 1
                    total_products += result["saved"]
                # 被停止信号跳过的站点保留为 uncrawled，以便下次继续；其余（成功/失败）均标记为已爬取
                if not result.get("stopped"):
                    crawled_domains[url_doc["domain"]] = {"products": result["saved"], "success": result["success"]}
                if i < total_sites and not (stop_event and stop_event.is_set()):
                    time.sleep(random.uniform(*SITE_COOLDOWN_RANGE))

            self._mark_crawled(source_db, category, crawled_domains, subcategory=subcategory_norm)
            source_db.close()

            return {
                "total_sites": total_sites,
                "success_sites": success_sites,
                "total_products": total_products,
            }

        # ── 并发模式 ──
        lock = threading.Lock()
        success_sites = 0
        total_products = 0
        crawled_domains = {}

        def _worker(idx: int, url_doc: dict) -> dict:
            nonlocal success_sites, total_products, crawled_domains
            result = self._crawl_single_site(url_doc, category, idx, total_sites,
                                             progress_callback, stop_event=stop_event,
                                             subcategory=subcategory_norm)
            if result["success"]:
                with lock:
                    success_sites += 1
                    total_products += result["saved"]
            # 被停止信号跳过的站点保留为 uncrawled；其余（成功/失败）均标记为已爬取
            if not result.get("stopped"):
                with lock:
                    crawled_domains[url_doc["domain"]] = {"products": result["saved"], "success": result["success"]}
            return result

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl_site") as executor:
            futures = {
                executor.submit(_worker, i, url_doc): url_doc
                for i, url_doc in enumerate(store_urls, 1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    url_doc = futures[future]
                    log.error(f"线程异常 {url_doc.get('domain', '')}: {e}")

        self._mark_crawled(source_db, category, crawled_domains, subcategory=subcategory_norm)
        source_db.close()

        log.info(f"分类 {category}/{subcategory_norm} 爬取完成: {success_sites}/{total_sites} 站点, {total_products} 件商品")
        if progress_callback:
            progress_callback(f"爬取完成: {category}/{subcategory_norm} - {success_sites}/{total_sites} 站点, {total_products} 件商品")

        return {
            "total_sites": total_sites,
            "success_sites": success_sites,
            "total_products": total_products,
        }

    def crawl_category_all_subcategories(self, category: str, max_sites: int = 0,
                                         workers: int = 1, progress_callback=None,
                                         stop_event: threading.Event = None) -> Dict:
        """爬取一级分类下所有二级分类的商品数据

        从 MongoDB 动态获取该一级分类下所有有 filtered 数据的二级分类，
        依次调用 crawl_category() 爬取每个二级分类，汇总结果。

        Args:
            category: 一级分类名称
            max_sites: 每个二级分类最大爬取站点数（0 表示不限制）
            workers: 并发线程数（默认 1 串行）
            progress_callback: 进度回调函数
            stop_event: 停止信号事件（可选）

        Returns:
            {"total_subcategories": int, "total_sites": int,
             "success_sites": int, "total_products": int}
        """
        source_db = MongoDBClient()
        try:
            subcategories = source_db.list_filtered_subcategories(category)
        finally:
            source_db.close()

        if not subcategories:
            log.warning(f"一级分类 {category} 无可用二级分类")
            if progress_callback:
                progress_callback(f"一级分类 {category} 无可用二级分类")
            return {
                "total_subcategories": 0,
                "total_sites": 0,
                "success_sites": 0,
                "total_products": 0,
                "error": "无可用二级分类",
            }

        log.info(f"开始爬取一级分类 {category}: 共 {len(subcategories)} 个二级分类")
        if progress_callback:
            progress_callback(f"开始爬取一级分类 {category}: 共 {len(subcategories)} 个二级分类")

        grand_total_sites = 0
        grand_success_sites = 0
        grand_total_products = 0

        for idx, sub in enumerate(subcategories, 1):
            if stop_event and stop_event.is_set():
                log.info(f"一级分类 {category}: 收到停止信号，已完成 {idx - 1}/{len(subcategories)} 个二级分类")
                if progress_callback:
                    progress_callback(f"已停止: 完成 {idx - 1}/{len(subcategories)} 个二级分类")
                break

            sub_display = sub if sub else "other"
            if progress_callback:
                progress_callback(f"[{idx}/{len(subcategories)}] 开始爬取二级分类: {category}/{sub_display}")

            result = self.crawl_category(
                category, max_sites=max_sites, workers=workers,
                progress_callback=progress_callback, stop_event=stop_event,
                subcategory=sub,
            )

            grand_total_sites += result.get("total_sites", 0)
            grand_success_sites += result.get("success_sites", 0)
            grand_total_products += result.get("total_products", 0)

            if progress_callback:
                progress_callback(
                    f"[{idx}/{len(subcategories)}] 二级分类 {category}/{sub_display} 完成: "
                    f"成功 {result.get('success_sites', 0)}/{result.get('total_sites', 0)} 站点, "
                    f"{result.get('total_products', 0)} 件商品"
                )

        summary = (f"一级分类 {category} 爬取完成: {len(subcategories)} 个二级分类, "
                   f"{grand_success_sites}/{grand_total_sites} 站点, {grand_total_products} 件商品")
        log.info(summary)
        if progress_callback:
            progress_callback(summary)

        return {
            "total_subcategories": len(subcategories),
            "total_sites": grand_total_sites,
            "success_sites": grand_success_sites,
            "total_products": grand_total_products,
        }


def create_crawler() -> ProductCrawler:
    """创建爬取器实例"""
    # 加载汇率配置
    currency_config_path = settings.data_dir / "currency_config.json"
    if currency_config_path.exists():
        import json
        with open(currency_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        currency_map = {}
        if isinstance(data, list):
            for item in data:
                key = item.get("nation")
                value = item.get("exchange_rate_usd")
                if key and value is not None:
                    currency_map[str(key).upper()] = float(value)
        elif isinstance(data, dict):
            for key, value in data.items():
                currency_map[str(key).upper()] = float(value)
    else:
        # 默认汇率
        currency_map = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "CAD": 1.36, "AUD": 1.53}
    
    # 加载代理配置（ProxyManager 自动转换格式 + 支持标记坏代理 + 冷却轮换）
    # 本地代理池关闭、或探测后无可用代理（load_proxies 内部完成探测）时不再创建，
    # 爬取器整条链路都不会取代理，改走「代理服务 → 直连 → cloudscraper」降级链
    proxy_manager = (
        ProxyManager.from_settings()
        if (_LOCAL_PROXY_POOL_ENABLED and settings.load_proxies())
        else None
    )

    return ProductCrawler(currency_map=currency_map, proxy_manager=proxy_manager)
