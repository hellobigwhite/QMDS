"""批量生成图片工具（Banner + Icon + Logo + 合并）"""

import asyncio
import base64
import os
import random
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

from qmds.config import settings as _settings
from qmds.utils.logger import get_logger

log = get_logger("image_generator")

# 极速AI API Key 配置文件路径（项目根目录下的 jisuai_api_keys.txt）
JISUAI_KEYS_FILE: Path = _settings.project_root / "jisuai_api_keys.txt"


def load_jisuai_keys(keys_file: Optional[Path] = None) -> list[str]:
    """从 jisuai_api_keys.txt 加载未注释的 key（# 开头为注释，表示额度用完）。

    文件格式参考 scraperapi_keys.txt：每行一个 key，# 开头的行会被跳过。
    """
    filepath = keys_file or JISUAI_KEYS_FILE
    if not filepath.exists():
        return []
    keys = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keys.append(line)
    return keys


def comment_out_key(api_key: str, keys_file: Optional[Path] = None) -> bool:
    """将指定 key 在配置文件中注释掉（前面加 '# '）。

    这样下次运行时该 key 会被自动跳过，直接从有额度的 key 开始使用。
    返回 True 表示成功注释，False 表示未找到或文件不存在。
    """
    filepath = keys_file or JISUAI_KEYS_FILE
    if not filepath.exists():
        return False
    lines = filepath.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 跳过已注释的行
        if stripped.startswith("#"):
            continue
        # 匹配未注释的 key 行（去掉行内注释后比较）
        key_part = stripped.split("#")[0].strip()
        if key_part == api_key:
            lines[i] = f"# {line.strip()}  # 额度用完"
            changed = True
            break
    if changed:
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info(f"已将 key {api_key[:12]}... 在 {filepath.name} 中注释掉")
    return changed

# =========================
# Banner 布局样式
# =========================
BANNER_LAYOUTS = [
    "Text on the left, product image on the right.",
    "Text on the right, product image on the left.",
    "Centered text overlay on top of a full-bleed product background image.",
    "Text at the top, products arranged across the bottom.",
    "Text at the bottom, products arranged across the top.",
    "Hero product on one side, lifestyle scene background, slogan text floating over the scene.",
]

# =========================
# 字体配置
# =========================
FONTS = {
    "rosmatika-regular.png": ("BWA45", "OTUyODZmMTcwZjJjNDI4OWFiNjEwZjJlODU4NzA2MzUudHRm"),
    "border-wall.png": ("OG55o", "YjRjYzFiYTY5ZjcxNDJkYzljYWU5NzE0NGFiZmRiNGMub3Rm"),
    "remalos-regular.png": ("aYj1m", "ZGE0MjZkMzBjNzliNDllYmE0YTI3MjcwZTUwOWQxYTgudHRm"),
    "blush-asliring-regular.png": ("OGP66", "MmViNTViMmRjYWZiNDg1ZmI1NDljMmExNDIxYmRhMTIub3Rm"),
    "granika.png": ("MAm6r", "YjhhYmI2NDM1ZGI2NDQzOGIzMTk5ZDlkYTIyNjU3NmUub3Rm"),
    "billionery-regular.png": ("drXjg", "NWUyOThkN2E2MGJiNDA4N2FkZDk0OTA3Yjc4Y2VlZjkub3Rm"),
    "kingsman-demo.png": ("1GVgg", "OTI2YjVlNjExZGJlNDMyMzk3ZTA2YzUxNjIyOGIwYmMudHRm"),
    "shifty-notes-regular.png": ("BWZ6d", "N2NjMWFjYTM2M2M2NGYyMjhhZTg1NjliNWM4ZTJhMWMudHRm"),
}

ENGLISH_SAFE_FONTS = {
    "rosmatika-regular.png": FONTS["rosmatika-regular.png"],
    "remalos-regular.png": FONTS["remalos-regular.png"],
    "granika.png": FONTS["granika.png"],
    "billionery-regular.png": FONTS["billionery-regular.png"],
    "shifty-notes-regular.png": FONTS["shifty-notes-regular.png"],
}

DIGIT_SAFE_FONTS = {
    "rosmatika-regular.png": FONTS["rosmatika-regular.png"],
    "remalos-regular.png": FONTS["remalos-regular.png"],
    "granika.png": FONTS["granika.png"],
}

MIXED_SAFE_FONTS = {
    "rosmatika-regular.png": FONTS["rosmatika-regular.png"],
    "remalos-regular.png": FONTS["remalos-regular.png"],
    "granika.png": FONTS["granika.png"],
    "shifty-notes-regular.png": FONTS["shifty-notes-regular.png"],
}

# =========================
# 图片生成模型注册表
# =========================
# 每个模型条目定义：
#   provider:  "jisuai" 或 "ark"（火山方舟）
#   model_id:  传给 API 的模型名
#   label:     下拉菜单显示文本
#   desc:      描述（UI 提示）
#
# 说明：所有模型共用相同的 API 调用格式（OpenAI images.generate 接口），
# 仅切换 model_id 和 provider。尺寸统一使用配置中的 banner_size/icon_size，
# 不做模型专属尺寸映射。需不同请求格式的模型不要添加到此处。
# =========================
IMAGE_MODELS = [
    {
        "value": "gpt-image-2",
        "provider": "jisuai",
        "model_id": "gpt-image-2",
        "label": "gpt-image-2（极速AI）",
        "desc": "极速AI 文生图，OpenAI 兼容接口",
    },
    {
        "value": "doubao-seedream-4-0-250828",
        "provider": "ark",
        "model_id": "doubao-seedream-4-0-250828",
        "label": "豆包 Seedream 4.0（火山方舟）",
        "desc": "火山方舟豆包文生图 4.0，需配置 ARK API Key",
    },
]

