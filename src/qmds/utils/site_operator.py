"""站点操作工具 - WordPress站点登录、数据上传、配置等
基于YSQD的完整实现移植"""

import json
import os
import re
import time
import warnings
import ssl
from typing import Optional
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from bs4 import BeautifulSoup
from PIL import Image

from qmds.utils.logger import get_logger

log = get_logger("site_operator")

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# 默认密码
DEFAULT_PASSWORD = "f!XsS$J2WneOkMyUgQ"

# 图标文件名
ICON_NAMES = ["icon.png", "head.png", "favicon.png"]

# Banner文件名
WP_BANNER_NAMES = ["banner.jpg", "banner.webp", "bannerstore.jpg", "banner-scaled.jpg"]


class SSLAdapter(HTTPAdapter):
    """自定义SSL适配器，禁用证书验证"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def request_with_retry(session, method, url, retries=3, delay=5, verify_ssl=False, **kwargs):
    """带重试机制的HTTP请求函数（兼容302）"""
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=120, verify=verify_ssl, **kwargs)
            if resp is not None and resp.status_code in (200, 201, 302):
                return resp
            else:
                log.warning(f"状态码 {getattr(resp, 'status_code', None)} 第 {i + 1}/{retries} 次重试: {url}")
        except requests.exceptions.RequestException as e:
            log.warning(f"请求异常: {e}，{method} {url}，第 {i + 1}/{retries} 次重试")
        time.sleep(delay)
    return None


class SiteOperator:
    """WordPress站点操作器"""

    def __init__(self):
        self._session: Optional[requests.Session] = None
        self._login_cookies = {}
        self._settings: dict = {}

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            })
            self._session.verify = False
            self._session.mount("https://", SSLAdapter())
        return self._session

    def _reset_session(self):
        try:
            self._session.close()
        except Exception:
            pass
        self._session = None

    def _parse_json_response(self, text):
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"(\{.*\})", raw, re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def _has_warning(self, text):
        raw = str(text or "")
        return "Warning" in raw or "Fatal error" in raw

    def _parse_progress(self, message):
        pattern = r"成功[:：]?(\d+)失败:(\d+)-重复(\d+)-名牌(\d+)已上传-?(\d+)执行时间"
        match = re.search(pattern, str(message or ""))
        if not match:
            return None
        return {
            "success": int(match.group(1)),
            "failure": int(match.group(2)),
            "repeat": int(match.group(3)),
            "brand": int(match.group(4)),
            "cs": int(match.group(5)),
        }

    def login(self, domain: str, password: str = DEFAULT_PASSWORD) -> dict:
        """登录WordPress站点

        Args:
            domain: 域名 (如 example.com)
            password: 登录密码

        Returns:
            {"success": bool, "message": str, "session": Session}
        """
        self._reset_session()

        domain = str(domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
        if domain.startswith("www."):
            domain = domain[4:]
        site_url = f"https://www.{domain}"
        login_url = f"{site_url}/bbwllogin/"
        user = domain.split(".")[0]

        login_data = {
            "log": f"Ad{user}min",
            "pwd": password,
            "wp-submit": "Log In",
            "redirect_to": f"{site_url}/wp-admin/",
            "testcookie": "1",
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = request_with_retry(self.session, "POST", login_url, data=login_data, allow_redirects=True)

                if any("wordpress_logged_in" in c.name for c in self.session.cookies):
                    for cookie in self.session.cookies:
                        self._login_cookies[cookie.name] = cookie.value
                    log.info(f"[{domain}] 登录成功（cookie验证）")
                    return {"success": True, "message": "登录成功"}

                admin_check = request_with_retry(self.session, "GET", f"{site_url}/wp-admin/")
                if admin_check is not None and admin_check.status_code == 200:
                    soup = BeautifulSoup(admin_check.text, "html.parser")
                    body = soup.find("body")
                    body_classes = " ".join(body.get("class", []) or []) if body else ""
                    if "wp-admin" in body_classes or "wpbody-content" in body_classes:
                        log.info(f"[{domain}] 登录成功（后台验证）")
                        return {"success": True, "message": "登录成功"}

                log.warning(f"[{domain}] 登录失败（尝试 {attempt + 1}/{max_retries}）")
                time.sleep(2)

            except Exception as e:
                log.warning(f"[{domain}] 登录异常 (尝试 {attempt + 1}/{max_retries}): {e}")
                time.sleep(2)

        return {"success": False, "message": "登录失败，已重试多次"}

    def _get_upload_nonce(self, site: str) -> Optional[str]:
        """获取上传nonce"""
        url = f"https://www.{site}/wp-admin/media-new.php"
        resp = request_with_retry(self.session, "GET", url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        input_nonce = soup.find("input", {"id": "_wpnonce"})
        if input_nonce:
            return input_nonce.get("value")

        for script in soup.find_all("script"):
            if script.string and "_wpnonce" in script.string:
                m = re.search(r'_wpnonce[\'"]?\s*:\s*[\'"]([a-zA-Z0-9]+)', script.string)
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _strip_bom(text: str) -> str:
        if text and text.startswith('\ufeff'):
            return text[1:]
        return text

    def _query_existing_media(self, site: str, filename: str) -> tuple:
        """查询现有的媒体文件

        Returns:
            (media_id, edit_nonce, delete_nonce) or (None, None, None)
        """
        ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
        data = {
            "action": "query-attachments",
            "post_id": 0,
            "query[post_mime_type]": "image",
            "query[orderby]": "date",
            "query[s]": filename,
            "query[order]": "DESC",
            "query[posts_per_page]": 80,
            "query[paged]": 1
        }
        resp = request_with_retry(self.session, "POST", ajax_url, data=data)
        if not resp:
            return None, None, None

        try:
            text = self._strip_bom(resp.text)
            js = json.loads(text)
            if js and js.get("data"):
                for media in js["data"]:
                    if media.get("filename") == filename or filename in media.get("url", ""):
                        return (media.get("id"),
                                media.get("nonces", {}).get("edit"),
                                media.get("nonces", {}).get("delete"))
        except Exception as e:
            log.warning(f"[{site}] 查询媒体JSON解析出错: {e}")
        return None, None, None

    def _upload_media_file(self, site: str, file_path: str, filename: str, upload_nonce: str,
                           mime: str = "image/png") -> tuple:
        """上传媒体文件

        Returns:
            (media_id, url_or_edit_nonce) or (None, None)
        """
        upload_url = f"https://www.{site}/wp-admin/async-upload.php"
        with open(file_path, "rb") as f:
            files = {"async-upload": (filename, f, mime)}
            data = {
                "action": "upload-attachment",
                "_wpnonce": upload_nonce,
                "_wp_http_referer": "/wp-admin/media-new.php",
                "name": filename
            }
            headers = {
                "Accept": "*/*",
                "Origin": f"https://www.{site}",
                "Referer": f"https://www.{site}/wp-admin/media-new.php",
                "User-Agent": "Mozilla/5.0"
            }
            resp = self.session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)

        if resp is None:
            return None, None
        try:
            text = self._strip_bom(resp.text)
            js = json.loads(text)
            if js.get("success") and js.get("data", {}).get("id"):
                return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
        except Exception as e:
            log.warning(f"[{site}] 上传媒体JSON解析出错: {e}")
        return None, None

    def _delete_media(self, site: str, media_id: int, delete_nonce: str) -> bool:
        """删除媒体文件"""
        ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
        data = {"action": "delete-post", "id": media_id, "_wpnonce": delete_nonce}
        resp = request_with_retry(self.session, "POST", ajax_url, data=data)
        return resp is not None and resp.status_code == 200

    def _crop_icon(self, site: str, media_id: int, crop_nonce: str) -> Optional[int]:
        """裁剪图标为512x512"""
        ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
        crop_data = {
            "_wpnonce": crop_nonce,
            "id": media_id,
            "context": "site-icon",
            "cropDetails[x1]": 0,
            "cropDetails[y1]": 0,
            "cropDetails[x2]": "full",
            "cropDetails[y2]": "full",
            "cropDetails[width]": "full",
            "cropDetails[height]": "full",
            "cropDetails[dst_width]": 512,
            "cropDetails[dst_height]": 512,
            "action": "crop-image",
        }
        resp = request_with_retry(self.session, "POST", ajax_url, data=crop_data)
        if resp is None:
            return None
        try:
            text = self._strip_bom(resp.text)
            js = json.loads(text)
            if js.get("success") and js.get("data", {}).get("id"):
                return js["data"]["id"]
        except Exception:
            pass
        return None

    def _convert_to_jpg(self, input_path: str, target_path: str) -> str:
        img = Image.open(input_path).convert("RGB")
        img.save(target_path, format="JPEG", quality=95)
        return target_path

    def _convert_to_webp(self, input_path: str, target_path: str) -> str:
        img = Image.open(input_path).convert("RGB")
        img.save(target_path, format="WEBP", quality=95)
        return target_path

    def _find_local_file(self, folder: str, filename: str) -> Optional[str]:
        """在文件夹中查找文件"""
        for root, dirs, files in os.walk(folder):
            if filename in files:
                return os.path.join(root, filename)
        return None

    def _save_wp_settings(self, site: str, site_icon_id: int = None,
                          date_format: str = None, time_format: str = None,
                          week_starts_on: int = None) -> bool:
        """保存WordPress设置"""
        options_url = f"https://www.{site}/wp-admin/options-general.php"
        resp = request_with_retry(self.session, "GET", options_url)
        if not resp:
            return False

        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", {"action": "options.php"})
        if not form:
            return False

        form_data = {}
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if not name:
                continue
            t = input_tag.get("type")
            if name == "whl_page":
                form_data[name] = "bbwllogin"
            elif t == "radio" and input_tag.has_attr("checked"):
                form_data[name] = input_tag.get("value")
            else:
                form_data[name] = input_tag.get("value", "")

        for select_tag in form.find_all("select"):
            name = select_tag.get("name")
            if not name:
                continue
            selected_option = select_tag.find("option", selected=True)
            form_data[name] = selected_option.get("value") if selected_option else ""

        for textarea_tag in form.find_all("textarea"):
            name = textarea_tag.get("name")
            if name:
                form_data[name] = textarea_tag.text

        if site_icon_id:
            form_data["site_icon"] = site_icon_id
        if date_format:
            form_data["date_format"] = date_format
        if time_format:
            form_data["time_format"] = time_format
        if week_starts_on is not None:
            form_data["start_of_week"] = str(week_starts_on)

        form_data["whl_page"] = "bbwllogin"

        save_url = f"https://www.{site}/wp-admin/options.php"
        headers = {"Referer": options_url, "User-Agent": "Mozilla/5.0"}
        resp = request_with_retry(self.session, "POST", save_url, data=form_data, headers=headers)
        return resp is not None and resp.status_code in (200, 302)


    def process_logo(self, domain: str, site_folder: str) -> dict:
        """处理Logo上传"""
        log.info(f"[{domain}] 处理Logo...")
        logo_filename = "logo.png"

        # 查询现有logo
        media_id, _, _ = self._query_existing_media(domain, logo_filename)
        if media_id:
            log.info(f"[{domain}] 已有logo，id={media_id}，直接使用")
            return {"success": True, "message": f"使用现有logo id={media_id}", "media_id": media_id}

        # 获取本地logo.png
        logo_path = self._find_local_file(site_folder, logo_filename)
        if not logo_path:
            return {"success": False, "message": f"本地未找到{logo_filename}", "media_id": None}

        # 获取上传nonce
        upload_nonce = self._get_upload_nonce(domain)
        if not upload_nonce:
            return {"success": False, "message": "获取上传nonce失败", "media_id": None}

        # 上传logo
        media_id, _ = self._upload_media_file(domain, logo_path, logo_filename, upload_nonce)
        if not media_id:
            return {"success": False, "message": "上传logo失败", "media_id": None}

        log.info(f"[{domain}] Logo上传成功，id={media_id}")
        return {"success": True, "message": f"上传成功 id={media_id}", "media_id": media_id}

    def _find_icon_file(self, folder: str) -> Optional[tuple]:
        """在文件夹中查找icon文件，返回 (path, filename)"""
        for name in ICON_NAMES:
            path = self._find_local_file(folder, name)
            if path:
                return path, name
        return None

    def process_icon(self, domain: str, site_folder: str,
                     date_format: str = "F j, Y", time_format: str = "g:i a",
                     week_starts_on: int = 1) -> dict:
        """处理Icon上传（set 3.py 实现：删除 → 上传 → 裁剪 → 保存设置含日期格式）"""
        log.info(f"[{domain}] 处理Icon...")

        for name in ICON_NAMES:
            icon_media_id, _, icon_delete_nonce = self._query_existing_media(domain, name)
            if icon_media_id and icon_delete_nonce:
                self._delete_media(domain, icon_media_id, icon_delete_nonce)
                log.info(f"[{domain}] 已删除旧icon({name}): {icon_media_id}")

        icon_info = self._find_icon_file(site_folder)
        if not icon_info:
            return {"success": False, "message": f"本地未找到icon文件 (尝试: {', '.join(ICON_NAMES)})"}
        icon_path, icon_name = icon_info

        upload_nonce = self._get_upload_nonce(domain)
        if not upload_nonce:
            return {"success": False, "message": "获取上传nonce失败"}

        media_id, crop_nonce = self._upload_media_file(domain, icon_path, icon_name, upload_nonce)
        if not media_id:
            return {"success": False, "message": "上传icon失败"}

        final_id = self._crop_icon(domain, media_id, crop_nonce) or media_id

        if not self._save_wp_settings(domain, site_icon_id=final_id,
                                      date_format=date_format, time_format=time_format,
                                      week_starts_on=week_starts_on):
            return {"success": False, "message": "保存WP设置失败"}

        log.info(f"[{domain}] Icon设置成功，id={final_id}")
        return {"success": True, "message": f"Icon设置成功 id={final_id}"}

    def _query_existing_banners(self, site: str) -> list:
        """查询所有已存在的banner"""
        existing = []
        for name in WP_BANNER_NAMES:
            media_id, _, delete_nonce = self._query_existing_media(site, name)
            if media_id and delete_nonce:
                existing.append({"id": media_id, "delete_nonce": delete_nonce, "name": name})
        return existing

    def process_banner(self, domain: str, site_folder: str) -> dict:
        """处理Banner上传（YSQD实现：删除旧banner → 格式转换 → 上传）"""
        log.info(f"[{domain}] 处理Banner...")

        upload_nonce = self._get_upload_nonce(domain)
        if not upload_nonce:
            return {"success": False, "message": "获取banner上传nonce失败"}

        # 1. 查询并删除所有现有banner
        existing = self._query_existing_banners(domain)
        target_name = None
        for item in existing:
            name_lower = item["name"].lower()
            if name_lower == "banner-scaled.jpg":
                continue
            if name_lower.endswith(".webp"):
                target_name = item["name"]
                break
        if not target_name:
            for item in existing:
                name_lower = item["name"].lower()
                if name_lower == "banner-scaled.jpg":
                    continue
                if name_lower.endswith(".jpg"):
                    target_name = item["name"]
                    break
        if not target_name:
            target_name = "banner.jpg"

        for item in existing:
            if self._delete_media(domain, item["id"], item["delete_nonce"]):
                log.info(f"[{domain}] 已删除旧banner: {item['name']}")

        # 2. 获取本地banner文件，确定目标格式
        banner_jpg = os.path.join(site_folder, "banner.jpg")
        banner_webp = os.path.join(site_folder, "banner.webp")
        banner_png = os.path.join(site_folder, "banner.png")

        if target_name.lower().endswith(".webp"):
            if os.path.exists(banner_webp):
                banner_path = banner_webp
            elif os.path.exists(banner_jpg):
                banner_path = self._convert_to_webp(banner_jpg, os.path.join(site_folder, "banner.webp"))
            elif os.path.exists(banner_png):
                banner_path = self._convert_to_webp(banner_png, os.path.join(site_folder, "banner.webp"))
            else:
                for name in WP_BANNER_NAMES + ["banner.png"]:
                    banner_path = self._find_local_file(site_folder, name)
                    if banner_path:
                        banner_path = self._convert_to_webp(banner_path, os.path.join(site_folder, "banner.webp"))
                        break
                else:
                    return {"success": False, "message": "本地未找到banner图片"}
        else:
            if os.path.exists(banner_jpg):
                banner_path = banner_jpg
            elif os.path.exists(banner_webp):
                banner_path = self._convert_to_jpg(banner_webp, os.path.join(site_folder, "banner.jpg"))
            elif os.path.exists(banner_png):
                banner_path = self._convert_to_jpg(banner_png, os.path.join(site_folder, "banner.jpg"))
            else:
                for name in WP_BANNER_NAMES + ["banner.png"]:
                    banner_path = self._find_local_file(site_folder, name)
                    if banner_path:
                        banner_path = self._convert_to_jpg(banner_path, os.path.join(site_folder, "banner.jpg"))
                        break
                else:
                    return {"success": False, "message": "本地未找到banner图片"}

        # 3. 上传banner
        mime = "image/webp" if target_name.lower().endswith(".webp") else "image/jpeg"
        media_id, _ = self._upload_media_file(domain, banner_path, target_name, upload_nonce, mime=mime)
        if not media_id:
            return {"success": False, "message": "Banner上传失败"}

        banner_url = f"https://www.{domain}/wp-content/uploads/{target_name}"
        log.info(f"[{domain}] Banner上传成功: {banner_url}")
        return {"success": True, "message": f"Banner上传成功: {banner_url}"}

    def process_rocket(self, domain: str) -> dict:
        """配置WP Rocket

        Args:
            domain: 域名

        Returns:
            {"success": bool, "message": str}
        """
        log.info(f"[{domain}] 配置WP Rocket...")

        try:
            # 获取插件页面
            wp_url = f'https://www.{domain}/wp-admin/plugins.php'
            wp_response = request_with_retry(self.session, "GET", wp_url)
            if not wp_response or wp_response.status_code != 200:
                return {"success": False, "message": "无法访问插件页面"}

            # 解析插件页面
            soup = BeautifulSoup(wp_response.text, 'html.parser')

            # 尝试激活WP Rocket插件
            activate_element = soup.find('a', {'id': 'activate-wp-rocket'})
            if activate_element and activate_element.get('href'):
                activate_url = activate_element.get('href')
                if not activate_url.startswith('http'):
                    activate_url = f'https://www.{domain}/wp-admin/{activate_url}'

                activate_response = request_with_retry(self.session, "GET", activate_url)
                if activate_response and activate_response.status_code == 200:
                    log.info(f"[{domain}] 已激活WP Rocket")
            else:
                log.info(f"[{domain}] WP Rocket已激活")

            # 获取WP Rocket设置页面
            setting_url = f'https://www.{domain}/wp-admin/options-general.php?page=wprocket'
            st_response = request_with_retry(self.session, "GET", setting_url)
            if not st_response or st_response.status_code != 200:
                return {"success": False, "message": "无法访问WP Rocket设置页面"}

            # 解析设置页面
            nonce_soup = BeautifulSoup(st_response.text, 'html.parser')

            wpnonce = nonce_soup.find('input', {"id": "_wpnonce"})
            wpnonce = wpnonce.get('value') if wpnonce else ""

            secret_key = nonce_soup.find('input', {'id': 'secret_key'})
            secret_key = secret_key.get('value') if secret_key else ""

            minify_js_key = nonce_soup.find('input', {'id': 'minify_js_key'})
            minify_js_key = minify_js_key.get('value') if minify_js_key else ""

            consumer_email = nonce_soup.find('input', {'id': 'consumer_email'})
            consumer_email = consumer_email.get('value') if consumer_email else ""

            consumer_key = nonce_soup.find('input', {'id': 'consumer_key'})
            consumer_key = consumer_key.get('value') if consumer_key else ""

            version = nonce_soup.find('input', {'id': 'version'})
            version = version.get('value') if version else ""

            minify_css_key = nonce_soup.find('input', {'id': 'minify_css_key'})
            minify_css_key = minify_css_key.get('value') if minify_css_key else ""

            # 构造提交设置的表单数据
            setting_data = {
                "option_page": "wprocket",
                "action": "update",
                "_wpnonce": wpnonce,
                "_wp_http_referer": "/wp-admin/options-general.php?page=wprocket",
                "wp_rocket_settings[cache_mobile]": "1",
                "wp_rocket_settings[do_caching_mobile_files]": "1",
                "wp_rocket_settings[purge_cron_interval]": "0",
                "wp_rocket_settings[purge_cron_unit]": "HOUR_IN_SECONDS",
                "wp_rocket_settings[minify_css]": "1",
                "wp_rocket_settings[exclude_css]": "",
                "wp_rocket_settings[optimize_css_delivery]": "1",
                "wp_rocket_settings[remove_unused_css_safelist]": "",
                "wp_rocket_settings[critical_css]": "",
                "wp_rocket_settings[minify_js]": "1",
                "wp_rocket_settings[exclude_inline_js]": "",
                "wp_rocket_settings[exclude_js]": "",
                "wp_rocket_settings[exclude_defer_js]": "",
                "wp_rocket_settings[delay_js_exclusions]": "",
                "wp_rocket_settings[lazyload]": "1",
                "wp_rocket_settings[exclude_lazyload]": "",
                "wp_rocket_settings[image_dimensions]": "1",
                "wp_rocket_settings[manual_preload]": "1",
                "wp_rocket_settings[preload_excluded_uri]": "",
                "wp_rocket_settings[preload_links]": "1",
                "wp_rocket_settings[dns_prefetch]": "",
                "wp_rocket_settings[preload_fonts]": "",
                "wp_rocket_settings[cache_reject_uri]": "",
                "wp_rocket_settings[cache_reject_cookies]": "",
                "wp_rocket_settings[cache_reject_ua]": "",
                "wp_rocket_settings[cache_purge_pages]": "",
                "wp_rocket_settings[cache_query_strings]": "",
                "wp_rocket_settings[automatic_cleanup_frequency]": "daily",
                "wp_rocket_settings[cdn_cnames][]": "",
                "wp_rocket_settings[cdn_zone][]": "all",
                "wp_rocket_settings[cdn_reject_files]": "",
                "wp_rocket_settings[heartbeat_admin_behavior]": "",
                "wp_rocket_settings[heartbeat_editor_behavior]": "",
                "wp_rocket_settings[heartbeat_site_behavior]": "",
                "wp_rocket_settings[cloudflare_api_key]": "",
                "wp_rocket_settings[cloudflare_email]": "",
                "wp_rocket_settings[cloudflare_zone_id]": "",
                "wp_rocket_settings[sucury_waf_api_key]": "",
                "wp_rocket_settings[consumer_key]": consumer_key,
                "wp_rocket_settings[consumer_email]": consumer_email,
                "wp_rocket_settings[secret_key]": secret_key,
                "wp_rocket_settings[license]": "",
                "wp_rocket_settings[secret_cache_key]": "",
                "wp_rocket_settings[minify_css_key]": minify_css_key,
                "wp_rocket_settings[minify_js_key]": minify_js_key,
                "wp_rocket_settings[version]": version,
                "wp_rocket_settings[cloudflare_old_settings]": "",
                "wp_rocket_settings[cache_ssl]": "1",
                "wp_rocket_settings[minify_google_fonts]": "0",
                "wp_rocket_settings[emoji]": "0",
                "wp_rocket_settings[remove_unused_css]": "1",
                "wp_rocket_settings[async_css]": "0",
                "wp_rocket_settings[async_css_mobile]": ""
            }

            # 提交表单
            option_url = f'https://www.{domain}/wp-admin/options.php'
            st_response = request_with_retry(self.session, "POST", option_url, data=setting_data)

            if st_response and st_response.status_code == 200:
                log.info(f"[{domain}] WP Rocket配置成功")
                return {"success": True, "message": "WP Rocket配置成功"}
            else:
                return {"success": False, "message": "WP Rocket设置提交失败"}

        except Exception as e:
            log.error(f"[{domain}] WP Rocket配置失败: {e}")
            return {"success": False, "message": str(e)}

    def _get_yoast_nonce(self, domain: str) -> Optional[str]:
        """从Yoast页面提取WP REST API nonce"""
        candidates = [
            f"https://www.{domain}/wp-admin/admin.php?page=wpseo_dashboard",
            f"https://www.{domain}/wp-admin/index.php",
            f"https://www.{domain}/wp-admin/",
        ]
        html = None
        for url in candidates:
            resp = request_with_retry(self.session, "GET", url)
            if resp and resp.status_code == 200 and resp.text:
                html = resp.text
                break
        if not html:
            return None

        patterns = [
            r'wpApiSettings["\']?\s*[:=]\s*{[^}]*?["\']nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
            r'window\.wpApiSettings\s*=\s*{[^}]*?["\']nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
            r'nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
            r'X-WP-Nonce["\']?\s*[:=]\s*["\']([a-zA-Z0-9\-_]+)["\']',
            r'data-wp-nonce=["\']([a-zA-Z0-9\-_]+)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def _post_yoast_api(self, domain: str, path: str, nonce: str, payload: dict) -> bool:
        """调用Yoast REST API"""
        url = f"https://www.{domain}{path}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}_wpnonce={nonce}"
        headers = {
            "Content-Type": "application/json",
            "X-WP-Nonce": nonce,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.{domain}/wp-admin/",
        }
        resp = request_with_retry(self.session, "POST", url, headers=headers,
                                  data=json.dumps(payload), retries=2, delay=3)
        if resp and resp.status_code < 500:
            return True
        return False

    def _get_wpseo_page_settings_data(self, domain: str) -> Optional[dict]:
        """从wpseo_page_settings页面提取动态参数"""
        url = f"https://www.{domain}/wp-admin/admin.php?page=wpseo_page_settings#/homepage"
        resp = request_with_retry(self.session, "GET", url)
        if not resp or resp.status_code != 200:
            return None
        text = resp.text
        patterns = {
            'nonce': r'"endpoint".*?"nonce":"([^"]+)"',
            'index_now_key': r'"index_now_key":"([^"]+)"',
            'version': r'"version":"([^"]+)"',
            'first_activated_on': r'"first_activated_on":([^"]+),',
            'activation_redirect_timestamp_free': r'"activation_redirect_timestamp_free":([^"]+),',
            'website_name': r'"website_name":"([^"]+)"',
            'company_logo': r'"company_logo":"([^"]+)"',
            'company_logo_id': r'"company_logo_id":([^,]+),',
            'company_name': r'"company_name":"([^"]+)"',
            'blogdescription': r'"blogdescription":"([^"]*)"',
        }
        result = {}
        for key, pat in patterns.items():
            m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
            if m:
                result[key] = m.group(1).strip()
        if 'company_logo' in result:
            result['company_logo'] = result['company_logo'].replace('\\/', '/')
        required = {'nonce', 'index_now_key', 'version', 'company_logo', 'company_logo_id', 'company_name', 'website_name'}
        if required - set(result.keys()):
            return None
        return result

    def _process_yoast_advanced(self, domain: str, params: dict) -> bool:
        """提交完整Yoast设置表单到options.php"""
        site = domain
        data = {
            'option_page': 'wpseo_page_settings',
            '_wp_http_referer': 'admin.php?page=wpseo_page_settings_saved',
            'action': 'update',
            '_wpnonce': params['nonce'],
            'wpseo[tracking]': 'false',
            'wpseo[toggled_tracking]': 'true',
            'wpseo[license_server_version]': 'false',
            'wpseo[ms_defaults_set]': 'false',
            'wpseo[ignore_search_engines_discouraged_notice]': 'false',
            'wpseo[indexing_first_time]': 'true',
            'wpseo[indexing_started]': 'false',
            'wpseo[indexing_reason]': 'first_install',
            'wpseo[indexables_indexing_completed]': 'false',
            'wpseo[index_now_key]': params.get('index_now_key', ''),
            'wpseo[version]': params.get('version', ''),
            'wpseo[previous_version]': '',
            'wpseo[disableadvanced_meta]': 'true',
            'wpseo[enable_headless_rest_endpoints]': 'true',
            'wpseo[ryte_indexability]': 'false',
            'wpseo[baiduverify]': '',
            'wpseo[googleverify]': '',
            'wpseo[msverify]': '',
            'wpseo[yandexverify]': '',
            'wpseo[site_type]': '',
            'wpseo[has_multiple_authors]': '',
            'wpseo[environment_type]': '',
            'wpseo[content_analysis_active]': 'true',
            'wpseo[keyword_analysis_active]': 'true',
            'wpseo[inclusive_language_analysis_active]': 'false',
            'wpseo[enable_admin_bar_menu]': 'true',
            'wpseo[enable_cornerstone_content]': 'true',
            'wpseo[enable_xml_sitemap]': 'true',
            'wpseo[enable_text_link_counter]': 'true',
            'wpseo[enable_index_now]': 'true',
            'wpseo[enable_ai_generator]': 'true',
            'wpseo[ai_enabled_pre_default]': 'false',
            'wpseo[show_onboarding_notice]': 'true',
            'wpseo[first_activated_on]': params.get('first_activated_on', ''),
            'wpseo[semrush_integration_active]': 'true',
            'wpseo[semrush_country_code]': 'us',
            'wpseo[permalink_structure]': '',
            'wpseo[home_url]': '',
            'wpseo[dynamic_permalinks]': 'false',
            'wpseo[category_base_url]': '',
            'wpseo[tag_base_url]': '',
            'wpseo[enable_enhanced_slack_sharing]': 'true',
            'wpseo[enable_metabox_insights]': 'true',
            'wpseo[enable_link_suggestions]': 'true',
            'wpseo[algolia_integration_active]': 'false',
            'wpseo[dismiss_configuration_workout_notice]': 'false',
            'wpseo[dismiss_premium_deactivated_notice]': 'false',
            'wpseo[wincher_integration_active]': 'true',
            'wpseo[wincher_automatically_add_keyphrases]': 'false',
            'wpseo[wincher_website_id]': '',
            'wpseo[first_time_install]': 'true',
            'wpseo[should_redirect_after_install_free]': 'false',
            'wpseo[activation_redirect_timestamp_free]': params.get('activation_redirect_timestamp_free', ''),
            'wpseo[remove_feed_global]': 'false',
            'wpseo[remove_feed_global_comments]': 'false',
            'wpseo[remove_feed_post_comments]': 'false',
            'wpseo[remove_feed_authors]': 'false',
            'wpseo[remove_feed_categories]': 'false',
            'wpseo[remove_feed_tags]': 'false',
            'wpseo[remove_feed_custom_taxonomies]': 'false',
            'wpseo[remove_feed_post_types]': 'false',
            'wpseo[remove_feed_search]': 'false',
            'wpseo[remove_atom_rdf_feeds]': 'false',
            'wpseo[remove_shortlinks]': 'false',
            'wpseo[remove_rest_api_links]': 'false',
            'wpseo[remove_rsd_wlw_links]': 'false',
            'wpseo[remove_oembed_links]': 'false',
            'wpseo[remove_generator]': 'false',
            'wpseo[remove_emoji_scripts]': 'false',
            'wpseo[remove_powered_by_header]': 'false',
            'wpseo[remove_pingback_header]': 'false',
            'wpseo[clean_campaign_tracking_urls]': 'false',
            'wpseo[clean_permalinks]': 'false',
            'wpseo[search_cleanup]': 'false',
            'wpseo[search_cleanup_emoji]': 'false',
            'wpseo[search_cleanup_patterns]': 'false',
            'wpseo[search_character_limit]': '50',
            'wpseo[deny_search_crawling]': 'false',
            'wpseo[deny_wp_json_crawling]': 'false',
            'wpseo[deny_adsbot_crawling]': 'false',
            'wpseo[deny_ccbot_crawling]': 'false',
            'wpseo[deny_google_extended_crawling]': 'false',
            'wpseo[deny_gptbot_crawling]': 'false',
            'wpseo[redirect_search_pretty_urls]': 'false',
            'wpseo[indexables_overview_state]': 'dashboard-not-visited',
            'wpseo[last_known_public_post_types][0]': 'post',
            'wpseo[last_known_public_post_types][1]': 'page',
            'wpseo[last_known_public_post_types][2]': 'product',
            'wpseo[last_known_public_taxonomies][0]': 'category',
            'wpseo[last_known_public_taxonomies][1]': 'post_tag',
            'wpseo[last_known_public_taxonomies][2]': 'post_format',
            'wpseo[last_known_public_taxonomies][3]': 'product_brand',
            'wpseo[last_known_public_taxonomies][4]': 'product_cat',
            'wpseo[last_known_public_taxonomies][5]': 'product_tag',
            'wpseo[last_known_public_taxonomies][6]': 'product_shipping_class',
            'wpseo[last_known_no_unindexed]': '[object Object]',
            'wpseo[site_kit_configuration_permanently_dismissed]': 'false',
            'wpseo[site_kit_connected]': 'false',
            # 标题 & 元描述
            'wpseo_titles[forcerewritetitle]': 'false',
            'wpseo_titles[separator]': 'sc-dash',
            'wpseo_titles[title-home-wpseo]': '%%sitename%%',
            'wpseo_titles[title-author-wpseo]': '%%name%%, Author at %%sitename%% %%page%%',
            'wpseo_titles[title-archive-wpseo]': '%%date%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[title-search-wpseo]': 'You searched for %%searchphrase%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[title-404-wpseo]': 'Page not found %%sep%% %%sitename%%',
            'wpseo_titles[social-title-author-wpseo]': '%%name%%',
            'wpseo_titles[social-title-archive-wpseo]': '%%date%%',
            'wpseo_titles[social-description-author-wpseo]': '',
            'wpseo_titles[social-description-archive-wpseo]': '',
            'wpseo_titles[social-image-url-author-wpseo]': '',
            'wpseo_titles[social-image-url-archive-wpseo]': '',
            'wpseo_titles[social-image-id-author-wpseo]': '0',
            'wpseo_titles[social-image-id-archive-wpseo]': '0',
            'wpseo_titles[metadesc-home-wpseo]': '%%sitedesc%%',
            'wpseo_titles[metadesc-author-wpseo]': '',
            'wpseo_titles[metadesc-archive-wpseo]': '',
            'wpseo_titles[rssbefore]': '',
            'wpseo_titles[rssafter]': 'The post %%POSTLINK%% appeared first on %%BLOGLINK%%.',
            'wpseo_titles[noindex-author-wpseo]': 'false',
            'wpseo_titles[noindex-author-noposts-wpseo]': 'true',
            'wpseo_titles[noindex-archive-wpseo]': 'true',
            'wpseo_titles[disable-author]': 'false',
            'wpseo_titles[disable-date]': 'false',
            'wpseo_titles[disable-post_format]': 'false',
            'wpseo_titles[disable-attachment]': 'true',
            'wpseo_titles[breadcrumbs-404crumb]': 'Error 404: Page not found',
            'wpseo_titles[breadcrumbs-display-blog-page]': 'true',
            'wpseo_titles[breadcrumbs-boldlast]': 'false',
            'wpseo_titles[breadcrumbs-archiveprefix]': 'Archives for',
            'wpseo_titles[breadcrumbs-enable]': 'true',
            'wpseo_titles[breadcrumbs-home]': 'Home',
            'wpseo_titles[breadcrumbs-prefix]': '',
            'wpseo_titles[breadcrumbs-searchprefix]': 'You searched for',
            'wpseo_titles[breadcrumbs-sep]': '»',
            'wpseo_titles[website_name]': params.get('website_name', domain),
            'wpseo_titles[person_name]': '',
            'wpseo_titles[person_logo]': '',
            'wpseo_titles[person_logo_id]': '0',
            'wpseo_titles[alternate_website_name]': '',
            'wpseo_titles[company_logo]': params.get('company_logo', ''),
            'wpseo_titles[company_logo_id]': params.get('company_logo_id', '0'),
            'wpseo_titles[company_name]': params.get('company_name', domain),
            'wpseo_titles[company_alternate_name]': '',
            'wpseo_titles[company_or_person]': 'company',
            'wpseo_titles[company_or_person_user_id]': 'false',
            'wpseo_titles[stripcategorybase]': 'false',
            'wpseo_titles[open_graph_frontpage_title]': '%%sitename%%',
            'wpseo_titles[open_graph_frontpage_desc]': '',
            'wpseo_titles[open_graph_frontpage_image]': params.get('company_logo', ''),
            'wpseo_titles[open_graph_frontpage_image_id]': params.get('company_logo_id', '0'),
            'wpseo_titles[publishing_principles_id]': '0',
            'wpseo_titles[ownership_funding_info_id]': '0',
            'wpseo_titles[actionable_feedback_policy_id]': '0',
            'wpseo_titles[corrections_policy_id]': '0',
            'wpseo_titles[ethics_policy_id]': '0',
            'wpseo_titles[diversity_policy_id]': '0',
            'wpseo_titles[diversity_staffing_report_id]': '0',
            'wpseo_titles[org-description]': '',
            'wpseo_titles[org-email]': '',
            'wpseo_titles[org-phone]': '',
            'wpseo_titles[org-legal-name]': '',
            'wpseo_titles[org-founding-date]': '',
            'wpseo_titles[org-number-employees]': '',
            'wpseo_titles[org-vat-id]': '',
            'wpseo_titles[org-tax-id]': '',
            'wpseo_titles[org-iso]': '',
            'wpseo_titles[org-duns]': '',
            'wpseo_titles[org-leicode]': '',
            'wpseo_titles[org-naics]': '',
            'wpseo_titles[title-post]': '%%title%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-post]': '',
            'wpseo_titles[noindex-post]': 'false',
            'wpseo_titles[display-metabox-pt-post]': 'true',
            'wpseo_titles[post_types-post-maintax]': '0',
            'wpseo_titles[schema-page-type-post]': 'WebPage',
            'wpseo_titles[schema-article-type-post]': 'Article',
            'wpseo_titles[social-title-post]': '%%title%%',
            'wpseo_titles[social-description-post]': '',
            'wpseo_titles[social-image-url-post]': '',
            'wpseo_titles[social-image-id-post]': '0',
            'wpseo_titles[title-page]': '%%title%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-page]': '',
            'wpseo_titles[noindex-page]': 'false',
            'wpseo_titles[display-metabox-pt-page]': 'true',
            'wpseo_titles[post_types-page-maintax]': '0',
            'wpseo_titles[schema-page-type-page]': 'WebPage',
            'wpseo_titles[schema-article-type-page]': 'None',
            'wpseo_titles[social-title-page]': '%%title%%',
            'wpseo_titles[social-description-page]': '',
            'wpseo_titles[social-image-url-page]': '',
            'wpseo_titles[social-image-id-page]': '0',
            'wpseo_titles[title-attachment]': '%%title%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-attachment]': '',
            'wpseo_titles[noindex-attachment]': 'false',
            'wpseo_titles[display-metabox-pt-attachment]': 'true',
            'wpseo_titles[post_types-attachment-maintax]': '0',
            'wpseo_titles[schema-page-type-attachment]': 'WebPage',
            'wpseo_titles[schema-article-type-attachment]': 'None',
            'wpseo_titles[title-tax-category]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-category]': '',
            'wpseo_titles[display-metabox-tax-category]': 'true',
            'wpseo_titles[noindex-tax-category]': 'false',
            'wpseo_titles[social-title-tax-category]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-category]': '',
            'wpseo_titles[social-image-url-tax-category]': '',
            'wpseo_titles[social-image-id-tax-category]': '0',
            'wpseo_titles[taxonomy-category-ptparent]': '0',
            'wpseo_titles[title-tax-post_tag]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-post_tag]': '',
            'wpseo_titles[display-metabox-tax-post_tag]': 'true',
            'wpseo_titles[noindex-tax-post_tag]': 'false',
            'wpseo_titles[social-title-tax-post_tag]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-post_tag]': '',
            'wpseo_titles[social-image-url-tax-post_tag]': '',
            'wpseo_titles[social-image-id-tax-post_tag]': '0',
            'wpseo_titles[taxonomy-post_tag-ptparent]': '0',
            'wpseo_titles[title-tax-post_format]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-post_format]': '',
            'wpseo_titles[display-metabox-tax-post_format]': 'true',
            'wpseo_titles[noindex-tax-post_format]': 'true',
            'wpseo_titles[social-title-tax-post_format]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-post_format]': '',
            'wpseo_titles[social-image-url-tax-post_format]': '',
            'wpseo_titles[social-image-id-tax-post_format]': '0',
            'wpseo_titles[taxonomy-post_format-ptparent]': '0',
            'wpseo_titles[title-product]': '%%title%% %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-product]': '',
            'wpseo_titles[noindex-product]': 'false',
            'wpseo_titles[display-metabox-pt-product]': 'true',
            'wpseo_titles[post_types-product-maintax]': '0',
            'wpseo_titles[schema-page-type-product]': 'WebPage',
            'wpseo_titles[schema-article-type-product]': 'None',
            'wpseo_titles[social-title-product]': '%%title%%',
            'wpseo_titles[social-description-product]': '',
            'wpseo_titles[social-image-url-product]': '',
            'wpseo_titles[social-image-id-product]': '0',
            'wpseo_titles[title-ptarchive-product]': '%%pt_plural%% Archive %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-ptarchive-product]': '',
            'wpseo_titles[bctitle-ptarchive-product]': '',
            'wpseo_titles[noindex-ptarchive-product]': 'false',
            'wpseo_titles[social-title-ptarchive-product]': '%%pt_plural%% Archive',
            'wpseo_titles[social-description-ptarchive-product]': '',
            'wpseo_titles[social-image-url-ptarchive-product]': '',
            'wpseo_titles[social-image-id-ptarchive-product]': '0',
            'wpseo_titles[title-tax-product_brand]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-product_brand]': '',
            'wpseo_titles[display-metabox-tax-product_brand]': 'true',
            'wpseo_titles[noindex-tax-product_brand]': 'false',
            'wpseo_titles[social-title-tax-product_brand]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-product_brand]': '',
            'wpseo_titles[social-image-url-tax-product_brand]': '',
            'wpseo_titles[social-image-id-tax-product_brand]': '0',
            'wpseo_titles[taxonomy-product_brand-ptparent]': '0',
            'wpseo_titles[title-tax-product_cat]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-product_cat]': '',
            'wpseo_titles[display-metabox-tax-product_cat]': 'true',
            'wpseo_titles[noindex-tax-product_cat]': 'false',
            'wpseo_titles[social-title-tax-product_cat]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-product_cat]': '',
            'wpseo_titles[social-image-url-tax-product_cat]': '',
            'wpseo_titles[social-image-id-tax-product_cat]': '0',
            'wpseo_titles[taxonomy-product_cat-ptparent]': '0',
            'wpseo_titles[title-tax-product_tag]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-product_tag]': '',
            'wpseo_titles[display-metabox-tax-product_tag]': 'true',
            'wpseo_titles[noindex-tax-product_tag]': 'false',
            'wpseo_titles[social-title-tax-product_tag]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-product_tag]': '',
            'wpseo_titles[social-image-url-tax-product_tag]': '',
            'wpseo_titles[social-image-id-tax-product_tag]': '0',
            'wpseo_titles[taxonomy-product_tag-ptparent]': '0',
            'wpseo_titles[title-tax-product_shipping_class]': '%%term_title%% Archives %%page%% %%sep%% %%sitename%%',
            'wpseo_titles[metadesc-tax-product_shipping_class]': '',
            'wpseo_titles[display-metabox-tax-product_shipping_class]': 'true',
            'wpseo_titles[noindex-tax-product_shipping_class]': 'false',
            'wpseo_titles[social-title-tax-product_shipping_class]': '%%term_title%% Archives',
            'wpseo_titles[social-description-tax-product_shipping_class]': '',
            'wpseo_titles[social-image-url-tax-product_shipping_class]': '',
            'wpseo_titles[social-image-id-tax-product_shipping_class]': '0',
            'wpseo_titles[taxonomy-product_shipping_class-ptparent]': '0',
            'wpseo_social[facebook_site]': '',
            'wpseo_social[instagram_url]': '',
            'wpseo_social[linkedin_url]': '',
            'wpseo_social[myspace_url]': '',
            'wpseo_social[og_default_image]': '',
            'wpseo_social[og_default_image_id]': '',
            'wpseo_social[og_frontpage_title]': '',
            'wpseo_social[og_frontpage_desc]': '',
            'wpseo_social[og_frontpage_image]': '',
            'wpseo_social[og_frontpage_image_id]': '',
            'wpseo_social[opengraph]': 'true',
            'wpseo_social[pinterest_url]': '',
            'wpseo_social[pinterestverify]': '',
            'wpseo_social[twitter]': 'true',
            'wpseo_social[twitter_card_type]': 'summary_large_image',
            'wpseo_social[youtube_url]': '',
            'wpseo_social[wikipedia_url]': '',
            'wpseo_social[mastodon_url]': '',
            'blogdescription': params.get('blogdescription', ''),
        }
        url = f"https://www.{domain}/wp-admin/options.php"
        headers = {
            "Referer": f"https://www.{domain}/wp-admin/admin.php?page=wpseo_page_settings",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        resp = request_with_retry(self.session, "POST", url, data=data, headers=headers,
                                  retries=2, delay=3)
        return resp is not None and resp.status_code in (200, 302)

    def process_yoast(self, domain: str) -> dict:
        """配置Yoast SEO（完整YSQD实现）"""
        log.info(f"[{domain}] 配置Yoast SEO...")

        try:
            # 1. 激活Yoast SEO插件
            plugins_url = f"https://www.{domain}/wp-admin/plugins.php"
            resp = request_with_retry(self.session, "GET", plugins_url)
            if resp and resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for plugin_id in ("activate-wordpress-seo", "activate-yoast-seo-premium"):
                    link = soup.find("a", {"id": plugin_id})
                    if link and link.get("href"):
                        href = link["href"]
                        activate_url = href if href.startswith("http") else urljoin(plugins_url, href)
                        request_with_retry(self.session, "GET", activate_url, retries=1)

            # 2. 获取Yoast nonce
            nonce = self._get_yoast_nonce(domain)
            if not nonce:
                return {"success": False, "message": "无法获取Yoast nonce"}

            # 3. REST API - optimize SEO data
            self._post_yoast_api(domain, "/wp-json/yoast/v1/configuration/save_configuration_state",
                                 nonce, {"finishedSteps": ["optimizeSeoData"]})

            # 4. REST API - site representation
            logo_url = f"https://www.{domain}/wp-content/uploads/logo.png"
            self._post_yoast_api(domain, "/wp-json/yoast/v1/configuration/site_representation",
                                 nonce, {
                "company_or_person": "company",
                "company_name": domain,
                "company_logo": logo_url,
                "company_logo_id": 0,
                "person_logo": "",
                "person_logo_id": 0,
                "website_name": domain,
            })

            # 5. REST API - save all configuration steps
            self._post_yoast_api(domain, "/wp-json/yoast/v1/configuration/save_configuration_state",
                                 nonce, {
                "finishedSteps": ["optimizeSeoData", "siteRepresentation",
                                  "socialProfiles", "personalPreferences"]
            })

            # 6. REST API - social profiles
            self._post_yoast_api(domain, "/wp-json/yoast/v1/configuration/social_profiles",
                                 nonce, {
                "facebook_site": "", "twitter_site": "", "other_social_urls": []
            })

            # 7. REST API - disable tracking
            self._post_yoast_api(domain, "/wp-json/yoast/v1/configuration/enable_tracking",
                                 nonce, {"tracking": 0})

            # 8. 提取高级设置参数并提交完整表单
            params = self._get_wpseo_page_settings_data(domain)
            if params:
                self._process_yoast_advanced(domain, params)
            else:
                log.warning(f"[{domain}] 无法提取高级Yoast参数，跳过完整配置")

            log.info(f"[{domain}] Yoast SEO配置成功")
            return {"success": True, "message": "Yoast SEO配置成功"}

        except Exception as e:
            log.error(f"[{domain}] Yoast配置失败: {e}")
            return {"success": False, "message": str(e)}

    @staticmethod
    def _normalize_data_source_ids(raw: str) -> str:
        """清洗数据源ID格式：去空格、去首尾逗号、浮点数转整数

        Args:
            raw: 原始数据源ID字符串

        Returns:
            清洗后的逗号分隔纯数字字符串

        Raises:
            ValueError: 如果包含非数字ID
        """
        s = str(raw or "").strip()
        if not s or s.lower() in ("nan", "none"):
            return ""
        parts = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if part.replace(".", "", 1).isdigit() and "." in part:
                part = part.split(".")[0]
            if not part.isdigit():
                raise ValueError(f"数据源ID格式错误，应为纯数字，实际为: '{part}'")
            parts.append(part)
        return ",".join(parts)

    @staticmethod
    def _discover_update_img_url(session, site):
        try:
            resp = session.get(f"{site}/wp-admin/options-general.php", timeout=60, verify=False)
            if resp.status_code != 200:
                return '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'
            soup = BeautifulSoup(resp.text, "html.parser")
            update_img = soup.find("a", string="Update Img", class_="ab-item")
            if update_img and update_img.get("href"):
                return update_img.get("href")
        except requests.RequestException:
            pass
        return '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'

    def upload_data(self, domain: str, data_source_ids: str,
                    progress_callback=None) -> dict:
        """上传数据到WordPress站点（无需登录）

        Args:
            domain: 域名
            data_source_ids: 数据源ID (逗号分隔)
            progress_callback: 进度回调

        Returns:
            {"success": bool, "message": str, "success_count": int, "failure_count": int}
        """
        log.info(f"[{domain}] 上传数据，数据源ID: {data_source_ids}")

        domain = str(domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
        if domain.startswith("www."):
            domain = domain[4:]

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        })

        success_count = 0
        failure_count = 0
        try:
            site = f"https://www.{domain}"
            update_img = self._discover_update_img_url(session, site)

            data_source_ids = self._normalize_data_source_ids(data_source_ids)
            if not data_source_ids:
                return {"success": False, "message": "数据源ID为空或格式无效", "success_count": 0, "failure_count": 0}
            idcode = data_source_ids.replace(",", "%2C")
            cs = "0"
            retrytime = 0
            repeat_count = 0
            brand_count = 0

            if progress_callback:
                progress_callback(f"开始上传数据，update_img: {update_img}，断点: {cs}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest",
            }

            for i in range(800):
                upload_url = f'{site}{update_img.replace("/plxztp.php?", "/dan_duopsot.php?")}&lv={idcode}&cs={cs}'
                try:
                    resp = session.get(upload_url, headers=headers, timeout=120, verify=False)
                except requests.exceptions.RequestException as exc:
                    retrytime += 1
                    if progress_callback:
                        progress_callback(f"请求异常，重试 {retrytime}/10: {exc}")
                    if retrytime >= 10:
                        return {"success": False, "message": str(exc), "success_count": success_count, "failure_count": failure_count}
                    time.sleep(2)
                    continue

                payload = self._parse_json_response(resp.text) if resp.status_code == 200 else None
                if resp.status_code != 200 or payload is None:
                    retrytime += 1
                    snippet = (resp.text or "")[:200].replace("\n", " ") if resp.status_code == 200 else ""
                    if progress_callback:
                        progress_callback(f"请求失败，重试 {retrytime}/10" + (f" (响应非JSON: {snippet})" if snippet else ""))
                    if retrytime >= 10:
                        return {"success": False, "message": f"上传失败 HTTP {resp.status_code}", "success_count": success_count, "failure_count": failure_count}
                    time.sleep(2)
                    continue

                retrytime = 0
                msg = str(payload.get("msg", ""))
                if progress_callback:
                    progress_callback(f"上传状态: {msg}")

                progress = self._parse_progress(msg)
                if progress:
                    success_count += progress["success"]
                    failure_count += progress["failure"]
                    repeat_count += progress["repeat"]
                    brand_count += progress["brand"]
                    if progress["cs"] > int(cs):
                        cs = str(progress["cs"])
                        if progress_callback:
                            progress_callback(f"更新断点: {cs}")

                if "完成" in msg:
                    if progress_callback:
                        progress_callback(f"完成 已上传{cs} 成功{success_count} 失败{failure_count} 重复{repeat_count}")
                        progress_callback("开始批量处理图片")

                    img_retry = 0
                    for j in range(400):
                        dimg_url = f'{site}{update_img.replace("/plxztp.php?", "/dimg.php?")}'
                        try:
                            img_resp = session.get(dimg_url, headers=headers, timeout=120, verify=False)
                        except requests.exceptions.RequestException as exc:
                            img_retry += 1
                            if progress_callback:
                                progress_callback(f"图片处理请求异常，重试 {img_retry}/10: {exc}")
                            if img_retry > 10:
                                break
                            time.sleep(2)
                            continue

                        img_payload = self._parse_json_response(img_resp.text) if img_resp.status_code == 200 else None
                        if img_resp.status_code != 200 or img_payload is None:
                            img_retry += 1
                            if progress_callback:
                                progress_callback(f"图片处理失败，重试 {img_retry}/10")
                            if img_retry > 10:
                                break
                            time.sleep(2)
                            continue

                        img_retry = 0
                        img_msg = str(img_payload.get("msg", ""))
                        if "成功-0失败-0" in img_msg:
                            if progress_callback:
                                progress_callback(f"图片处理完成")
                            break

                        if progress_callback:
                            progress_callback(f"图片处理: {img_msg}")
                        time.sleep(1)

                    return {"success": True, "message": f"上传完成: 成功{success_count}, 失败{failure_count}, 重复{repeat_count}",
                            "success_count": success_count, "failure_count": failure_count}

                if "code" in payload:
                    next_cs = str(payload["code"])
                    if next_cs != cs:
                        cs = next_cs
                        if progress_callback:
                            progress_callback(f"更新断点: {cs}")
                else:
                    if progress_callback:
                        progress_callback("无code返回，结束上传")
                    return {"success": True, "message": f"上传结束: 成功{success_count}, 失败{failure_count}",
                            "success_count": success_count, "failure_count": failure_count}

                time.sleep(1)

            return {"success": True, "message": f"上传结束: 成功{success_count}, 失败{failure_count}",
                    "success_count": success_count, "failure_count": failure_count}

        except Exception as e:
            log.error(f"[{domain}] 上传异常: {e}")
            return {"success": False, "message": str(e), "success_count": success_count,
                    "failure_count": failure_count}
        finally:
            try:
                session.close()
            except Exception:
                pass

    def configure_media(self, domain: str, media_root: str, progress_callback=None) -> dict:
        """配置媒体文件 (Logo, Icon, Banner)

        Args:
            domain: 域名
            media_root: 媒体文件根目录
            progress_callback: 进度回调

        Returns:
            {"success": bool, "message": str}
        """
        site_folder = os.path.join(media_root, domain)

        try:
            # 检查媒体文件夹是否存在
            if not os.path.exists(site_folder):
                return {"success": False, "message": f"媒体文件夹不存在: {site_folder}"}

            # 上传Logo
            if progress_callback:
                progress_callback("上传Logo...")
            logo_result = self.process_logo(domain, site_folder)
            log.info(f"[{domain}] Logo: {logo_result['message']}")

            # 上传Icon
            if progress_callback:
                progress_callback("上传Icon...")
            icon_result = self.process_icon(domain, site_folder)
            log.info(f"[{domain}] Icon: {icon_result['message']}")

            # 上传Banner
            if progress_callback:
                progress_callback("上传Banner...")
            banner_result = self.process_banner(domain, site_folder)
            log.info(f"[{domain}] Banner: {banner_result['message']}")

            log.info(f"[{domain}] 媒体配置完成")
            return {"success": True, "message": "媒体配置完成"}

        except Exception as e:
            log.error(f"[{domain}] 媒体配置失败: {e}")
            return {"success": False, "message": str(e)}

    def clear_cache(self, domain: str) -> dict:
        """清理WP Rocket缓存

        Args:
            domain: 域名

        Returns:
            {"success": bool, "message": str}
        """
        log.info(f"[{domain}] 清理WP Rocket缓存...")
        site_url = f"https://www.{domain}"

        try:
            r = self.session.get(f"{site_url}/wp-admin/", timeout=15)
            if r.status_code != 200:
                return {"success": False, "message": "无法访问后台"}

            idx = r.text.find('purge_cache')
            if idx < 0:
                return {"success": False, "message": "未找到清理缓存按钮"}

            start = r.text.rindex('href="', 0, idx) + 6
            end = r.text.index('"', start)
            purge_url = r.text[start:end]
            if not purge_url.startswith("http"):
                purge_url = f"{site_url}/wp-admin/{purge_url}"
            purge_url = purge_url.replace('&amp;', '&')

            pr = self.session.get(purge_url, timeout=15)
            if pr.status_code in (200, 302):
                log.info(f"[{domain}] 缓存已清理")
                return {"success": True, "message": "缓存已清理"}

            return {"success": False, "message": f"清理失败: HTTP {pr.status_code}"}

        except Exception as e:
            log.error(f"[{domain}] 清理缓存失败: {e}")
            return {"success": False, "message": str(e)}

    def set_main_category(self, domain: str, category_name: str, progress_callback=None) -> dict:
        """设置主分类

        Args:
            domain: 域名
            category_name: 分类名称
            progress_callback: 进度回调

        Returns:
            {"success": bool, "message": str, "link": str}
        """
        if "|||" in category_name:
            category_name = category_name.rsplit("|||", 1)[-1].strip()

        log.info(f"[{domain}] 设置主分类: {category_name}")
        site_url = f"https://www.{domain}"

        if category_name.strip().lower() == "none":
            return {"success": True, "message": "已跳过", "link": ""}

        search_url = f"{site_url}/cf-updata/category/categorySearch.php"
        set_url = f"{site_url}/cf-updata/category/mainCategorySet.php"

        try:
            found_items = []
            already_main = False

            for page in range(1, 11):
                if progress_callback:
                    progress_callback(f"搜索第 {page} 页...")

                resp = request_with_retry(self.session, "POST", search_url,
                                           data={"page": str(page), "limit": "25", "category_name": category_name},
                                           headers={"User-Agent": "Mozilla/5.0"}, retries=2)
                if resp is None or resp.status_code != 200:
                    break

                try:
                    data = resp.json()
                except Exception:
                    break

                if data.get("code") != 0:
                    break

                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    if item.get("term_name") == category_name:
                        if item.get("is_main") == "是":
                            already_main = True
                        found_items.append(item)

                if len(items) < 25:
                    break

            if already_main:
                log.info(f"[{domain}] {category_name} 已是主分类")
                return {"success": True, "message": f"{category_name} 已是主分类", "link": ""}

            if len(found_items) == 0:
                return {"success": False, "message": f"未找到分类: {category_name}", "link": ""}

            if len(found_items) > 1:
                return {"success": False, "duplicates": True, "results": found_items,
                        "message": f"同名分类 {len(found_items)} 个，需手动选择", "link": ""}

            term_id = found_items[0]["term_id"]

            if progress_callback:
                progress_callback(f"设置分类 term_id={term_id}...")

            resp = request_with_retry(self.session, "POST", set_url,
                                       data={"term_id": term_id},
                                       headers={"User-Agent": "Mozilla/5.0"}, retries=2)
            if resp is None:
                return {"success": False, "message": "设置请求无响应", "link": ""}

            try:
                result = resp.json()
            except Exception:
                return {"success": False, "message": f"响应不是JSON: {resp.text[:200]}", "link": ""}

            if result.get("error"):
                msg = result.get("msg", ["设置失败"])
                return {"success": False, "message": str(msg), "link": ""}

            msg = result.get("msg", ["设置成功"])
            log.info(f"[{domain}] 主分类设置成功: {msg}")
            return {"success": True, "message": str(msg), "link": ""}

        except Exception as e:
            log.error(f"[{domain}] 设置主分类失败: {e}")
            return {"success": False, "message": str(e), "link": ""}

    def health_check(self, domain: str) -> dict:
        """健康检查

        Args:
            domain: 域名

        Returns:
            {"success": bool, "message": str, "status_code": int}
        """
        site_url = f"https://www.{domain}"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.get(site_url, timeout=15, allow_redirects=True)
                status_code = resp.status_code

                if status_code == 200:
                    if "wp-content" in resp.text or "wordpress" in resp.text.lower():
                        return {"success": True, "message": "站点正常", "status_code": status_code}
                    else:
                        return {"success": False, "message": "站点响应异常，可能未安装WordPress", "status_code": status_code}
                else:
                    return {"success": False, "message": f"HTTP {status_code}", "status_code": status_code}

            except requests.exceptions.SSLError as e:
                log.warning(f"[{domain}] 健康检查SSL错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return {"success": False, "message": f"SSL连接失败: {e}", "status_code": 0}
            except requests.exceptions.Timeout:
                return {"success": False, "message": "连接超时", "status_code": 0}
            except requests.exceptions.ConnectionError:
                return {"success": False, "message": "连接失败", "status_code": 0}
            except Exception as e:
                return {"success": False, "message": str(e), "status_code": 0}

        return {"success": False, "message": "健康检查失败，已重试多次", "status_code": 0}

    def close(self):
        """关闭会话"""
        if self._session:
            self._session.close()
            self._session = None


# 全局实例
_operator: Optional[SiteOperator] = None


def get_operator() -> SiteOperator:
    """获取全局操作器实例"""
    global _operator
    if _operator is None:
        _operator = SiteOperator()
    return _operator
