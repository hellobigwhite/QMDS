"""电商平台检测器 — 单通道代理 meta.json 检测（2026-09 简化版）

检测方式（按用户要求简化）：
- 仅通过远程代理服务访问 {url}meta.json，不直连、不访问首页、无其他兜底；
- 判定：meta.json 返回 200 且 JSON 含 published_products_count 指标值
  > SHOPIFY_MIN_PRODUCTS（默认 20）→ 判定为 Shopify 网站。

判定语义：
- 200 + published_products_count > 20  → Shopify（confidence 0.95）
- 200 + published_products_count ≤ 20  → 非 Shopify（商品数不达标）
- 404 / 410                            → 非 Shopify（meta.json 不存在）
- 200 非 JSON / JSON 无该字段          → 非 Shopify（不是 Shopify 的 meta 格式）
- 0（超时）/ 403 / 429 / 5xx / -1 / -2 → 无法确认（inconclusive）：
  访问失败或被拦截不代表非 Shopify（如 techniquerecords.com 直连全程 429，
  却是真 Shopify 店），交由上层二轮复检；二轮仍失败由上层保存为
  "待确认"（uncertain），绝不按非 Shopify 误杀。

设计说明：
- 代理服务出口 IP 与直连不同，WAF 通常不会拦截代理访问，能拿到直连拿
  不到的 meta.json（techniquerecords.com 直连 429，代理大概率可访问）；
- 单通道请求量最小（每站 1 次代理请求 + 二轮复检最多 1 次），彻底去掉
  首页抓取 / DNS 预检 / cloudscraper / myshopify 回退等兜底，逻辑简单。
"""

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from qmds.modules.data_scraper.models.schemas import Platform
from qmds.utils.logger import get_logger

log = get_logger("detection")


# 判定为 Shopify 的最低商品数（meta.json 的 published_products_count 指标）
SHOPIFY_MIN_PRODUCTS = 20

# 检测超时（秒）：真 Shopify 的 meta.json 由 CDN 秒回，8 秒足够；
# 被 WAF 拦的站是快速 403/429（不走超时），挂起超时的几乎都是
# 无 meta.json 且响应极慢的杂站（政府站/论坛/博客等）。
DETECT_TIMEOUT_PROXY = 8

# 连续超时防护：单站挂起超时判"非 Shopify"；但若连续大量超时，
# 说明代理服务可能整体故障，改判"无法确认"保护真店不被大面积误杀。
MAX_CONSECUTIVE_TIMEOUTS = 5
_consecutive_timeouts = 0
_timeout_guard = threading.Lock()


@dataclass
class DetectionResult:
    platform: Platform
    product_count: int = 0
    store_name: str = ""
    currency: str = "USD"
    confidence: float = 0.0
    raw: dict = None
    page_text: str = ""  # 保留字段（后续语言检测可能使用）
    inconclusive: bool = False  # True=访问失败/被拦无法确认（区别于"确认非该平台"）

    def __bool__(self):
        return self.platform != Platform.UNKNOWN


