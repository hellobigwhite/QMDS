"""LLM 文本模型统一配置 — 筛站/补充分类/构建菜单共享

类似 image_generator.py 中 IMAGE_MODELS 的注册表模式，
所有 LLM 文本功能从此处读取模型配置，避免分散硬编码。
"""

import re
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
#   provider:  "mimo"、"ark" 或 "agentrouter"
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
        "label": "火山方舟 DeepSeek-V4-Flash 正式版",
        "desc": "火山方舟 DeepSeek-V4-Flash 文本模型，需配置 ARK API Key",
    },
    {
        "value": "ark-ep-20260822151540",
        "provider": "ark",
        "model_id": "ep-20260822151540-9zcdd",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "label": "火山方舟 DeepSeek-V4-Pro 正式版",
        "desc": "火山方舟 DeepSeek-V4-Pro 文本模型，需配置 ARK API Key",
    },
    {
        # AgentRouter 平台（https://agentrouter.org/，OpenAI 兼容网关）。
        # 不硬编码模型名：model_id 运行时从设置解析（site_db agentrouter_model /
        # .env AGENTROUTER_MODEL），模型列表通过平台 /models 接口实时获取
        # （配置页「获取模型列表」按钮 / AI 生成网站信息卡片）。
        "value": "agentrouter",
        "provider": "agentrouter",
        "model_id": "",
        "base_url": "https://agentrouter.org/v1",
        "label": "AgentRouter 平台模型",
        "desc": "AgentRouter 平台真实模型（模型列表从平台实时获取后选择），需配置 AgentRouter API Key",
    },
]

DEFAULT_LLM_MODEL = "mimo-v2.5"

# AgentRouter（https://agentrouter.org/）平台模型的选择值（模型 ID 从设置解析）
DEFAULT_AGENTROUTER_MODEL = "agentrouter"


# =========================
# 查询辅助
# =========================

def get_llm_default_headers(model_config: dict) -> dict:
    """返回创建 OpenAI client 时需要的 default_headers

    AgentRouter（agentrouter.org，AI Coding 公益站）按 User-Agent 做客户端
    白名单：OpenAI SDK 默认 UA 会被 401 "unauthorized client detected" 拒绝，
    必须伪装为白名单内的编程工具 UA（实测 cline/1.0.0 可用）。
    其他 provider 返回空 dict（用 SDK 默认头）。
    """
    if model_config.get("provider") == "agentrouter":
        from qmds.utils.agentrouter_client import TOOL_USER_AGENT
        return {"User-Agent": TOOL_USER_AGENT}
    return {}

def get_llm_model_config(model_value: str = "", site_db=None,
                        model_id_override: str = "") -> dict:
    """根据 model_value 返回模型配置字典；未找到返回默认模型。

    Args:
        model_value: 模型唯一标识（如 "mimo-v2.5"、"agentrouter"）
                     空字符串时返回默认模型。
        site_db: 可选的 SiteDB 实例。AgentRouter 的模型 ID / API 地址
                 存放在 site_db 设置 agentrouter_model / agentrouter_base_url
                 （回退 .env AGENTROUTER_MODEL / AGENTROUTER_BASE_URL），
                 在此解析填充；不硬编码任何平台模型名。
        model_id_override: 显式模型 ID（如从平台 /models 实时列表中选择的值），
                 优先级最高。为空时按设置解析。

    Returns:
        模型配置 dict
    """
    if not model_value:
        model_value = settings.llm_model or DEFAULT_LLM_MODEL
    config = None
    for m in LLM_MODELS:
        if m["value"] == model_value:
            config = m
            break
    if config is None:
        config = next(m for m in LLM_MODELS if m["value"] == DEFAULT_LLM_MODEL)

    # AgentRouter：model_id / base_url 均运行时解析（模型名不硬编码，
    # 一律来自平台 /models 实时列表的选择或用户显式填写）
    if config.get("provider") == "agentrouter":
        model_id = str(model_id_override or "").strip()
        base_url = ""
        if site_db is not None:
            if not model_id:
                model_id = site_db.get_setting("agentrouter_model", "") or ""
            base_url = site_db.get_setting("agentrouter_base_url", "") or ""
        if not model_id:
            model_id = settings.agentrouter_model
        if not base_url:
            base_url = settings.agentrouter_base_url
        model_id = str(model_id or "").strip()
        if not model_id:
            raise ValueError(
                "AgentRouter 未选择模型：请先点击「获取模型列表」从平台真实"
                "模型中选择（或在配置页设置默认模型）后再调用")
        config = dict(config, model_id=model_id,
                      base_url=(str(base_url).strip().rstrip("/")
                                or config["base_url"]))
    return config


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

    if provider == "agentrouter":
        if site_db is not None:
            key = site_db.get_setting("agentrouter_api_key", "")
            if key:
                return key
        if settings.agentrouter_api_key:
            return settings.agentrouter_api_key
        raise RuntimeError("未配置 AgentRouter API Key（请在配置页设置，"
                           "或在 .env 配置 AGENTROUTER_API_KEY）")

    # mimo: 多 key 轮换
    return _get_next_mimo_key()


