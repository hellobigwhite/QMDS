import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


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

    # 网站分类器
    niche_threshold: float = 0.7  # 主营类目占比阈值（≥此值为专一站）

    # AI 分类（MiMo LLM）
    mimo_api_key: str = "sk-sqlv0zc1341mtj6nk6y9c6sulv4n6qbz3i4cvp0m24rwgn06"
    glm_model: str = "mimo-v2.5"
    ai_batch_size: int = 10

    def __post_init__(self):
        self.data_dir = self.project_root / self.data_dir
        if self.log_file is None:
            self.log_file = self.project_root / "logs" / "qmds.log"
        if self.proxies_file is None:
            path = self.project_root / "proxies.txt"
            if path.exists():
                self.proxies_file = path

    def load_proxies(self) -> list[str]:
        """从 proxies.txt 加载代理，支持两种格式：
        1. http://user:pass@ip:port  （已格式化，直接使用）
        2. ip:port:user:pass         （自动转换）
        """
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
        return result

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
        )


settings = Settings.from_env()
