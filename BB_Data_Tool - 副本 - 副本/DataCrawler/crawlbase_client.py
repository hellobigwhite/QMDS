import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests

DEFAULT_CONFIG = {
    "enabled": False,
    "token": "",
    "js_token": "",
    "use_js": False,
    "timeout": 120,
    "max_retries": 3,
    "retry_delay": 5,
    "ajax_wait": 0,
    "page_wait": 0,
    "country": "",
}


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def get_default_config_path() -> Path:
    return get_project_root() / "配置" / "Crawlbase配置.json"


def generate_default_config(config_path: Optional[str] = None) -> Path:
    path = Path(config_path) if config_path else get_default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=4), encoding="utf-8")
    return path


def load_crawlbase_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    path = Path(config_path) if config_path else get_default_config_path()
    if not path.exists():
        generate_default_config(str(path))

    config = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update(loaded)
    except Exception:
        pass

    env_enabled = os.getenv("CRAWLBASE_ENABLED")
    if env_enabled is not None:
        config["enabled"] = env_enabled.strip().lower() in {"1", "true", "yes", "on"}

    env_token = os.getenv("CRAWLBASE_TOKEN")
    if env_token:
        config["token"] = env_token.strip()

    env_js_token = os.getenv("CRAWLBASE_JS_TOKEN")
    if env_js_token:
        config["js_token"] = env_js_token.strip()

    return config


class CrawlbaseClient:
    api_url = "https://api.crawlbase.com/"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                )
            }
        )

    @classmethod
    def from_default_config(cls, config_path: Optional[str] = None) -> "CrawlbaseClient":
        return cls(load_crawlbase_config(config_path))

    @property
    def enabled(self) -> bool:
        if not self.config.get("enabled", False):
            return False
        return bool(self._get_active_token())

    @property
    def timeout(self) -> int:
        return int(self.config.get("timeout", 120) or 120)

    @property
    def max_retries(self) -> int:
        return int(self.config.get("max_retries", 3) or 3)

    @property
    def retry_delay(self) -> int:
        return int(self.config.get("retry_delay", 5) or 5)

    def _get_active_token(self) -> str:
        use_js = bool(self.config.get("use_js", False))
        if use_js and self.config.get("js_token"):
            return str(self.config.get("js_token", "")).strip()
        return str(self.config.get("token", "")).strip()

    def _build_params(self, target_url: str) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "token": self._get_active_token(),
            "url": target_url,
        }
        ajax_wait = int(self.config.get("ajax_wait", 0) or 0)
        page_wait = int(self.config.get("page_wait", 0) or 0)
        country = str(self.config.get("country", "") or "").strip()

        if ajax_wait > 0:
            params["ajax_wait"] = ajax_wait
        if page_wait > 0:
            params["page_wait"] = page_wait
        if country:
            params["country"] = country
        return params

    def get_json(self, target_url: str) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Crawlbase 未启用或未配置 token")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    self.api_url,
                    params=self._build_params(target_url),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                import time
                time.sleep(self.retry_delay)

        if last_error is None:
            raise RuntimeError("Crawlbase 请求失败")
        raise last_error