class PlatformDetector:
    """电商平台检测器（单通道：远程代理访问 meta.json）"""

    def __init__(self, proxy_manager=None):
        self._proxy_manager = proxy_manager  # 兼容旧签名；单通道检测不使用直连
        # 商品数据爬取使用的代理服务客户端（懒加载）
        self._proxy_service = None
        self._proxy_service_failed = False
        # 同域名互斥锁：避免并发检测轰击同一站点触发限流
        self._domain_locks = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _get_domain_lock(self, domain: str) -> threading.Lock:
        with self._locks_guard:
            return self._domain_locks[domain]

    def _get_proxy_service(self):
        """懒加载代理服务客户端（与 ProductCrawler 共用同一服务），失败后永久停用"""
        if self._proxy_service_failed:
            return None
        if self._proxy_service is None:
            try:
                from qmds.modules.data_scraper.product_crawler import ProxyServiceClient
                # 平台检测关闭熔断：每个 URL 都真实请求远程代理，
                # 避免连续失败进入冷却后把真实 Shopify 站点漏判。
                self._proxy_service = ProxyServiceClient(breaker_enabled=False)
            except Exception as e:
                log.warning(f"代理服务客户端初始化失败，已停用: {e}")
                self._proxy_service_failed = True
                return None
        return self._proxy_service

    @staticmethod
    def _shopify_result_from_meta(data: dict, confidence: float) -> DetectionResult:
        """由 meta.json 内容构建 Shopify 检测结果"""
        return DetectionResult(
            platform=Platform.SHOPIFY,
            product_count=int(data.get("published_products_count", 0) or 0),
            store_name=data.get("name", ""),
            currency=data.get("currency", "USD"),
            confidence=confidence,
            raw=data,
        )

    def detect(self, url: str, url_map: dict = None) -> DetectionResult:
        """检测是否 Shopify（单通道：仅通过远程代理访问 meta.json）

        url_map 保留兼容旧签名（myshopify 回退已随多通道一并移除）。
        """
        url_map = url_map or {}
        try:
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            if not url.endswith("/"):
                url += "/"
            domain = urlparse(url).netloc.lower()
            with self._get_domain_lock(domain):
                return self._detect_locked(url)
        except Exception:
            # 未预见异常不构成平台否定证据，标记 inconclusive 交由上层复检
            return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)

    def _detect_locked(self, url: str) -> DetectionResult:
        """单通道检测：仅通过远程代理服务访问 meta.json

        判定语义见模块 docstring。核心原则：
        - 快速被拒（403/429/5xx）≠ 非 Shopify（WAF 拦的真店如
          techniquerecords.com 全程 429）→ 无法确认，绝不误杀；
        - 单站挂起超时（status=0，如政府站/论坛/博客等无 meta.json
          且响应极慢的杂站）→ 判非 Shopify，避免杂站堆积"待确认"；
        - 连续大量超时（疑似代理服务整体故障）→ 恢复"无法确认"，
          防止大面积误杀真店。
        """
        global _consecutive_timeouts
        meta_url = f"{url}meta.json"
        client = self._get_proxy_service()
        if client is None:
            log.warning(f"远程代理服务不可用，无法确认: {url}")
            return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)

        data, status = client.fetch(meta_url, timeout=DETECT_TIMEOUT_PROXY)

        # 任何"有响应"的结果都重置连续超时计数
        if status != 0:
            with _timeout_guard:
                _consecutive_timeouts = 0

        # 代理成功拿到 meta.json：有 published_products_count 指标即确认平台
        if status == 200 and isinstance(data, dict):
            count = data.get("published_products_count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                count = int(count)
                if count > SHOPIFY_MIN_PRODUCTS:
                    log.info(f"代理 meta.json 确认 Shopify: {url} (published_products_count={count})")
                    return self._shopify_result_from_meta(data, confidence=0.95)
                # meta.json 拿到了但商品数不达标：判非 Shopify（非目标店铺）
                log.info(f"meta.json 商品数不达标（{count} ≤ {SHOPIFY_MIN_PRODUCTS}），判非 Shopify: {url}")
                return DetectionResult(platform=Platform.UNKNOWN, product_count=count)
            log.info(f"meta.json 无 published_products_count 字段，判非 Shopify: {url}")
            return DetectionResult(platform=Platform.UNKNOWN)

        # 明确不存在
        if status in (404, 410):
            log.info(f"meta.json 不存在（HTTP{status}），判非 Shopify: {url}")
            return DetectionResult(platform=Platform.UNKNOWN)

        # 单站挂起超时：站本身无响应（几乎都是非 Shopify 杂站），判非 Shopify；
        # 连续超时超过阈值时疑似代理服务故障，改判无法确认保护真店
        if status == 0:
            with _timeout_guard:
                _consecutive_timeouts += 1
                if _consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    log.warning(f"连续 {_consecutive_timeouts} 个 URL 代理超时，疑似代理服务故障，改判无法确认: {url}")
                    return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)
            log.info(f"代理访问 meta.json 超时（status=0），判非 Shopify: {url}")
            return DetectionResult(platform=Platform.UNKNOWN)

        # 访问失败/被拦（403/429/5xx/-1/-2）：无法确认，不误杀真店
        log.info(f"代理访问 meta.json 失败（status={status}），无法确认: {url}")
        return DetectionResult(platform=Platform.UNKNOWN, inconclusive=True)
