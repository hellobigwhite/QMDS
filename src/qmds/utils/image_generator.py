"""批量生成图片工具（Banner + Icon + Logo + 合并）"""

import asyncio
import base64
import os
import random
import re
import time
from io import BytesIO
from typing import Optional

import requests
from PIL import Image

from qmds.utils.logger import get_logger

log = get_logger("image_generator")

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
    "banner_concurrency": 5,
    "icon_concurrency": 2,
    "banner_max_size_kb": 300,
    "white_threshold": 245,
}


class ImageGenerator:
    """批量生成图片工具（Banner + Icon + Logo + 合并）"""

    def __init__(self, api_key: str, icon_api_key: Optional[str] = None, config: Optional[dict] = None):
        self.api_key = api_key
        self.icon_api_key = icon_api_key or api_key
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._stop = False

    def stop(self):
        """停止生成"""
        self._stop = True

    # ── AI调用 ──────────────────────────────────────────────

    async def _call_jisuai(self, prompt: str, model_id: str = "gpt-image-2",
                           size: str = "1920x800", api_key: Optional[str] = None) -> Optional[bytes]:
        """异步调用极速AI文生图，返回图片二进制数据或URL"""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            log.error("请安装 openai: pip install openai")
            return None

        api_key_to_use = api_key or self.api_key
        log.info(f"调用极速AI: model={model_id}, size={size}, api_key长度={len(api_key_to_use) if api_key_to_use else 0}")
        
        try:
            client = AsyncOpenAI(api_key=api_key_to_use, base_url="https://api.jisuai.top/v1")
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
            log.error(f"极速AI调用失败: {e}")
            raise

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
        log.info(f"API Key 长度: {len(self.api_key) if self.api_key else 0}")

        max_retries = self.config["max_retries"]
        for attempt in range(max_retries):
            if self._stop:
                return {"success": False, "path": "", "error": "已停止"}

            log.info(f"尝试 {attempt + 1}/{max_retries} 调用极速AI...")
            try:
                result = await self._call_jisuai(
                    prompt=prompt,
                    model_id="gpt-image-2",
                    size=self.config["banner_size"]
                )
                log.info(f"_call_jisuai 返回: {type(result)}")
                image = await self._download_image(result)
                if image is None:
                    log.warning(f"未返回有效图片 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3 * (attempt + 1))
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
                log.error(f"Banner API错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3 * (attempt + 1))

        return {"success": False, "path": "", "error": "生成失败，已达最大重试次数"}

    # ── Step 1: Icon ────────────────────────────────────────

    def _build_icon_prompt(self, keyword: str, domain: str = "") -> str:
        """构建Icon生成的prompt"""
        return f"""
A professional website favicon icon for "{keyword}".
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

        max_retries = self.config["max_retries"]
        for attempt in range(max_retries):
            if self._stop:
                return {"success": False, "path": "", "error": "已停止"}

            await asyncio.sleep(1.5)
            try:
                result = await self._call_jisuai(
                    prompt=prompt,
                    model_id="gpt-image-2",
                    size=self.config["icon_size"],
                    api_key=self.icon_api_key
                )
                image = await self._download_image(result)
                if image is None:
                    log.warning(f"未返回有效图片 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3 * (attempt + 1))
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
                log.error(f"Icon API错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))

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
                time.sleep(self.config["logo_delay"])

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