def has_llm_api_key(model_config: dict, site_db=None) -> bool:
    """检查指定模型的 API Key 是否已配置（无副作用，不推进 MiMo key 轮换）"""
    provider = model_config.get("provider", "mimo")

    if provider == "ark":
        if site_db is not None and site_db.get_setting("ark_api_key", ""):
            return True
        return bool(settings.ark_api_key)

    if provider == "agentrouter":
        if site_db is not None and site_db.get_setting("agentrouter_api_key", ""):
            return True
        return bool(settings.agentrouter_api_key)

    # mimo: key 文件有可用 key，或 settings.mimo_api_key 有默认值
    return count_mimo_keys() > 0 or bool(settings.mimo_api_key)


# =========================
# Provider 差异参数
# =========================

def get_llm_extra_body(model_config: dict) -> Optional[dict]:
    """返回 extra_body 参数

    MiMo 和 Ark（Doubao-Seed-1.x 等思考模型）均支持
    {"thinking": {"type": "disabled"}} 关闭深度思考。
    思考型模型若不关闭思考，token 会被 reasoning 耗尽导致 content 为空。
    若模型不支持该参数，chat_completion_with_fallback 会自动降级重试。

    AgentRouter 网关（Gemini/GPT/Claude 等）不支持该参数，返回 None
    直接以标准 OpenAI 参数调用。
    """
    if model_config.get("provider") == "agentrouter":
        return None
    return {"thinking": {"type": "disabled"}}


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
    config = get_llm_model_config(model_value, site_db)
    api_key = get_llm_api_key(config, site_db)
    return {
        "config": config,
        "api_key": api_key,
        "base_url": config["base_url"],
        "model_id": config["model_id"],
        "extra_body": get_llm_extra_body(config),
        "system_message": get_llm_system_message(config),
    }


# =========================
# 响应解析与调用封装
# =========================

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)


def extract_llm_text(message) -> str:
    """从 LLM 响应消息中健壮地提取纯文本

    处理思考型模型（火山方舟 Doubao-Seed / DeepSeek-R1 等）的常见输出形态：
    1. content 中含 <think>...</think> 思考块 → 剥离
    2. content 为空（token 被 reasoning 耗尽）→ 回退到
       message.reasoning_content，从中提取最后的 JSON/文本主体
    3. markdown 代码围栏 ```json ...``` → 剥离

    Returns:
        清洗后的文本；完全无内容时返回空字符串
    """
    text = (getattr(message, "content", None) or "")
    text = _THINK_RE.sub('', text).strip()

    if not text:
        # content 为空：尝试从 reasoning_content 提取最终答案
        rc = getattr(message, "reasoning_content", None) or ""
        if rc:
            log.warning("LLM content 为空，尝试从 reasoning_content 提取（思考模型 token 耗尽）")
            m = re.search(r'[\[{].*[\]}]', rc, re.DOTALL)
            if m:
                text = m.group(0)

    if not text:
        return ""

    # 剥离 markdown 围栏
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def chat_completion_with_fallback(client, *, config: dict, messages: list,
                                  temperature: float, max_completion_tokens: int,
                                  top_p: float, timeout):
    """统一的 chat.completions 调用封装

    特性：
    - 自动附加 thinking disabled 参数（避免思考模型耗尽 token）
    - 模型不支持 thinking 参数时自动降级重试（去掉 extra_body）
    """
    kwargs = dict(
        model=config["model_id"],
        messages=messages,
        temperature=temperature,
        max_completion_tokens=max_completion_tokens,
        top_p=top_p,
        timeout=timeout,
    )
    extra_body = get_llm_extra_body(config)
    if extra_body:
        try:
            return client.chat.completions.create(**kwargs, extra_body=extra_body)
        except Exception as e:
            err_lower = str(e).lower()
            if "thinking" in err_lower and ("not support" in err_lower or "invalid" in err_lower
                                            or "unsupported" in err_lower or "未知" in str(e)):
                log.warning(f"模型 {config['model_id']} 不支持 thinking 参数，降级为不带 extra_body 重试")
                return client.chat.completions.create(**kwargs)
            raise
    return client.chat.completions.create(**kwargs)
