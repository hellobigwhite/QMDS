"""LLM 文本模型统一配置 — 筛站/补充分类/构建菜单共享

类似 image_generator.py 中 IMAGE_MODELS 的注册表模式，
所有 LLM 文本功能从此处读取模型配置，避免分散硬编码。
"""

import threading
from typing import Optional

from qmds.config.settings import settings
from qmds.utils.logger import get_logger

log = get_logger("llm_models")


# =========================
# 模型注册表
# =========================
# 每个模型条目定义：
#   value:     唯一标识（Web 下拉框 value / .env 配置值）
#   provider:  "mimo" 或 "ark"
#   model_id:  传给 API 的模型名
#   base_url:  API 地址
#   label:     下拉菜单显示文本
#   desc:      描述（UI 提示）

LLM_MODELS = [
    {
        "value": "mimo-v2.5",
        "provider": "mimo",
        "model_id": "mimo-v2.5",
        "base_url": "https://api.xiaomimimo.com/v1",
        "label": "MiMo v2.5",
        "desc": "小米 MiMo 文本模型",
    },
    {
        "value": "mimo-v2.5-pro",
        "provider": "mimo",
        "model_id": "mimo-v2.5-pro",
        "base_url": "https://api.xiaomimimo.com/v1",
        "label": "MiMo v2.5 Pro",
        "desc": "MiMo Pro，分类优化能力更强",
    },
    {
        "value": "ark-ep-20260822151623",
        "provider": "ark",
        "model_id": "ep-20260822151623-pqkd4",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "label": "火山方舟 EP 模型",
        "desc": "火山方舟文本模型，需配置 ARK API Key",
    },
]

DEFAULT_LLM_MODEL = "mimo-v2.5"


# =========================
# 查询辅助
# =========================

def get_llm_model_config(model_value: str = "") -> dict:
    """根据 model_value 返回模型配置字典；未找到返回默认模型。

    Args:
        model_value: 模型唯一标识（如 "mimo-v2.5"、"ark-ep-20260822151623"）
                     空字符串时返回默认模型。

    Returns:
        模型配置 dict
    """
    if not model_value:
        model_value = settings.llm_model or DEFAULT_LLM_MODEL
    for m in LLM_MODELS:
        if m["value"] == model_value:
            return m
    return next(m for m in LLM_MODELS if m["value"] == DEFAULT_LLM_MODEL)


def list_llm_models() -> list:
    """返回所有可选 LLM 模型（用于 UI 渲染）"""
    return LLM_MODELS


# =========================
# API Key 解析（按 provider 分发）
# =========================

# MiMo 多 key 轮换状态
_key_lock = threading.Lock()
_key_index = 0


def _load_mimo_keys() -> list[str]:
    """从 menu_ai_api_keys.txt 加载未注释的 key"""
    keys_file = settings.project_root / "menu_ai_api_keys.txt"
    if not keys_file.exists():
        return []
    lines = keys_file.read_text(encoding="utf-8").strip().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def count_mimo_keys() -> int:
    """返回 menu_ai_api_keys.txt 中可用的 key 数量"""
    return len(_load_mimo_keys())


def _get_next_mimo_key() -> str:
    """线程安全轮换获取下一个 MiMo API Key

    menu_ai_api_keys.txt 为空时回退到 settings.mimo_api_key（.env MIMO_API_KEY）。
    """
    global _key_index
    with _key_lock:
        keys = _load_mimo_keys()
        if not keys:
            if settings.mimo_api_key:
                return settings.mimo_api_key
            raise RuntimeError("未配置 MiMo API Key（menu_ai_api_keys.txt 为空且 MIMO_API_KEY 未设置）")
        key = keys[_key_index % len(keys)]
        _key_index += 1
    return key


def get_llm_api_key(model_config: dict, site_db=None) -> str:
    """根据 provider 解析 API Key

    Args:
        model_config: get_llm_model_config() 返回的配置 dict
        site_db: 可选的 SiteDB 实例（Web 模式下传入，读取数据库中的 ark_api_key）

    Returns:
        API Key 字符串
    """
    provider = model_config.get("provider", "mimo")

    if provider == "ark":
        if site_db is not None:
            key = site_db.get_setting("ark_api_key", "")
            if key:
                return key
        return settings.ark_api_key

    # mimo: 多 key 轮换
    return _get_next_mimo_key()


def has_llm_api_key(model_config: dict, site_db=None) -> bool:
    """检查指定模型的 API Key 是否已配置"""
    try:
        key = get_llm_api_key(model_config, site_db)
        return bool(key)
    except RuntimeError:
        return False


# =========================
# Provider 差异参数
# =========================

def get_llm_extra_body(model_config: dict) -> Optional[dict]:
    """返回 extra_body 参数（provider 专属）

    MiMo 需要 {"thinking": {"type": "disabled"}}，
    Ark 不需要此参数。
    """
    provider = model_config.get("provider", "mimo")
    if provider == "mimo":
        return {"thinking": {"type": "disabled"}}
    return None


def get_llm_system_message(model_config: dict) -> str:
    """返回 system message（provider 专属）"""
    provider = model_config.get("provider", "mimo")
    if provider == "mimo":
        return "You are MiMo, an AI assistant. Respond with valid JSON only."
    return "You are an AI assistant. Respond with valid JSON only."


def resolve_llm_model(model_value: str = "", site_db=None) -> dict:
    """一站式解析：返回包含 model_config + api_key + extra_body + system_message 的完整调用包

    Args:
        model_value: 模型唯一标识（空字符串时从 settings/默认值读取）
        site_db: 可选的 SiteDB 实例

    Returns:
        {
            "config": dict,          # 模型配置
            "api_key": str,          # API Key
            "base_url": str,         # API 地址
            "model_id": str,         # 模型 ID
            "extra_body": dict|None, # extra_body 参数
            "system_message": str,   # system message
        }
    """
    config = get_llm_model_config(model_value)
    api_key = get_llm_api_key(config, site_db)
    return {
        "config": config,
        "api_key": api_key,
        "base_url": config["base_url"],
        "model_id": config["model_id"],
        "extra_body": get_llm_extra_body(config),
        "system_message": get_llm_system_message(config),
    }
