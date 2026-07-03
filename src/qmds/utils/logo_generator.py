"""Logo批量生成工具"""

import base64
import os
import random
import time
from typing import Optional

import requests

from qmds.utils.logger import get_logger

log = get_logger("logo_generator")

# 可用字体配置
FONTS = {
    "rosmatika-regular": ("BWA45", "OTUyODZmMTcwZjJjNDI4OWFiNjEwZjJlODU4NzA2MzUudHRm"),
    "border-wall": ("OG55o", "YjRjYzFiYTY5ZjcxNDJkYzljYWU5NzE0NGFiZmRiNGMub3Rm"),
    "remalos-regular": ("aYj1m", "ZGE0MjZkMzBjNzliNDllYmE0YTI3MjcwZTUwOWQxYTgudHRm"),
    "blush-asliring-regular": ("OGP66", "MmViNTViMmRjYWZiNDg1ZmI1NDljMmExNDIxYmRhMTIub3Rm"),
    "granika": ("MAm6r", "YjhhYmI2NDM1ZGI2NDQzOGIzMTk5ZDlkYTIyNjU3NmUub3Rm"),
    "billionery-regular": ("drXjg", "NWUyOThkN2E2MGJiNDA4N2FkZDk0OTA3Yjc4Y2VlZjkub3Rm"),
    "kingsman-demo": ("1GVgg", "OTI2YjVlNjExZGJlNDMyMzk3ZTA2YzUxNjIyOGIwYmMudHRm"),
    "shifty-notes-regular": ("BWZ6d", "N2NjMWFjYTM2M2M2NGYyMjhhZTg1NjliNWM4ZTJhMWMudHRm"),
}

# 默认配置
DEFAULT_CONFIG = {
    "height": 65,
    "width": 1000,
    "fg_color": "000000",
    "bg_color": "FFFFFF",
    "size": 65,
    "tb": 1,
    "delay": 2,
}


class LogoGenerator:
    """Logo批量生成器"""

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._stop = False

    def stop(self):
        """停止生成"""
        self._stop = True

    def generate_single(self, domain: str, output_dir: str, 
                       font_name: Optional[str] = None) -> dict:
        """为单个域名生成logo

        Args:
            domain: 域名 (如 example.com)
            output_dir: 输出目录
            font_name: 指定字体名称，None则随机选择

        Returns:
            {"success": bool, "path": str, "error": str}
        """
        self._stop = False
        
        # 提取域名前缀作为文字
        text = domain.replace(".com", "").replace(".net", "").replace(".org", "")
        
        # 选择字体
        if font_name and font_name in FONTS:
            selected_font = font_name
        else:
            selected_font = random.choice(list(FONTS.keys()))
        
        font_id, code = FONTS[selected_font]
        
        # 构建请求URL
        encoded_text = base64.urlsafe_b64encode(text.encode()).decode()
        url = (
            f"https://see.fontimg.com/api/rf5/{font_id}/{code}/{encoded_text}/{selected_font}.png"
            f"?r=fs&h={self.config['height']}&w={self.config['width']}"
            f"&fg={self.config['fg_color']}&bg={self.config['bg_color']}"
            f"&tb={self.config['tb']}&s={self.config['size']}"
        )

        try:
            # 不使用代理直接请求
            response = requests.get(url, timeout=30, proxies={"http": None, "https": None})
            if response.status_code == 200:
                # 确保输出目录存在
                os.makedirs(output_dir, exist_ok=True)
                filepath = os.path.join(output_dir, "logo.png")
                
                with open(filepath, "wb") as f:
                    f.write(response.content)
                
                log.info(f"Logo生成成功: {domain} -> {filepath}")
                return {"success": True, "path": filepath, "error": ""}
            else:
                error = f"HTTP {response.status_code}"
                log.error(f"Logo生成失败: {domain} - {error}")
                return {"success": False, "path": "", "error": error}
        except Exception as e:
            error = str(e)
            log.error(f"Logo生成异常: {domain} - {error}")
            return {"success": False, "path": "", "error": error}

    def generate_batch(self, domains: list, base_dir: str, 
                      progress_callback=None) -> dict:
        """批量生成logo

        Args:
            domains: 域名列表
            base_dir: 基础目录，每个域名会在其下创建子文件夹
            progress_callback: 进度回调函数 callback(current, total, domain, result)

        Returns:
            {"total": int, "success": int, "failed": int, "errors": list}
        """
        self._stop = False
        total = len(domains)
        success = 0
        failed = 0
        errors = []

        for i, domain in enumerate(domains):
            if self._stop:
                log.info("Logo生成已停止")
                break

            # 构建输出目录
            output_dir = os.path.join(base_dir, domain)
            
            # 生成logo
            result = self.generate_single(domain, output_dir)
            
            if result["success"]:
                success += 1
            else:
                failed += 1
                errors.append(f"{domain}: {result['error']}")

            # 回调进度
            if progress_callback:
                progress_callback(i + 1, total, domain, result)

            # 延迟避免请求过快
            if i < total - 1:
                time.sleep(self.config["delay"])

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "errors": errors,
        }


def get_available_fonts() -> list:
    """获取可用字体列表"""
    return list(FONTS.keys())
