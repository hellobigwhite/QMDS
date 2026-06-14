import os
import sys
from pathlib import Path

# 添加Mango目录到Python路径
sys.path.append(str(Path(__file__).parent / 'mango'))

from mango.WP与SEO设置一体 import (
    login,
    request_with_retry,
    process_logo,
    process_icon,
    process_banner,
    process_yoast,
    process_rocket
)

class MangoIntegrator:
    def __init__(self, password, media_root):
        self._password = password
        self._media_root = media_root
    
    def upload_main_data(self, site, idcode):
        """使用Mango的功能上传主数据"""
        try:
            session = login(site, password=self._password)
            # 这里可以添加主数据上传的逻辑
            # 暂时使用现有的逻辑结构
            print(f"上传主数据到 {site}，ID: {idcode}")
            return {"upload_success": 1}
        except Exception as e:
            raise RuntimeError(f"主数据上传失败: {str(e)}")
    
    def upload_extra_data(self, site, idcode):
        """使用Mango的功能上传补充数据"""
        try:
            session = login(site, password=self._password)
            # 这里可以添加补充数据上传的逻辑
            print(f"上传补充数据到 {site}，ID: {idcode}")
            return {"upload_success": 1}
        except Exception as e:
            raise RuntimeError(f"补充数据上传失败: {str(e)}")
    
    def configure_media(self, site):
        """使用Mango的功能配置媒体"""
        try:
            site_folder = os.path.join(self._media_root, site)
            session = login(site, password=self._password)
            
            # 处理媒体文件
            success = True
            success &= process_logo(site_folder, session, request_with_retry)
            success &= process_icon(site_folder, session, request_with_retry)
            success &= process_banner(site_folder, session, request_with_retry)
            
            if not success:
                raise RuntimeError("媒体配置部分失败")
            return True
        except Exception as e:
            raise RuntimeError(f"媒体配置失败: {str(e)}")
    
    def configure_plugins(self, site):
        """使用Mango的功能配置插件"""
        try:
            session = login(site, password=self._password)
            
            # 处理插件配置
            process_yoast(site, session, request_with_retry)
            process_rocket(site, session, request_with_retry)
            
            return True
        except Exception as e:
            raise RuntimeError(f"插件配置失败: {str(e)}")