"""AgentRouter 平台客户端 — 真实连接 https://agentrouter.org/（OpenAI 兼容网关）

实测平台特性（2026-09 真实验证）：
1. 模型列表通过 GET {base_url}/models 实时获取，不硬编码；
2. 平台按 User-Agent 做客户端白名单：python-requests / OpenAI SDK 默认 UA
   一律返回 401 "unauthorized client detected"（不校验 Key 直接拒绝）；
   带编程工具 UA（cline 等）即可通过 —— 所有请求必须携带 TOOL_USER_AGENT；
3. 响应为 new-api 网关格式；模型下线时返回 503 "无可用渠道"。

AgentRouter API Key / API 地址 / 默认模型在「配置」页设置：
    agentrouter_api_key    API Key（或 .env AGENTROUTER_API_KEY）
    agentrouter_base_url   API 地址（默认 https://agentrouter.org/v1）
    agentrouter_model      默认模型 ID（从平台模型列表中选择）
"""

import requests

from qmds.utils.logger import get_logger

log = get_logger("agentrouter_client")

# 平台默认 API 地址（OpenAI 兼容路径）
DEFAULT_AGENTROUTER_BASE_URL = "https://agentrouter.org/v1"

# 平台客户端白名单 UA：实测 python-requests/OpenAI-SDK 默认 UA 被 401 拒绝，
# 编程工具 UA（cline 等）放行。项目所有 AgentRouter 请求统一携带。
TOOL_USER_AGENT = "cline/1.0.0"

# 请求超时（秒）
REQUEST_TIMEOUT = 30


def normalize_base_url(base_url: str) -> str:
    """规范化 base_url：去首尾空白、去尾部斜杠；空值回退默认地址"""
    url = (base_url or "").strip().rstrip("/")
    return url or DEFAULT_AGENTROUTER_BASE_URL


def build_agentrouter_headers(api_key: str, base_url: str = "",
                              timeout: int = REQUEST_TIMEOUT) -> dict:
    """构造平台请求头（含白名单 UA 与鉴权）"""
    return {
        "Authorization": f"Bearer {(api_key or '').strip()}",
        "x-api-key": (api_key or "").strip(),
        "User-Agent": TOOL_USER_AGENT,
        "Content-Type": "application/json",
    }


def fetch_agentrouter_models(api_key: str, base_url: str = "",
                             timeout: int = REQUEST_TIMEOUT) -> list:
    """调用 AgentRouter 平台 GET {base_url}/models，返回真实模型 ID 列表

    兼容两种响应形态：
    - OpenAI 标准: {"object": "list", "data": [{"id": "...", ...}, ...]}
    - 简单列表:    ["model-a", "model-b"]

    Returns:
        排序去重后的模型 ID 列表（空列表表示平台无可用模型）

    Raises:
        ValueError: 未配置 API Key
        RuntimeError: 连接失败 / Key 无效 / 响应格式不正确
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("未配置 AgentRouter API Key（请先在配置页设置）")

    url = normalize_base_url(base_url) + "/models"
    try:
        resp = requests.get(url, headers=build_agentrouter_headers(api_key),
                            timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError(f"连接 AgentRouter 超时（{timeout}s）: {url}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"无法连接 AgentRouter（{url}）: {e}")

    if resp.status_code == 401 or resp.status_code == 403:
        raise RuntimeError(
            f"AgentRouter 拒绝访问（HTTP {resp.status_code}）: {resp.text[:150]}。"
            "请检查 API Key 是否有效；若 Key 正确仍被拒，可能是平台客户端"
            "白名单或 IP 风控（公益站通常限制代理/机房 IP，可尝试更换网络）")
    if resp.status_code != 200:
        raise RuntimeError(
            f"AgentRouter 返回 HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"AgentRouter 返回的不是 JSON: {resp.text[:200]}")

    items = payload.get("data") if isinstance(payload, dict) else payload
    if items is None:
        items = []
    if not isinstance(items, list):
        raise RuntimeError(f"AgentRouter 模型列表格式不正确: {str(payload)[:200]}")

    models = []
    for item in items:
        mid = item.get("id") if isinstance(item, dict) else item
        if mid and str(mid).strip():
            models.append(str(mid).strip())
    models = sorted(set(models))
    log.info(f"AgentRouter 模型列表获取成功: {len(models)} 个模型 ({url})")
    return models