# 默认模型值
DEFAULT_IMAGE_MODEL = "gpt-image-2"

# 火山方舟 API 常量
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def get_image_model_config(model_value: str) -> Optional[dict]:
    """根据 model_value 返回模型配置字典；未找到返回 None。"""
    for m in IMAGE_MODELS:
        if m["value"] == model_value:
            return m
    return None


def list_image_models() -> list:
    """返回所有可选模型（用于 UI 渲染）。"""
    return IMAGE_MODELS


# =========================
# 默认配置
# =========================
DEFAULT_CONFIG = {
    "banner_size": "1920x800",
    "icon_size": "1024x1024",
    "icon_resize": (512, 512),
    "logo_height": 65,
    "logo_width": 1000,
    "logo_fg_color": "000000",
    "logo_bg_color": "FFFFFF",
    "logo_size": 65,
    "logo_tb": 1,
    "logo_delay": 2,
    "logo_max_retries": 3,
    "max_retries": 5,
    "banner_concurrency": 1,
    "icon_concurrency": 1,
    "banner_max_size_kb": 300,
    "white_threshold": 245,
}


class ImageGenerator:
    """批量生成图片工具（Banner + Icon + Logo + 合并）"""

    def __init__(self, api_keys, proxy_manager=None, config: Optional[dict] = None,
                 keys_file: Optional[Path] = None,
                 model: str = DEFAULT_IMAGE_MODEL,
                 ark_api_key: Optional[str] = None):
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        self.api_keys = [k for k in api_keys if k]
        self.proxy_manager = proxy_manager
        self._failed_keys = set()
        self._key_lock = threading.Lock()
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._stop = False
        # 配置文件路径，额度用完时自动注释掉对应 key
        self._keys_file = keys_file or JISUAI_KEYS_FILE

        # 模型选择
        self.model = model or DEFAULT_IMAGE_MODEL
        self._model_cfg = get_image_model_config(self.model) or get_image_model_config(DEFAULT_IMAGE_MODEL)
        self._provider = self._model_cfg["provider"]
        self._model_id = self._model_cfg["model_id"]
        # 火山方舟 API Key（仅 provider == "ark" 时需要）
        self.ark_api_key = ark_api_key or ""

        if self._provider == "ark" and not self.ark_api_key:
            raise ValueError("使用火山方舟模型时必须提供 ark_api_key")
        if self._provider == "jisuai":
            if not self.api_keys:
                raise ValueError("api_keys 不能为空")
        elif self._provider == "ark":
            # ark 模式不依赖 jisuai key 池；保持 api_keys 可空
            if not self.api_keys:
                self.api_keys = [self.ark_api_key]

    def stop(self):
        """停止生成"""
        self._stop = True

    def _next_key(self) -> Optional[str]:
        """获取当前粘性Key（第一个未失败的Key），用完一个再换下一个。返回None表示池耗尽。"""
        with self._key_lock:
            for key in self.api_keys:
                if key not in self._failed_keys:
                    return key
            return None

    def _reset_failed_keys(self):
        """已废弃：Sticky Key模式下失败Key不再重置，额度用完的Key在整个批量任务期间永久跳过"""
        pass

    @staticmethod
    def _is_ip_ban_429(err_str: str) -> bool:
        """识别IP级封锁429（含'无效令牌'/'请等待'）"""
        return "429" in err_str and ("无效令牌" in err_str or "请等待" in err_str)

    @staticmethod
    def _has_status_code(err_str: str, codes) -> bool:
        """判断错误信息是否包含指定 HTTP 状态码（用单词边界匹配，避免模型名中的数字误判）。

        例如：codes=("500","502","503","504") 仅匹配独立的 "504"，
        不会误匹配模型名 "doubao-seedream-3-0-t2i-250415" 中的 "504"。
        """
        for code in codes:
            if re.search(rf"(?<!\d){re.escape(code)}(?!\d)", err_str):
                return True
        return False

    @staticmethod
    def _should_switch_key(err_str: str) -> bool:
        """判断错误是否应该切换Key：仅401/403认证失败切换；429/5xx由代理层处理"""
        err_lower = err_str.lower()
        if ImageGenerator._has_status_code(err_str, ("401", "403")) or "unauthorized" in err_lower or "forbidden" in err_lower:
            return True
        return False

    @staticmethod
    def _should_switch_proxy(err_str: str) -> bool:
        """判断错误是否应该切换代理：5xx/429/超时/连接错误切换；400/404不切换"""
        err_lower = err_str.lower()
        if ImageGenerator._has_status_code(err_str, ("500", "502", "503", "504")):
            return True
        if "error code: 5" in err_lower:
            return True
        if "429" in err_str or "rate limit" in err_lower:
            return True
        if "timeout" in err_lower or "apiconnectionerror" in err_lower or "connection" in err_lower:
            return True
        return False

    @staticmethod
    def _is_non_retryable_error(err_str: str) -> bool:
        """判断错误是否不可重试（请求本身错误，换代理/换key也无济于事）。

        包含：
        - 404 模型/接入点不存在
        - 400 请求参数错误
        - InvalidEndpointOrModel / NotFound
        """
        if ImageGenerator._has_status_code(err_str, ("404", "400")):
            return True
        err_lower = err_str.lower()
        if "notfound" in err_lower or "invalidendpoint" in err_lower or "invalidmodel" in err_lower:
            return True
        if "does not exist" in err_lower or "no access to it" in err_lower:
            return True
        return False

    @staticmethod
    def _is_quota_exhausted(err_str: str) -> bool:
        """判断错误是否为额度用完（403 + 额度相关关键词）"""
        if "403" not in err_str and "forbidden" not in err_str.lower():
            return False
        quota_keywords = ("额度", "余额", "quota", "exceeded", "balance", "insufficient", "credit", "usage limit")
        err_lower = err_str.lower()
        return any(kw in err_lower or kw in err_str for kw in quota_keywords)

    def _build_client(self, api_key: str):
        """构建带代理的 AsyncOpenAI 客户端，返回 (client, proxy_dict, proxy_url)

        根据 self._provider 选择 base_url：
        - jisuai: https://api.tofastcode.xyz
        - ark:    https://ark.cn-beijing.volces.com/api/v3
        """
        from openai import AsyncOpenAI
        import httpx
        proxy_dict = None
        proxy_url = None
        if self.proxy_manager:
            proxy_dict = self.proxy_manager.get_proxy()
            if proxy_dict:
                proxy_url = proxy_dict.get("http") or proxy_dict.get("https")
        base_url = ARK_BASE_URL if self._provider == "ark" else "https://api.tofastcode.xyz"
        # ark 模式下强制使用 ark_api_key，忽略传入的 api_key
        effective_key = self.ark_api_key if self._provider == "ark" else api_key
        if proxy_url:
            http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120)
            client = AsyncOpenAI(api_key=effective_key, base_url=base_url, http_client=http_client)
        else:
            client = AsyncOpenAI(api_key=effective_key, base_url=base_url)
        return client, proxy_dict, proxy_url

    # ── AI调用 ──────────────────────────────────────────────

    async def _call_jisuai(self, prompt: str, model_id: str = "gpt-image-2",
                           size: str = "1920x800") -> Optional[bytes]:
        """异步调用极速AI文生图，返回图片二进制数据或URL。
        Sticky Key模式：始终使用第一个未失败的Key，用完一个再换下一个。
        内层遍历代理池（每次请求轮换proxy）。
        - 401/403: 永久标记key失败，换下一个key（整个批量任务期间不再重试）
        - 429/5xx/超时: 换proxy（IP级封锁mark_bad_long），不换key
        - 代理耗尽: 不换key，直接报错让上层重试
        - 400: 直接raise不重试
        """
        try:
            from openai import AsyncOpenAI
        except ImportError:
            log.error("请安装 openai: pip install openai")
            return None

        total_keys = len(self.api_keys)
        max_proxy_attempts = 10  # 单个key最多尝试的代理数，防止无限循环
        tried_keys = set()
        last_error: Optional[Exception] = None

        while True:
            api_key = self._next_key()
            if api_key is None or api_key in tried_keys:
                break
            tried_keys.add(api_key)

            try:
                key_idx = self.api_keys.index(api_key) + 1
            except ValueError:
                key_idx = 0

            proxy_attempts = 0
            while proxy_attempts < max_proxy_attempts:
                if self._stop:
                    return None

                client, proxy_dict, proxy_url = self._build_client(api_key)
                proxy_label = proxy_url.split("@")[-1] if proxy_url else "直连"
                log.info(f"调用极速AI: model={model_id}, size={size}, 使用第{key_idx}/{total_keys}个key, 代理: {proxy_label}")

                try:
                    async with client:
                        response = await client.images.generate(model=model_id, prompt=prompt, size=size, n=1)
                        item = response.data[0]
                        if getattr(item, "b64_json", None):
                            log.info("极速AI返回b64_json格式")
                            return base64.b64decode(item.b64_json)
                        if getattr(item, "url", None):
                            log.info(f"极速AI返回url格式: {item.url[:50]}...")
                            return item.url
                        log.warning("极速AI未返回有效数据")
                        return None
                except Exception as e:
                    last_error = e
                    err_str = str(e)

                    # 400类请求错误：不重试直接抛出
                    if not self._should_switch_key(err_str) and not self._should_switch_proxy(err_str):
                        log.error(f"极速AI调用失败(不重试): {e}")
                        raise

                    # 401/403认证失败/额度用完：永久标记key失败，换下一个key
                    if self._should_switch_key(err_str):
                        with self._key_lock:
                            self._failed_keys.add(api_key)
                        remaining_keys = total_keys - len(self._failed_keys)
                        if self._is_quota_exhausted(err_str):
                            log.warning(f"第{key_idx}/{total_keys}个key额度用完，永久切换下一个key（剩余可用key: {remaining_keys}/{total_keys}）: {err_str}")
                            # 将额度用完的 key 在配置文件中注释掉，下次运行自动跳过
                            try:
                                comment_out_key(api_key, self._keys_file)
                            except Exception as ce:
                                log.warning(f"注释配置文件中的 key 失败（不影响本次运行）: {ce}")
                        else:
                            log.warning(f"第{key_idx}/{total_keys}个key认证失败，永久切换下一个key（剩余可用key: {remaining_keys}/{total_keys}）: {err_str}")
                        break  # 跳出proxy循环，进入下一个key

                    # 429/5xx/超时/连接错误：换proxy
                    if self._should_switch_proxy(err_str):
                        # IP级封锁429：代理长期标记不可用
                        if self._is_ip_ban_429(err_str):
                            if self.proxy_manager and proxy_dict:
                                self.proxy_manager.mark_bad_long(proxy_dict)
                            log.warning(f"IP级封锁429，代理 {proxy_label} 标记长期不可用(300s)，换代理重试: {err_str}")
                        else:
                            if self.proxy_manager and proxy_dict:
                                self.proxy_manager.mark_bad(proxy_dict)
                            log.warning(f"代理 {proxy_label} 报错，标记冷却60s，换代理重试: {err_str}")

                        proxy_attempts += 1
                        avail = self.proxy_manager.available_count if self.proxy_manager else 0
                        if self.proxy_manager and avail == 0:
                            log.error("代理池已耗尽，无可用代理")
                            raise last_error
                        continue

            # proxy循环耗尽仍失败
            if last_error and self._should_switch_key(str(last_error)):
                # key已被标记失败，继续下一个key
                continue
            elif last_error:
                # 代理耗尽导致的失败：不换key，直接报错让上层重试（避免浪费其他key额度）
                log.warning(f"第{key_idx}/{total_keys}个key的代理尝试已耗尽({proxy_attempts}次)，不切换key，等待上层重试")
                raise last_error

        if last_error:
            log.error(f"所有API Key均失败（共{total_keys}个），最后错误: {last_error}")
            raise last_error
        return None

    async def _call_ark(self, prompt: str, size: str) -> Optional[bytes]:
        """异步调用火山方舟豆包文生图，返回图片二进制数据或URL。

        火山方舟 Ark API 兼容 OpenAI Images 接口：
            POST {ARK_BASE_URL}/images/generations
            Authorization: Bearer <ark_api_key>
            body: {model, prompt, size, n, response_format}

        本方法使用 AsyncOpenAI SDK 调用（base_url 指向 ark）。
        - 单 key（self.ark_api_key），不做 sticky key 轮换
        - 仍走代理池（429/5xx/超时换代理）
        - 400 类错误直接抛出
        """
        try:
            from openai import AsyncOpenAI
        except ImportError:
            log.error("请安装 openai: pip install openai")
            return None

        max_proxy_attempts = 10
        last_error: Optional[Exception] = None

        for proxy_attempts in range(max_proxy_attempts):
            if self._stop:
                return None

            client, proxy_dict, proxy_url = self._build_client(self.ark_api_key)
            proxy_label = proxy_url.split("@")[-1] if proxy_url else "直连"
            log.info(f"调用火山方舟: model={self._model_id}, size={size}, 代理: {proxy_label}")

            try:
                async with client:
                    response = await client.images.generate(
                        model=self._model_id,
                        prompt=prompt,
                        size=size,
                        n=1,
                        response_format="b64_json",
                    )
                    item = response.data[0]
                    if getattr(item, "b64_json", None):
                        log.info("火山方舟返回b64_json格式")
                        return base64.b64decode(item.b64_json)
                    if getattr(item, "url", None):
                        log.info(f"火山方舟返回url格式: {item.url[:50]}...")
                        return item.url
                    log.warning("火山方舟未返回有效数据")
                    return None
            except Exception as e:
                last_error = e
                err_str = str(e)

                # 不可重试错误（404 模型不存在 / 400 参数错误）：直接抛出，不换代理
                if self._is_non_retryable_error(err_str):
                    log.error(f"火山方舟调用失败(不可重试): {e}")
                    raise

                # 400类请求错误：不重试直接抛出
                if not self._should_switch_proxy(err_str):
                    log.error(f"火山方舟调用失败(不重试): {e}")
                    raise

                # 429/5xx/超时：换proxy
                if self._is_ip_ban_429(err_str):
                    if self.proxy_manager and proxy_dict:
                        self.proxy_manager.mark_bad_long(proxy_dict)
                    log.warning(f"IP级封锁429，代理 {proxy_label} 标记长期不可用(300s)，换代理重试: {err_str}")
                else:
                    if self.proxy_manager and proxy_dict:
                        self.proxy_manager.mark_bad(proxy_dict)
                    log.warning(f"代理 {proxy_label} 报错，标记冷却60s，换代理重试: {err_str}")

                if self.proxy_manager and self.proxy_manager.available_count == 0:
                    log.error("代理池已耗尽，无可用代理")
                    raise last_error
                continue

        if last_error:
            log.error(f"火山方舟调用已达到最大代理重试次数({max_proxy_attempts})，最后错误: {last_error}")
            raise last_error
        return None

    async def _call_image_api(self, prompt: str, size: str) -> Optional[bytes]:
        """根据当前模型 provider 调度到对应的文生图 API。

        - provider == "jisuai": 调用 _call_jisuai（支持多 key 轮换）
        - provider == "ark":    调用 _call_ark（单 key + 代理轮换）
        """
        if self._provider == "ark":
            return await self._call_ark(prompt=prompt, size=size)
        return await self._call_jisuai(prompt=prompt, model_id=self._model_id, size=size)

    async def _interruptible_sleep(self, seconds: float):
        """可被 stop() 打断的异步 sleep。

        将长 sleep 拆分为 0.2s 的片段，每次检查 self._stop，
        一旦收到停止信号立即返回，避免任务无法及时停止。
        """
        if seconds <= 0:
            return
        interval = 0.2
        elapsed = 0.0
        while elapsed < seconds:
            if self._stop:
                return
            step = min(interval, seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step

    async def _download_image(self, result) -> Optional[Image.Image]:
        """将返回的bytes或URL统一转为PIL Image"""
        if isinstance(result, bytes):
            return Image.open(BytesIO(result))
        if isinstance(result, str) and result.startswith("http"):
            resp = await asyncio.to_thread(
                requests.get, result, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
            )
            resp.raise_for_status()
            return Image.open(BytesIO(resp.content))
        return None

    # ── Step 0: Banner ──────────────────────────────────────

    def _build_banner_prompt(self, keyword: str) -> tuple:
        """构建Banner生成的prompt"""
        layout = random.choice(BANNER_LAYOUTS)
        prompt = f"""
A professional high-quality e-commerce website banner for products related to "{keyword}".
- If the keyword contains " > ", the PRIMARY subject MUST be the LAST segment (the most specific category). Segments before " > " are parent categories provided for CONTEXT ONLY, to keep the subject accurate - do not let them override the main subject.
1. Landscape orientation, exactly 1920 pixels wide by 800 pixels tall, sharp and crisp, photorealistic, Size within 300kb, no blur.
2. Layout style: {layout}
3. Include a compelling marketing slogan text on the image, and a clear "Buy Now" button.
4. ALL text on the image (slogan, button, and any other words) MUST be in English only.
   Do NOT include any Chinese characters or any non-English text.
5. The overall style must be realistic, harmonious and visually appealing.
   No distorted objects, no visual misalignment, no unrealistic or weird artifacts.
"""
        return prompt, layout

    async def generate_banner(self, keyword: str, save_path: str) -> dict:
        """生成Banner图片

        Args:
            keyword: 关键词
            save_path: 保存路径

        Returns:
            {"success": bool, "path": str, "error": str, "size_kb": float}
        """
        if os.path.exists(save_path):
            log.info(f"[SKIP] banner 已存在: {save_path}")
            return {"success": True, "path": save_path, "error": "", "skipped": True}

        prompt, layout = self._build_banner_prompt(keyword)
        log.info(f"正在为 '{keyword}' 生成 banner...（布局: {layout}）")
        log.info(f"模型: {self._model_id} (provider={self._provider}), API Key 池大小: {len(self.api_keys)}")

        banner_size = self.config["banner_size"]

        max_retries = self.config["max_retries"]
        for attempt in range(max_retries):
            if self._stop:
                return {"success": False, "path": "", "error": "已停止"}

            log.info(f"尝试 {attempt + 1}/{max_retries} 调用文生图 API...")
            try:
                result = await self._call_image_api(
                    prompt=prompt,
                    size=banner_size
                )
                log.info(f"_call_image_api 返回: {type(result)}")
                image = await self._download_image(result)
                if image is None:
                    log.warning(f"未返回有效图片 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await self._interruptible_sleep(3 * (attempt + 1))
                    continue

                image = image.convert("RGB")
                os.makedirs(os.path.dirname(save_path), exist_ok=True)

                # 二分法压缩至指定大小
                max_kb = self.config["banner_max_size_kb"]
                best_data, best_q = None, 30
                low, high = 30, 95
                while low <= high:
                    mid = (low + high) // 2
                    buf = BytesIO()
                    image.save(buf, format="JPEG", quality=mid, optimize=True, subsampling=0)
                    kb = buf.tell() / 1024
                    if kb <= max_kb:
                        best_data, best_q = buf.getvalue(), mid
                        low = mid + 1
                    else:
                        high = mid - 1

                if best_data is None:
                    buf = BytesIO()
                    image.save(buf, format="JPEG", quality=30, optimize=True, subsampling=0)
                    best_data = buf.getvalue()

                with open(save_path, "wb") as f:
                    f.write(best_data)

                size_kb = len(best_data) / 1024
                log.info(f"[OK] banner 已保存: {save_path} ({size_kb:.1f}KB, q={best_q})")
                return {"success": True, "path": save_path, "error": "", "size_kb": size_kb}

            except Exception as e:
                err_str = str(e)
                # 不可重试错误（404 模型不存在 / 400 参数错误）：立即返回，避免无意义重试
                if self._is_non_retryable_error(err_str):
                    log.error(f"Banner 不可重试错误，立即终止: {e}")
                    return {"success": False, "path": "", "error": f"不可重试错误: {e}"}
                log.error(f"Banner API错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await self._interruptible_sleep(3 * (attempt + 1))

        return {"success": False, "path": "", "error": "生成失败，已达最大重试次数"}

    # ── Step 1: Icon ────────────────────────────────────────

    def _build_icon_prompt(self, keyword: str, domain: str = "") -> str:
        """构建Icon生成的prompt"""
        return f"""
A professional website favicon icon for "{keyword}".
- If the keyword contains " > ", the PRIMARY subject MUST be the LAST segment (the most specific category). Segments before " > " are parent categories for CONTEXT ONLY.
1. Exact square format, exactly 512 by 512 pixels, 2D flat design, clean vector-style outline.
2. Exactly ONE single centered object only, large padding, no scene.
3. White or very light solid background, minimal and clean.
4. Style: ecommerce category icon, crisp, simple, cute, 2 to 3 soft colors.
5. IMPORTANT: NO text, NO letters, NO watermark, NO domain name anywhere in the image.
   No Chinese characters.
"""

    async def generate_icon(self, keyword: str, save_path: str, domain: str = "") -> dict:
        """生成Icon图标

        Args:
            keyword: 关键词
            save_path: 保存路径
            domain: 域名（用于日志）

        Returns:
            {"success": bool, "path": str, "error": str}
        """
        if os.path.exists(save_path):
            log.info(f"[SKIP] icon 已存在: {save_path}")
            return {"success": True, "path": save_path, "error": "", "skipped": True}

        prompt = self._build_icon_prompt(keyword, domain)
        log.info(f"为 '{domain}' 生成 icon...")

        icon_size = self.config["icon_size"]

        max_retries = self.config["max_retries"]
        for attempt in range(max_retries):
            if self._stop:
                return {"success": False, "path": "", "error": "已停止"}

            await self._interruptible_sleep(1.5)
            try:
                result = await self._call_image_api(
                    prompt=prompt,
                    size=icon_size
                )
                image = await self._download_image(result)
                if image is None:
                    log.warning(f"未返回有效图片 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await self._interruptible_sleep(3 * (attempt + 1))
                    continue

                # 转RGBA并调整大小
                image = image.convert("RGBA")
                resize_to = self.config["icon_resize"]
                image = image.resize(resize_to, Image.LANCZOS)

                # 白色背景变透明
                threshold = self.config["white_threshold"]
                gray = image.convert("L")
                new_a = Image.eval(gray, lambda x: 0 if x > threshold else 255)
                image.putalpha(new_a)

                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                image.save(save_path, format="PNG")

                log.info(f"[OK] icon 已保存: {save_path}")
                return {"success": True, "path": save_path, "error": ""}

            except Exception as e:
                err_str = str(e)
                # 不可重试错误（404 模型不存在 / 400 参数错误）：立即返回
                if self._is_non_retryable_error(err_str):
                    log.error(f"Icon 不可重试错误，立即终止: {e}")
                    return {"success": False, "path": "", "error": f"不可重试错误: {e}"}
                log.error(f"Icon API错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await self._interruptible_sleep(2 * (attempt + 1))

        return {"success": False, "path": "", "error": "生成失败，已达最大重试次数"}

    # ── Step 2: 强制重命名 ──────────────────────────────────

    def force_rename_icon(self, folder_path: str) -> dict:
        """强制重命名为icon.png（兜底处理）

        Args:
            folder_path: 文件夹路径

        Returns:
            {"success": bool, "renamed": str, "error": str}
        """
        icon_path = os.path.join(folder_path, "icon.png")
        if os.path.exists(icon_path):
            log.info(f"[->] 已存在 icon.png，跳过")
            return {"success": True, "renamed": "", "error": ""}

        try:
            for filename in os.listdir(folder_path):
                file_path = os.path.join(folder_path, filename)
                if not os.path.isfile(file_path):
                    continue
                if not filename.lower().endswith(".png"):
                    continue
                if filename.lower() in ["icon.png", "banner.png", "logo.png"]:
                    continue
                try:
                    os.rename(file_path, icon_path)
                    log.info(f"[OK] 强制重命名成功：{filename} → icon.png")
                    return {"success": True, "renamed": filename, "error": ""}
                except Exception as e:
                    log.warning(f"重命名失败：{e}")
                    return {"success": False, "renamed": "", "error": str(e)}
        except Exception as e:
            log.error(f"处理 icon 失败：{e}")
            return {"success": False, "renamed": "", "error": str(e)}

        return {"success": False, "renamed": "", "error": "未找到可重命名的图片"}

    # ── Step 3: Logo文字 ────────────────────────────────────

    def _is_pure_english(self, text: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]+", text))

    def _is_pure_digit(self, text: str) -> bool:
        return bool(re.fullmatch(r"\d+", text))

    def _is_english_digit_mixed(self, text: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9]+", text)) and not self._is_pure_english(text) and not self._is_pure_digit(text)

    def _choose_font(self, text: str, used_font_names: list) -> tuple:
        """根据文本类型选择字体

        Returns:
            (font_name, (font_id, code), pool_name)
        """
        if self._is_pure_digit(text):
            font_pool = DIGIT_SAFE_FONTS
            pool_name = "数字安全字体池"
        elif self._is_pure_english(text):
            font_pool = ENGLISH_SAFE_FONTS
            pool_name = "英文安全字体池"
        elif self._is_english_digit_mixed(text):
            font_pool = MIXED_SAFE_FONTS
            pool_name = "英文数字混合安全字体池"
        else:
            font_pool = ENGLISH_SAFE_FONTS
            pool_name = "默认安全字体池"

        available = [(k, v) for k, v in font_pool.items() if k not in used_font_names]
        if not available:
            available = list(font_pool.items())

        font_name, font_value = random.choice(available)
        return font_name, font_value, pool_name

    def _delete_bad_file(self, filepath: str):
        """删除异常文件"""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                log.info(f"[CLEAN] 已删除异常文件：{filepath}")
        except Exception as e:
            log.warning(f"删除异常文件失败：{e}")

    def generate_logo(self, domain: str, output_dir: str) -> dict:
        """生成Logo文字

        Args:
            domain: 域名
            output_dir: 输出目录

        Returns:
            {"success": bool, "path": str, "error": str, "font": str}
        """
        text = domain.replace(".com", "").replace(".net", "").replace(".org", "")
        filepath = os.path.join(output_dir, "logo.png")

        if os.path.exists(filepath):
            log.info(f"Domain: {domain} → [SKIP] 已存在 logo.png，跳过")
            return {"success": True, "path": filepath, "error": "", "font": "", "skipped": True}

        success = False
        used_font_names = []
        max_retries = self.config["logo_max_retries"]
        delay = self.config["logo_delay"]

        for attempt in range(max_retries):
            if self._stop:
                return {"success": False, "path": "", "error": "已停止", "font": ""}

            font_name, (font_id, code), pool_name = self._choose_font(text, used_font_names)
            used_font_names.append(font_name)

            encoded_text = base64.urlsafe_b64encode(text.encode()).decode()
            log.info(f"Domain: {domain} → Text: {text} (尝试 {attempt + 1}/{max_retries})")
            log.info(f"  [FONT] 使用字体：{font_name} | 来源：{pool_name}")

            url = (
                f"https://see.fontimg.com/api/rf5/{font_id}/{code}/{encoded_text}/{font_name}"
                f"?r=fs&h={self.config['logo_height']}&w={self.config['logo_width']}"
                f"&fg={self.config['logo_fg_color']}&bg={self.config['logo_bg_color']}"
                f"&tb={self.config['logo_tb']}&s={self.config['logo_size']}"
            )

            try:
                response = requests.get(url, timeout=20, proxies={"http": None, "https": None})
                if response.status_code == 200:
                    content_length = len(response.content)
                    if content_length < 500:
                        log.warning(f"  [WARN] 响应内容过小 ({content_length} 字节)，重试...")
                        self._delete_bad_file(filepath)
                        time.sleep(delay)
                        continue

                    os.makedirs(output_dir, exist_ok=True)
                    with open(filepath, "wb") as f:
                        f.write(response.content)

                    if os.path.exists(filepath):
                        saved_size = os.path.getsize(filepath)
                        if saved_size > 500:
                            log.info(f"  [OK] 保存成功：{filepath} ({saved_size} 字节)")
                            return {"success": True, "path": filepath, "error": "", "font": font_name}
                        else:
                            log.warning(f"  [FAIL] 文件大小异常 ({saved_size} 字节)，重试...")
                            self._delete_bad_file(filepath)
                    else:
                        log.warning("  [FAIL] 文件未成功写入，重试...")
                        self._delete_bad_file(filepath)
                else:
                    log.warning(f"  [FAIL] 状态码: {response.status_code}，重试...")
                    self._delete_bad_file(filepath)
            except Exception as e:
                log.error(f"  [ERROR] 异常：{e}，重试...")
                self._delete_bad_file(filepath)

            time.sleep(delay)

        return {"success": False, "path": "", "error": f"{domain} 在 {max_retries} 次尝试后仍然失败", "font": ""}

    # ── Step 4: 合并 ────────────────────────────────────────

    def _trim_image(self, img: Image.Image) -> Image.Image:
        """裁剪透明或白色边距"""
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            rgba = rgba.crop(bbox)
            alpha = rgba.getchannel("A")
        r, g, b, a = rgba.split()
        gray = Image.merge("RGB", (r, g, b)).convert("L")
        non_white = Image.eval(gray, lambda px: 255 if px < 248 else 0)
        mask = Image.composite(non_white, a, a)
        bbox = mask.getbbox()
        if bbox:
            rgba = rgba.crop(bbox)
        return rgba

    def combine_icon_logo(self, domain: str, folder_path: str) -> dict:
        """合并icon（左）+ logo文字（右）

        Args:
            domain: 域名
            folder_path: 文件夹路径

        Returns:
            {"success": bool, "path": str, "error": str}
        """
        marker_name = ".logo_combined"
        marker_path = os.path.join(folder_path, marker_name)
        icon_path = os.path.join(folder_path, "icon.png")
        logo_path = os.path.join(folder_path, "logo.png")

        if os.path.exists(marker_path):
            log.info(f"[SKIP] {domain}：已合并过")
            return {"success": True, "path": logo_path, "error": "", "skipped": True}

        if not os.path.exists(icon_path):
            return {"success": False, "path": "", "error": "缺少 icon.png"}
        if not os.path.exists(logo_path):
            return {"success": False, "path": "", "error": "缺少 logo.png"}

        try:
            icon = Image.open(icon_path).convert("RGBA")
            logo = Image.open(logo_path).convert("RGBA")

            icon = self._trim_image(icon)
            logo = self._trim_image(logo)

            # 调整icon大小与logo匹配
            icon_height = logo.height
            icon_width = max(1, round(icon.width * icon_height / icon.height))
            icon = icon.resize((icon_width, icon_height), Image.LANCZOS)

            # 创建画布
            gap = 10
            padding = 8
            canvas_w = padding * 2 + icon.width + gap + logo.width
            canvas_h = padding * 2 + logo.height
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 0))

            # 绘制
            icon_x = padding
            logo_x = padding + icon.width + gap
            icon_y = padding + (logo.height - icon.height) // 2
            logo_y = padding

            canvas.alpha_composite(icon, (icon_x, icon_y))
            canvas.alpha_composite(logo, (logo_x, logo_y))
            canvas.save(logo_path)

            # 写入标记文件
            with open(marker_path, "w") as f:
                f.write("")

            log.info(f"[OK] {domain}：合并完成（icon 左 + 文字右）")
            return {"success": True, "path": logo_path, "error": ""}

        except Exception as e:
            log.error(f"合并失败: {domain} - {e}")
            return {"success": False, "path": "", "error": str(e)}

    # ── 批量处理 ────────────────────────────────────────────

    async def generate_batch(self, items: list, base_dir: str,
                             generate_banner: bool = True,
                             generate_icon: bool = True,
                             generate_logo: bool = True,
                             do_combine: bool = True,
                             progress_callback=None) -> dict:
        """批量生成图片

        Args:
            items: [(domain, keyword), ...] 列表
            base_dir: 基础目录
            generate_banner: 是否生成Banner
            generate_icon: 是否生成Icon
            generate_logo: 是否生成Logo文字
            do_combine: 是否合并icon+logo
            progress_callback: 进度回调 callback(step, current, total, domain, result)

        Returns:
            {"total": int, "banner": {"success": int, "failed": int},
             "icon": {"success": int, "failed": int},
             "logo": {"success": int, "failed": int},
             "combine": {"success": int, "failed": int},
             "errors": list}
        """
        self._stop = False
        total = len(items)
        errors = []

        stats = {
            "total": total,
            "banner": {"success": 0, "failed": 0},
            "icon": {"success": 0, "failed": 0},
            "logo": {"success": 0, "failed": 0},
            "combine": {"success": 0, "failed": 0},
            "errors": [],
        }

        if total == 0:
            return stats

        domains = [d for d, _ in items]
        keyword_map = dict(items)

        def _progress(step, current, domain, result):
            if progress_callback:
                progress_callback(step, current, total, domain, result)

        # ── Step 0: Banner ──
        if generate_banner and not self._stop:
            log.info("=" * 50)
            log.info("Step 0: AI 生成 banner")
            log.info("=" * 50)

            semaphore = asyncio.Semaphore(self.config["banner_concurrency"])

            async def banner_worker(idx, domain):
                async with semaphore:
                    if self._stop:
                        return
                    keyword = keyword_map[domain]
                    save_path = os.path.join(base_dir, domain, "banner.jpg")
                    result = await self.generate_banner(keyword, save_path)
                    if result["success"]:
                        stats["banner"]["success"] += 1
                    else:
                        stats["banner"]["failed"] += 1
                        errors.append(f"{domain} banner: {result['error']}")
                    _progress("banner", idx + 1, domain, result)

            tasks = [banner_worker(i, d) for i, d in enumerate(domains)]
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info(f"[Banner] 完成：{stats['banner']['success']}/{total}")

        # ── Step 1: Icon ──
        if generate_icon and not self._stop:
            log.info("=" * 50)
            log.info("Step 1: AI 生成 icon")
            log.info("=" * 50)

            semaphore = asyncio.Semaphore(self.config["icon_concurrency"])

            async def icon_worker(idx, domain):
                async with semaphore:
                    if self._stop:
                        return
                    keyword = keyword_map[domain]
                    save_path = os.path.join(base_dir, domain, "icon.png")
                    result = await self.generate_icon(keyword, save_path, domain=domain)
                    if result["success"]:
                        stats["icon"]["success"] += 1
                    else:
                        stats["icon"]["failed"] += 1
                        errors.append(f"{domain} icon: {result['error']}")
                    _progress("icon", idx + 1, domain, result)

            tasks = [icon_worker(i, d) for i, d in enumerate(domains)]
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info(f"[Icon] 完成：{stats['icon']['success']}/{total}")

        # ── Step 2: 强制重命名 ──
        if generate_icon and not self._stop:
            log.info("=" * 50)
            log.info("Step 2: 强制重命名 icon")
            log.info("=" * 50)

            for i, domain in enumerate(domains):
                if self._stop:
                    break
                folder_path = os.path.join(base_dir, domain)
                if os.path.isdir(folder_path):
                    self.force_rename_icon(folder_path)
                _progress("rename", i + 1, domain, {"success": True})

        # ── Step 3: Logo文字 ──
        if generate_logo and not self._stop:
            log.info("=" * 50)
            log.info("Step 3: 生成 logo 文字")
            log.info("=" * 50)

            for i, domain in enumerate(domains):
                if self._stop:
                    break
                output_dir = os.path.join(base_dir, domain)
                result = self.generate_logo(domain, output_dir)
                if result["success"]:
                    stats["logo"]["success"] += 1
                else:
                    stats["logo"]["failed"] += 1
                    errors.append(f"{domain} logo: {result['error']}")
                _progress("logo", i + 1, domain, result)
                await self._interruptible_sleep(self.config["logo_delay"])

            log.info(f"[Logo] 完成：{stats['logo']['success']}/{total}")

        # ── Step 4: 合并 ──
        if do_combine and not self._stop:
            log.info("=" * 50)
            log.info("Step 4: 合并 icon + logo")
            log.info("=" * 50)

            for i, domain in enumerate(domains):
                if self._stop:
                    break
                folder_path = os.path.join(base_dir, domain)
                result = self.combine_icon_logo(domain, folder_path)
                if result["success"]:
                    stats["combine"]["success"] += 1
                else:
                    stats["combine"]["failed"] += 1
                    if result["error"]:
                        errors.append(f"{domain} combine: {result['error']}")
                _progress("combine", i + 1, domain, result)

            log.info(f"[Combine] 完成：{stats['combine']['success']}/{total}")

        stats["errors"] = errors
        return stats


def get_available_fonts() -> list:
    """获取可用字体列表"""
    return list(FONTS.keys())
