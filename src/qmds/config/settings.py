import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量（1/true/yes/on 视为 True）"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class Settings:
    # 项目路径
    project_root: Path = Path(__file__).resolve().parent.parent.parent.parent
    data_dir: Path = field(default_factory=lambda: Path("Data"))

    # HTTP 请求
    request_timeout: int = 30
    max_retries: int = 3
    retry_backoff_base: float = 2.0

    # 爬取控制
    page_sleep_min: float = 1.5
    page_sleep_max: float = 3.5
    site_cooldown_min: float = 6.0
    site_cooldown_max: float = 12.0

    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_url: str = "qmds_url_stores"
    shopify_url_db_name: str = "shopify_url"              # 外部源数据库（shopify_url 库，只读）
    comprehensive_collection: str = "comprehensive_stores"  # 综合站集合名
    shopify_url_info_collection: str = "shopify_url_info"   # shopify_url 库网站信息缓存集合

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # 日志
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    # 代理文件
    proxies_file: Optional[Path] = None

    # 本地代理池总开关（proxies.txt / HttpClient）
    # 默认关闭：抓取降级链不再经过本地代理池这一级，直接「远程代理服务 → 直连」，
    # 避免整池 429 / 欠费时逐域名刷屏日志并白等重试。
    # 需要恢复时在 .env 里设置 LOCAL_PROXY_POOL_ENABLED=1。
    local_proxy_pool_enabled: bool = False

    # 远程代理服务（平台检测 meta.json 被拦截时的复检通道 / AI 抓取首页的第1级降级）
    proxy_service_url: str = "http://66.154.112.62:8000/fetch"
    proxy_service_key: str = "change-me-please"

    # 网站分类器
    niche_threshold: float = 0.7  # 主营类目占比阈值（≥此值为专一站）

    # AI 分类（MiMo LLM）
    mimo_api_key: str = "sk-sqlv0zc1341mtj6nk6y9c6sulv4n6qbz3i4cvp0m24rwgn06"
    glm_model: str = "mimo-v2.5"
    ai_batch_size: int = 10

    # 火山方舟（Ark）— 用于 LLM 文本模型和图片生成
    ark_api_key: str = ""

    # AgentRouter（https://agentrouter.org/）— OpenAI 兼容模型网关
    agentrouter_api_key: str = ""
    agentrouter_base_url: str = "https://agentrouter.org/v1"
    agentrouter_model: str = ""

    # LLM 文本模型选择（筛站/补充分类/构建菜单共用）
    llm_model: str = "mimo-v2.5"

    def __post_init__(self):
        self.data_dir = self.project_root / self.data_dir
        if self.log_file is None:
            self.log_file = self.project_root / "logs" / "qmds.log"
        if self.proxies_file is None:
            path = self.project_root / "proxies.txt"
            if path.exists():
                self.proxies_file = path

    def load_proxies(self) -> list[str]:
        """加载本地代理池（proxies.txt），返回探测后确认可用的代理

        总开关 local_proxy_pool_enabled=False 时直接返回空列表（本地代理完全关闭）。

        开启时会对每个代理做一次轻量可用性探测（结果缓存 300 秒）：
        - 有可用代理：返回可用列表，调用方正常使用本地代理；
        - 全部不可用：返回空列表，调用方（Google 搜索 / 平台检测 / HttpClient 等）
          自动关闭本地代理，改走各自的降级链（远程代理服务 → 直连 / cloudscraper）。

        支持两种格式：
        1. http://user:pass@ip:port  （已格式化，直接使用）
        2. ip:port:user:pass         （自动转换）
        """
        if not self.local_proxy_pool_enabled:
            return []
        # 检测到本地代理不可用已被禁止（进程级）：直接返回空，不再尝试
        from qmds.utils.proxy_probe import is_local_pool_banned

        if is_local_pool_banned():
            return []
        if not self.proxies_file or not self.proxies_file.exists():
            return []
        lines = self.proxies_file.read_text(encoding="utf-8").strip().splitlines()
        result = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 已是完整 URL 格式，直接使用（必须先于 split 检查）
            if line.startswith("http://") or line.startswith("https://"):
                result.append(line)
                continue
            # ip:port:user:pass 格式，转换为 http://user:pass@ip:port
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pw = parts
                result.append(f"http://{user}:{pw}@{ip}:{port}")
        if not result:
            return []

        # 开启时先探测可用性：全部不可用则返回空列表（自动关闭本地代理）
        from qmds.utils.proxy_probe import cached_probe

        target = os.getenv("PROXY_PROBE_TARGET") or None
        kwargs = {} if not target else {"target": target}
        return cached_probe(result, **kwargs)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            shopify_url_db_name=os.getenv("SHOPIFY_URL_DB_NAME", "shopify_url"),
            comprehensive_collection=os.getenv("COMPREHENSIVE_COLLECTION", "comprehensive_stores"),
            shopify_url_info_collection=os.getenv("SHOPIFY_URL_INFO_COLLECTION", "shopify_url_info"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            mimo_api_key=os.getenv("MIMO_API_KEY", "sk-sqlv0zc1341mtj6nk6y9c6sulv4n6qbz3i4cvp0m24rwgn06"),
            glm_model=os.getenv("GLM_MODEL", "mimo-v2.5"),
            ai_batch_size=int(os.getenv("AI_BATCH_SIZE", "10")),
            ark_api_key=os.getenv("ARK_API_KEY", ""),
            agentrouter_api_key=os.getenv("AGENTROUTER_API_KEY", ""),
            agentrouter_base_url=os.getenv("AGENTROUTER_BASE_URL",
                                           "https://agentrouter.org/v1"),
            agentrouter_model=os.getenv("AGENTROUTER_MODEL", ""),
            llm_model=os.getenv("LLM_MODEL", "mimo-v2.5"),
            proxy_service_url=os.getenv("PROXY_SERVICE_URL", "http://66.154.112.62:8000/fetch"),
            proxy_service_key=os.getenv("PROXY_SERVICE_KEY", "change-me-please"),
            local_proxy_pool_enabled=_env_bool("LOCAL_PROXY_POOL_ENABLED", False),
        )


settings = Settings.from_env()
