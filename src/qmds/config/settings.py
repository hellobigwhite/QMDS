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

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379

    # 日志
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    # 代理文件
    proxies_file: Optional[Path] = None

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
        1. ip:port:user:pass
        2. http://user:pass@ip:port
        """
        if not self.proxies_file or not self.proxies_file.exists():
            return []
        lines = self.proxies_file.read_text(encoding="utf-8").strip().splitlines()
        result = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pw = parts
                result.append(f"http://{user}:{pw}@{ip}:{port}")
            elif line.startswith("http://") or line.startswith("https://"):
                result.append(line)
        return result

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


settings = Settings.from_env()
