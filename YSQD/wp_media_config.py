import os
import re
import json
import urllib3

import requests
from bs4 import BeautifulSoup
from PIL import Image

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


ICON_NAME = "icon.png"
WP_BANNER_NAMES = ["banner.jpg", "banner.webp", "bannerstore.jpg", "banner-scaled.jpg"]
DEFAULT_MEDIA_ROOT = r"E:\logo"
MAX_ICON_SIZE = 5 * 1024 * 1024


class WPMediaConfigurator:
    def __init__(self, password, media_root=DEFAULT_MEDIA_ROOT):
        self._password = password
        self._media_root = media_root
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            }
        )

    def _resolve_domain_dir(self, domain):
        candidates = []
        seen = set()
        for root in [self._media_root, DEFAULT_MEDIA_ROOT, "media"]:
            if not root:
                continue
            domain_dir = os.path.join(root, domain)
            normalized = os.path.normcase(os.path.abspath(domain_dir))
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(domain_dir)

        for domain_dir in candidates:
            if os.path.isdir(domain_dir):
                return domain_dir
        raise RuntimeError(f"本地未找到站点素材目录: {domain}")

    def _find_icon_path(self, domain_dir):
        for root, _dirs, files in os.walk(domain_dir):
            if ICON_NAME not in files:
                continue
            icon_path = os.path.join(root, ICON_NAME)
            if os.path.getsize(icon_path) > MAX_ICON_SIZE:
                raise RuntimeError("本地未找到 icon.png 或文件过大（>5MB）")
            return icon_path
        raise RuntimeError("本地未找到 icon.png")

    def _request(self, method, url, **kwargs):
        for _ in range(3):
            try:
                resp = self._session.request(method, url, timeout=30, verify=False, **kwargs)
                if resp is not None and resp.status_code in (200, 201, 302):
                    return resp
            except requests.exceptions.RequestException:
                pass
        return None

    def _login(self, site):
        login_url = f"https://www.{site}/bbwllogin/"
        user = site.replace(".com", "").strip()
        data = {
            "log": f"Ad{user}min",
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"https://www.{site}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data, allow_redirects=True)
        if resp is None:
            raise RuntimeError("WP 登录请求失败")
        if any("wordpress_logged_in" in c.name for c in self._session.cookies):
            return True
        admin_check = self._request("GET", f"https://www.{site}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            return True
        raise RuntimeError("WP 登录失败")

    def _get_upload_nonce(self, site):
        url = f"https://www.{site}/wp-admin/media-new.php"
        resp = self._request("GET", url)
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

    def _upload_icon(self, site, icon_path, upload_nonce):
        url = f"https://www.{site}/wp-admin/async-upload.php"
        with open(icon_path, "rb") as f:
            files = {"async-upload": (ICON_NAME, f, "image/png")}
            data = {
                "action": "upload-attachment",
                "_wpnonce": upload_nonce,
                "_wp_http_referer": "/wp-admin/media-new.php",
                "name": ICON_NAME,
            }
            resp = self._request("POST", url, data=data, files=files)
        if not resp:
            raise RuntimeError("icon 上传失败")
        # 处理 UTF-8 BOM 问题
        text = resp.text
        if text.startswith('\ufeff'):
            text = text[1:]
        js = json.loads(text)
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
        raise RuntimeError("icon 上传失败")

    def _query_media(self, site, filename):
        ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
        data = {
            "action": "query-attachments",
            "post_id": 0,
            "query[post_mime_type]": "image",
            "query[orderby]": "date",
            "query[s]": filename,
            "query[order]": "DESC",
            "query[posts_per_page]": 80,
            "query[paged]": 1,
        }
        resp = self._request("POST", ajax_url, data=data)
        if not resp:
            return None
        try:
            # 处理 UTF-8 BOM 问题
            text = resp.text
            if text.startswith('\ufeff'):
                text = text[1:]
            js = json.loads(text)
            if js.get("data"):
                media = js["data"][0]
                return {
                    "id": media.get("id"),
                    "edit_nonce": media.get("nonces", {}).get("edit"),
                    "delete_nonce": media.get("nonces", {}).get("delete"),
                }
        except Exception:
            return None
        return None

    def _query_existing_banners(self, site):
        existing = []
        for name in WP_BANNER_NAMES:
            info = self._query_media(site, name)
            if info and info.get("id") and info.get("delete_nonce"):
                info["name"] = name
                existing.append(info)
        return existing

    def _delete_media(self, site, media_id, delete_nonce):
        ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
        data = {"action": "delete-post", "id": media_id, "_wpnonce": delete_nonce}
        resp = self._request("POST", ajax_url, data=data)
        return resp is not None and resp.status_code == 200

    def _crop_icon(self, site, media_id, crop_nonce):
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
        resp = self._request("POST", ajax_url, data=crop_data)
        if resp is None:
            return None
        try:
            # 处理 UTF-8 BOM 问题
            text = resp.text
            if text.startswith('\ufeff'):
                text = text[1:]
            js = json.loads(text)
            if js.get("success") and js.get("data", {}).get("id"):
                return js["data"]["id"]
        except Exception:
            pass
        return None

    def _save_wp_settings(self, site, icon_id):
        options_url = f"https://www.{site}/wp-admin/options-general.php"
        resp = self._request("GET", options_url)
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
            selected = select_tag.find("option", selected=True)
            form_data[name] = selected.get("value") if selected else ""
        for textarea_tag in form.find_all("textarea"):
            name = textarea_tag.get("name")
            if name:
                form_data[name] = textarea_tag.text
        form_data["site_icon"] = icon_id
        form_data["whl_page"] = "bbwllogin"
        save_url = f"https://www.{site}/wp-admin/options.php"
        resp2 = self._request("POST", save_url, data=form_data)
        return resp2 is not None and resp2.status_code in (200, 302)

    def _upload_banner(self, site, banner_path, upload_nonce, target_name):
        url = f"https://www.{site}/wp-admin/async-upload.php"
        with open(banner_path, "rb") as f:
            mime = "image/webp" if target_name.lower().endswith(".webp") else "image/jpeg"
            files = {"async-upload": (target_name, f, mime)}
            data = {
                "action": "upload-attachment",
                "_wpnonce": upload_nonce,
                "_wp_http_referer": "/wp-admin/media-new.php",
                "name": target_name,
            }
            resp = self._request("POST", url, data=data, files=files)
        if not resp:
            raise RuntimeError("banner 上传失败")
        # 处理 UTF-8 BOM 问题
        text = resp.text
        if text.startswith('\ufeff'):
            text = text[1:]
        js = json.loads(text)
        if js.get("success"):
            return True
        raise RuntimeError("banner 上传失败")

    def _convert_to_jpg(self, input_path, target_path):
        img = Image.open(input_path).convert("RGB")
        img.save(target_path, format="JPEG", quality=95)
        return target_path

    def _convert_to_webp(self, input_path, target_path):
        img = Image.open(input_path).convert("RGB")
        img.save(target_path, format="WEBP", quality=95)
        return target_path

    def configure(self, domain):
        self._login(domain)
        upload_nonce = self._get_upload_nonce(domain)
        if not upload_nonce:
            raise RuntimeError("获取上传 nonce 失败")

        domain_dir = self._resolve_domain_dir(domain)
        icon_path = self._find_icon_path(domain_dir)
        banner_jpg = os.path.join(domain_dir, "banner.jpg")
        banner_webp = os.path.join(domain_dir, "banner.webp")
        banner_png = os.path.join(domain_dir, "banner.png")

        # icon: 识别 -> 删除 -> 上传 -> 裁剪 -> 保存设置
        existing_icon = self._query_media(domain, ICON_NAME)
        if existing_icon and existing_icon.get("id") and existing_icon.get("delete_nonce"):
            self._delete_media(domain, existing_icon["id"], existing_icon["delete_nonce"])

        media_id, crop_nonce = self._upload_icon(domain, icon_path, upload_nonce)
        final_id = self._crop_icon(domain, media_id, crop_nonce) or media_id
        if not self._save_wp_settings(domain, final_id):
            raise RuntimeError("保存 WP 设置失败")

        # banner: 识别 -> 删除 -> 转换 -> 上传
        existing_banners = self._query_existing_banners(domain)
        target_name = None
        for item in existing_banners:
            name_lower = item["name"].lower()
            if name_lower == "banner-scaled.jpg":
                continue
            if name_lower.endswith(".webp"):
                target_name = item["name"]
                break
        if not target_name:
            for item in existing_banners:
                name_lower = item["name"].lower()
                if name_lower == "banner-scaled.jpg":
                    continue
                if name_lower.endswith(".jpg"):
                    target_name = item["name"]
                    break
        if not target_name:
            target_name = "banner.jpg"

        for item in existing_banners:
            self._delete_media(domain, item["id"], item["delete_nonce"])

        if target_name.lower().endswith(".webp"):
            if os.path.exists(banner_webp):
                banner_path = banner_webp
            elif os.path.exists(banner_jpg):
                banner_path = self._convert_to_webp(banner_jpg, os.path.join(domain_dir, "banner.webp"))
            elif os.path.exists(banner_png):
                banner_path = self._convert_to_webp(banner_png, os.path.join(domain_dir, "banner.webp"))
            else:
                raise RuntimeError("本地未找到 banner.webp/banner.jpg/banner.png")
        else:
            if os.path.exists(banner_jpg):
                banner_path = banner_jpg
            elif os.path.exists(banner_webp):
                banner_path = self._convert_to_jpg(banner_webp, os.path.join(domain_dir, "banner.jpg"))
            elif os.path.exists(banner_png):
                banner_path = self._convert_to_jpg(banner_png, os.path.join(domain_dir, "banner.jpg"))
            else:
                raise RuntimeError("本地未找到 banner.jpg/banner.webp/banner.png")

        self._upload_banner(domain, banner_path, upload_nonce, target_name)
        return True
