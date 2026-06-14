# integrated_wp_tool.py
from urllib.parse import quote
import os
import random
import json
import urllib
from pathlib import Path
import html
import os
import random
import sys
import time
import re
import argparse
import json
import urllib3
import requests
from bs4 import BeautifulSoup
from PIL import Image
from urllib.parse import urljoin
from urllib.parse import urlparse, parse_qs

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

'''
1-6-更新 完成
'''

# ---------- CONFIG ----------
# BASE_DIR = r"D:\web\11-3周\1"  # <-- 修改为你的目录或通过命令行覆盖
SUCCESS_LOG = "success.log"
FAILED_LOG = "failed.log"
DEFAULT_PASSWORD = os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")
ICON_NAME = "head.png"
WP_BANNER_NAMES = ["banner.jpg", "banner.webp", "bannerstore.jpg", "banner-scaled.jpg"]
MOBAN_JSON_PATH = r"return_shipping_moban"   # 退货与运输政策 json 文件夹路径
MAX_KB = 300
LOGIN_COOKIES = {}


def filter_cookies_by_keyword(keyword, cookie_dict=LOGIN_COOKIES):
    """
    根据关键字筛选cookie字典

    Args:
        cookie_dict: cookie字典
        keyword: 要筛选的关键字

    Returns:
        包含指定关键字的键值对字典
    """
    # 使用字典推导式筛选包含关键字的键
    return {key: value for key, value in cookie_dict.items() if keyword in key}

# ---------- UTIL ----------
def write_success(site, action, extra=""):
    with open(SUCCESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t成功\t{extra}\n")
    print(f"✅ [{site}] {action} 成功 {extra}")


def write_failed(site, action, reason=""):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t失败\t{reason}\n")
    print(f"❌ [{site}] {action} 失败: {reason}")


def request_with_retry(session, method, url, retries=3, delay=5, verify_ssl=False, **kwargs):    # 带重试机制的 HTTP 请求函数
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=120, verify=verify_ssl, **kwargs)
            if resp is not None and resp.status_code in (200, 201):
                return resp
            else:
                print(f"⚠️ 状态码 {getattr(resp, 'status_code', None)} 第 {i + 1}/{retries} 次重试: {url}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ 请求异常: {e}，{method} {url}，第 {i + 1}/{retries} 次重试")
        time.sleep(delay)
    return None


# ---------- AUTH ----------
def login(site, session=None, password=None):  # 功能：登录 WordPress 后台
    if password is None:
        password = DEFAULT_PASSWORD
    if session is None:
        session = requests.Session()
    login_url = f"https://www.{site}/bbwllogin/"  # 登录界面
    name = site.replace('.com', '').strip()
    data = {
        'log': f'Ad{name}min',
        'pwd': password,
        'wp-submit': 'Log In',
        'redirect_to': f"https://www.{site}/wp-admin/",
        'testcookie': '1'
    }
    try:
        resp = session.post(login_url, data=data, allow_redirects=True, verify=False, timeout=20)  # 提交 POST 请求
    except Exception as e:
        raise RuntimeError(f"登录请求异常: {e}")
    if any("wordpress_logged_in" in c.name for c in session.cookies):  # 检查是否获得 cookie "wordpress_logged_in"
        for cookie in session.cookies:
                LOGIN_COOKIES[cookie.name] = cookie.value
        return session
    try:
        admin_check = session.get(f"https://www.{site}/wp-admin/", verify=False,
                                  timeout=15)  # 若无 cookie，则访问 /wp-admin/ 检查登录是否成功
        if admin_check is not None and admin_check.status_code == 200:
            return session
    except Exception:
        pass
    raise RuntimeError("登录失败：未检测到登录 cookie 或无法访问 /wp-admin/")


# ---------- LOGO FUNCTIONS ----------
def query_existing_logo(session, site, request_fn):
    """
    查询现有的 logo 图片
    """
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    data = {
        "action": "query-attachments",
        "post_id": 0,
        "query[post_mime_type]": "image",
        "query[orderby]": "date",
        "query[s]": "logo.png",  # 搜索 logo.png
        "query[order]": "DESC",
        "query[posts_per_page]": 80,
        "query[paged]": 1
    }
    resp = request_fn(session, "POST", ajax_url, data=data)
    if not resp:
        return None, None
    try:
        js = resp.json()
        if js and js.get("data"):
            # 查找确切的 logo.png
            for media in js["data"]:
                if media.get("filename") == "logo.png" or "logo.png" in media.get("url", ""):
                    return media.get("id"), media.get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ query_existing_logo JSON 解析出错: {e}")
    return None, None


def get_last_logo_path(domain_folder):
    """
    在网站文件夹中寻找 logo.png
    """
    logo_name = "logo.png"
    for root, dirs, files in os.walk(domain_folder):
        if logo_name in files:
            return os.path.join(root, logo_name)
    return None


def upload_logo(session, site, logo_path, upload_nonce):
    """
    上传 logo 图片
    """
    upload_url = f"https://www.{site}/wp-admin/async-upload.php"
    with open(logo_path, "rb") as f:
        files = {"async-upload": ("logo.png", f, "image/png")}
        data = {
            "action": "upload-attachment",
            "_wpnonce": upload_nonce,
            "_wp_http_referer": "/wp-admin/media-new.php",
            "name": "logo.png"
        }
        headers = {
            "Accept": "*/*",
            "Origin": f"https://www.{site}",
            "Referer": f"https://www.{site}/wp-admin/media-new.php",
            "User-Agent": "Mozilla/5.0"
        }
        resp = session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)

    if resp is None:
        return None, None
    try:
        js = resp.json()
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ upload_logo JSON 解析出错: {e}")
    return None, None

def process_logo(site_folder, session, request_fn):
    """
    综合执行：查询现有 logo → 若无则上传 → 写日志
    """
    site = os.path.basename(site_folder)
    print(f"--- LOGO: {site}")

    try:
        # ① 查询现有 logo
        media_id, _ = query_existing_logo(session, site, request_fn)
        if media_id:
            print(f"已有 logo，id={media_id}，直接使用现有图片")
            write_success(site, "LOGO", f"使用现有logo id={media_id}")
            return True
        else:
            # ② 获取本地 logo.png
            logo_path = get_last_logo_path(site_folder)
            if not logo_path:
                raise RuntimeError("本地未找到 logo.png")

            # ③ 获取上传 nonce
            upload_nonce = get_upload_nonce(session, site, request_fn)
            if not upload_nonce:
                raise RuntimeError("获取上传 _wpnonce 失败")

            # ④ 上传 logo
            media_id, _ = upload_logo(session, site, logo_path, upload_nonce)
            if not media_id:
                raise RuntimeError("上传 logo 失败")

            # ⑤ 写入成功日志
            write_success(site, "LOGO", f"上传成功 id={media_id}")
            return True

    except Exception as e:
        write_failed(site, "LOGO", str(e))
        return False

# ---------- ICON FUNCTIONS ----------      ICON（网站图标）模块    上传并设置 WordPress 后台的站点图标
def get_upload_nonce(session, site, request_fn):  # 访问 /wp-admin/media-new.php，提取 _wpnonce（WordPress 的安全令牌）。
    url = f"https://www.{site}/wp-admin/media-new.php"
    resp = request_fn(session, "GET", url)
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


def query_existing_icon(session, site,      # 搜索角标
                        request_fn):  # 通过 AJAX 请求 admin-ajax.php?action=query-attachments 搜索 head.png 是否已存在。
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    data = {
        "action": "query-attachments",
        "post_id": 0,
        "query[post_mime_type]": "image",
        "query[orderby]": "date",
        "query[s]": ICON_NAME,
        "query[order]": "DESC",
        "query[posts_per_page]": 80,
        "query[paged]": 1
    }
    resp = request_fn(session, "POST", ajax_url, data=data)
    if not resp:
        return None, None
    try:
        js = resp.json()
        if js and js.get("data"):
            media = js["data"][0]    # # 取第一个匹配的媒体文件
            return media.get("id"), media.get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ query_existing_icon JSON 解析出错: {e}")
    return None, None

def query_existing_image_name(session, site, image_name,      # 搜索角标
                        request_fn):  # 通过 AJAX 请求 admin-ajax.php?action=query-attachments 搜索 head.png 是否已存在。
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    data = {
        "action": "query-attachments",
        "post_id": 0,
        "query[post_mime_type]": "image",
        "query[orderby]": "date",
        "query[s]": image_name,
        "query[order]": "DESC",
        "query[posts_per_page]": 80,
        "query[paged]": 1
    }
    resp = request_fn(session, "POST", ajax_url, data=data)
    if not resp:
        return None, None
    try:
        js = resp.json()
        if js and js.get("data"):
            media = js["data"][0]    # # 取第一个匹配的媒体文件
            return media.get("id"), media.get("url")
    except Exception as e:
        print(f"⚠️ query_existing_icon JSON 解析出错: {e}")
    return None, None


def get_last_icon_path(domain_folder):  # 在网站文件夹中寻找 head.png。    传入的是域名路径
    for root, dirs, files in os.walk(domain_folder):
        if ICON_NAME in files:
            return os.path.join(root, ICON_NAME)
    return None


def upload_icon(session, site, icon_path, upload_nonce):  # 上传 icon 发送 POST 请求到 async-upload.php，附带文件。
    upload_url = f"https://www.{site}/wp-admin/async-upload.php"  # 返回 media_id 和裁剪 nonce。
    with open(icon_path, "rb") as f:
        files = {"async-upload": (ICON_NAME, f, "image/png")}
        data = {
            "action": "upload-attachment",
            "_wpnonce": upload_nonce,
            "_wp_http_referer": "/wp-admin/media-new.php",
            "name": ICON_NAME
        }
        headers = {"Accept": "*/*", "Origin": f"https://www.{site}",
                   "Referer": f"https://www.{site}/wp-admin/media-new.php", "User-Agent": "Mozilla/5.0"}
        resp = session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)
        # print("图片上传响应json:", resp.json())
    if resp is None:
        return None, None
    try:
        js = resp.json()
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ upload_icon JSON 解析出错: {e}")
    return None, None

def upload_image(session, site, image_path, upload_nonce):  # 上传 icon 发送 POST 请求到 async-upload.php，附带文件。
    upload_url = f"https://www.{site}/wp-admin/async-upload.php"  # 返回 media_id 和裁剪 nonce。
    with open(image_path, "rb") as f:
        files = {"async-upload": (os.path.basename(image_path), f, "image/png")}
        data = {
            "action": "upload-attachment",
            "_wpnonce": upload_nonce,
            "_wp_http_referer": "/wp-admin/media-new.php",
            "name": os.path.basename(image_path),
        }
        headers = {"Accept": "*/*", "Origin": f"https://www.{site}",
                   "Referer": f"https://www.{site}/wp-admin/media-new.php", "User-Agent": "Mozilla/5.0"}
        resp = session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)
        # print("图片上传响应json:", resp.json())    # 可以直接返回响应体 得到 ID 和 链接
    if resp is None:
        return None, None
    try:
        js = resp.json()
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"]["url"]
    except Exception as e:
        print(f"⚠️ upload_icon JSON 解析出错: {e}")
    return None, None




def crop_icon(session, site, media_id,
              crop_nonce):  # 裁剪 icon 调用 admin-ajax.php?action=crop-image 接口裁剪图标（WordPress 要求的步骤）。
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    crop_data = {
        "_wpnonce": crop_nonce,
        "id": media_id,
        "context": "site-icon",
        "cropDetails[x1]": 0, "cropDetails[y1]": 0,
        "cropDetails[x2]": 50, "cropDetails[y2]": 50,
        "cropDetails[width]": 50, "cropDetails[height]": 50,
        "cropDetails[dst_width]": 512, "cropDetails[dst_height]": 512,
        "action": "crop-image"
    }
    headers = {"Referer": f"https://www.{site}/wp-admin/media-new.php", "User-Agent": "Mozilla/5.0"}
    resp = session.post(ajax_url, data=crop_data, headers=headers, verify=False, timeout=20)
    if resp and resp.status_code == 200:
        try:
            js = resp.json()
            if js.get("success") and js.get("data", {}).get("id"):
                return js["data"]["id"]
        except Exception:
            pass
    return None


# 读取 options-general.php，解析表单，再带上新 site_icon，回传到 options.php 保存。
def save_wp_settings(session, site, request_fn, site_icon_id=None, date_format=None, time_format=None,
                     week_starts_on=None):
    options_url = f"https://www.{site}/wp-admin/options-general.php"
    resp = request_fn(session, "GET", options_url)
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
    resp2 = session.post(save_url, data=form_data, headers=headers, verify=False, allow_redirects=True, timeout=25)
    return resp2 is not None and resp2.status_code in (200, 302)


# 综合执行 查询现有图标 → 上传 → 裁剪 → 保存 → 写日志。
def process_icon(site_folder, session, request_fn, date_format="F j, Y", time_format="g:i a", week_starts_on=1):
    """
    综合执行：查询现有图标 → 若无则上传 → 保存 → 写日志（不再执行裁剪）
    """
    site = os.path.basename(site_folder)
    print(f"--- ICON: {site}")

    # ① 查询现有图标
    media_id, _ = query_existing_icon(session, site, request_fn)
    if media_id:
        print(f"已有 icon，id={media_id}，直接使用现有图标")
    else:
        # ② 获取本地 icon.png
        icon_path = get_last_icon_path(site_folder)
        if not icon_path:
            raise RuntimeError("本地未找到 icon.png")

        # ③ 获取上传 nonce
        upload_nonce = get_upload_nonce(session, site, request_fn)
        if not upload_nonce:
            raise RuntimeError("获取上传 _wpnonce 失败")

        # ④ 上传图标（不裁剪）
        media_id, _ = upload_icon(session, site, icon_path, upload_nonce)
        if not media_id:
            raise RuntimeError("上传 icon 失败")

    # ⑤ 保存设置（直接用 media_id）
    ok = save_wp_settings(
        session,
        site,
        request_fn,
        site_icon_id=media_id,
        date_format=date_format,
        time_format=time_format,
        week_starts_on=week_starts_on
    )
    if not ok:
        raise RuntimeError("保存 WP 设置失败")

    # ⑥ 写入成功日志
    write_success(site, "ICON", f"id={media_id}")
    return True


# ---------- WP ROCKET FUNCTIONS ----------
def process_rocket(site, session, request_fn):
    """
    设置 WP Rocket 插件：激活并配置缓存设置
    """
    print(f"--- WP ROCKET: {site}")

    try:
        # 获取插件页面
        wp_url = f'https://www.{site}/wp-admin/plugins.php'
        wp_response = request_fn(session, "GET", wp_url)
        if not wp_response or wp_response.status_code != 200:
            raise RuntimeError(f"无法访问插件页面，状态码: {getattr(wp_response, 'status_code', '未知')}")

        # 解析插件页面
        soup = BeautifulSoup(wp_response.text, 'html.parser')

        # 尝试激活 WP Rocket 插件
        activate_element = soup.find('a', {'id': 'activate-wp-rocket'})
        if activate_element and activate_element.get('href'):
            activate_url = activate_element.get('href')
            if not activate_url.startswith('http'):
                activate_url = f'https://www.{site}/wp-admin/{activate_url}'

            activate_response = request_fn(session, "GET", activate_url)
            if activate_response and activate_response.status_code == 200:
                print(f'🚀 已激活 WP Rocket: {site}')
            else:
                print(f'⚠️ WP Rocket 激活请求可能失败: {site}')
        else:
            print(f'🎉 WP Rocket 已激活: {site}')

        # 获取 WP Rocket 设置页面
        setting_url = f'https://www.{site}/wp-admin/options-general.php?page=wprocket'
        st_response = request_fn(session, "GET", setting_url)
        if not st_response or st_response.status_code != 200:
            raise RuntimeError(f"无法访问 WP Rocket 设置页面，状态码: {getattr(st_response, 'status_code', '未知')}")

        # 解析设置页面，提取必要的字段
        nonce_soup = BeautifulSoup(st_response.text, 'html.parser')

        try:
            # 提取各种必要的隐藏字段
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

            wplicense = nonce_soup.find('input', {'id': 'license'})
            wplicense = wplicense.get('value') if wplicense else ""

        except Exception as e:
            raise RuntimeError(f"解析 WP Rocket 设置字段失败: {str(e)}")

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

        # 提交表单，更新设置
        option_url = f'https://www.{site}/wp-admin/options.php'
        st_response = request_fn(session, "POST", option_url, data=setting_data)

        if st_response and st_response.status_code == 200:
            write_success(site, "WP_ROCKET", "缓存设置已配置")
            print(f'------------------✅ ✅ ✅成功设置 WP Rocket: {site} ✅ ✅ ✅------------------')
            return True
        else:
            raise RuntimeError(f"设置失败，状态码: {getattr(st_response, 'status_code', '未知')}")

    except Exception as e:
        write_failed(site, "WP_ROCKET", str(e))
        return False


# 任务：替换网站的首页 banner 图片。
# ---------- BANNER FUNCTIONS ----------
def compress_image_to_max_size(input_path, max_kb=MAX_KB):
    """压缩图片到指定大小以下"""
    img = Image.open(input_path)
    quality = 95
    temp_path = input_path

    # 如果是临时文件，创建新的临时文件
    if "temp_" not in os.path.basename(input_path):
        base_name = os.path.splitext(input_path)[0]
        temp_path = f"{base_name}_temp.jpg"

    while True:
        img.save(temp_path, "JPEG", quality=quality, optimize=True)
        size_kb = os.path.getsize(temp_path) / 1024

        if size_kb <= max_kb or quality <= 10:
            break

        quality -= 5

    return temp_path


def convert_image_format(input_path, output_ext, target_path):
    img = Image.open(input_path).convert("RGB")
    if output_ext == "jpg":
        img.save(target_path, format="JPEG", quality=95)
    elif output_ext == "webp":
        img.save(target_path, format="WEBP", quality=95)
    elif output_ext == "png":
        img.save(target_path, format="PNG", quality=95)
    return target_path


def get_banner_upload_nonce(session, site, request_fn):
    return get_upload_nonce(session, site, request_fn)


def query_existing_banners(session, site, request_fn):
    """查询现有的 banner 图片"""
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    existing = {}

    # 使用域名作为文件名搜索
    domain_name = site.replace('.com', '').strip()
    banner_filename = f"{domain_name}.jpg"

    data = {
        "action": "query-attachments",
        "post_id": 0,
        "query[post_mime_type]": "image",
        "query[s]": banner_filename,
        "query[posts_per_page]": 40
    }
    resp = request_fn(session, "POST", ajax_url, data=data)
    if not resp:
        return existing

    try:
        js = resp.json()
        if js.get("data"):
            for media in js["data"]:
                media_id = media.get("id")
                delete_nonce = media.get("nonces", {}).get("delete")
                if media_id and delete_nonce:
                    existing[banner_filename] = {"id": media_id, "delete_nonce": delete_nonce}
    except Exception:
        pass

    return existing


def delete_banner(session, site, name, media_id, delete_nonce):
    """删除现有的 banner 图片"""
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    data = {"action": "delete-post", "id": media_id, "_wpnonce": delete_nonce}
    resp = session.post(ajax_url, data=data, verify=False, timeout=20)
    return resp is not None and resp.status_code == 200


def upload_banner(session, site, local_path, target_name, upload_nonce):
    """上传 banner 图片"""
    upload_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    with open(local_path, "rb") as f:
        mime_type = "image/jpeg"  # 统一使用 jpg 格式
        files = {"async-upload": (target_name, f, mime_type)}
        data = {"action": "upload-attachment", "_wpnonce": upload_nonce, "_wp_http_referer": "/wp-admin/media-new.php"}
        resp = session.post(upload_url, data=data, files=files, verify=False, timeout=30)
    if not resp:
        return None
    try:
        js = resp.json()
        if js.get("success") and "data" in js:
            return js["data"].get("url", "")
    except Exception:
        pass
    return None


def ensure_jpg_format_and_size(input_path, max_kb=MAX_KB):
    """
    确保图片为 JPG 格式且小于指定大小
    返回处理后的图片路径
    """
    # 如果是临时文件，直接使用
    if "temp_" in os.path.basename(input_path):
        return input_path

    img = Image.open(input_path)

    # 如果图片不是 JPG 格式，先转换为 JPG
    if img.format != "JPEG":
        # 创建临时文件路径
        base_name = os.path.splitext(input_path)[0]
        temp_jpg_path = f"{base_name}_temp.jpg"

        # 转换为 RGB 模式（避免 RGBA 问题）
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # 保存为 JPG
        img.save(temp_jpg_path, "JPEG", quality=95)
        input_path = temp_jpg_path

    # 检查并压缩大小
    size_kb = os.path.getsize(input_path) / 1024
    if size_kb > max_kb:
        input_path = compress_image_to_max_size(input_path, max_kb)

    return input_path

def get_last_banner_path(domain_folder):
    """在网站文件夹中寻找 banner 图片"""
    domain_name = os.path.basename(domain_folder).replace('.com', '').strip()

    # 支持的图片格式
    supported_formats = ['.jpg', '.jpeg', '.png', '.webp']

    # 首先查找域名命名的 banner（各种格式）
    for ext in supported_formats:
        banner_filename = f"{domain_name}{ext}"
        for root, dirs, files in os.walk(domain_folder):
            if banner_filename in files:
                return os.path.join(root, banner_filename)

    # 如果找不到，使用默认的 banner（各种格式）
    for ext in supported_formats:
        default_banner = f"banner{ext}"
        for root, dirs, files in os.walk(domain_folder):
            if default_banner in files:
                return os.path.join(root, default_banner)

    return None


def process_banner(site_folder, session, request_fn):
    """
    综合执行：查询现有 banner → 删除旧 banner → 上传新 banner
    确保上传的 banner 为 JPG 格式且小于 300KB
    """
    site = os.path.basename(site_folder)
    print(f"--- BANNER: {site}")

    try:
        # ① 获取上传 nonce
        upload_nonce = get_banner_upload_nonce(session, site, request_fn)
        if not upload_nonce:
            raise RuntimeError("获取 banner 上传 nonce 失败")

        # ② 查询现有 banner
        existing = query_existing_banners(session, site, request_fn)

        # ③ 删除现有的 banner
        for name, info in existing.items():
            try:
                deleted = delete_banner(session, site, name, info["id"], info["delete_nonce"])
                if deleted:
                    print(f"已删除旧 banner: {name}")
            except Exception as e:
                print(f"删除旧 banner 失败: {e}")

        # ④ 获取本地 banner 图片
        banner_path = get_last_banner_path(site_folder)
        if not banner_path:
            raise RuntimeError("本地未找到 banner 图片")

        print(f"找到本地 banner: {banner_path}")

        # ⑤ 确保图片为 JPG 格式且小于 300KB
        processed_banner_path = ensure_jpg_format_and_size(banner_path, MAX_KB)
        print(f"处理后的 banner: {processed_banner_path} (大小: {os.path.getsize(processed_banner_path) / 1024:.1f}KB)")

        # ⑥ 准备上传的目标文件名（使用域名）
        domain_name = site.replace('.com', '').strip()
        target_filename = f"{domain_name}.jpg"

        # ⑦ 上传 banner
        banner_url = upload_banner(session, site, processed_banner_path, target_filename, upload_nonce)
        if not banner_url:
            raise RuntimeError(f"上传 banner 失败: {target_filename}")

        # ⑧ 清理临时文件
        if processed_banner_path != banner_path and "temp_" in processed_banner_path:
            try:
                os.remove(processed_banner_path)
            except Exception:
                pass

        # ⑨ 写入成功日志
        write_success(site, "BANNER", f"{target_filename} -> {banner_url}")
        return True

    except Exception as e:
        write_failed(site, "BANNER", str(e))
        return False


# Yoast SEO 模块   SEO 插件自动化配置的核心逻辑。
# ---------- YOAST FUNCTIONS ----------
def get_yoast_nonce(session, site, request_fn, dump_on_fail=True):
    """
    更稳健抓取 Yoast / WP REST nonce（不使用 hash URL 单独解析前端渲染内容）
    """
    candidates = [
        f"https://www.{site}/wp-admin/admin.php?page=wpseo_dashboard",
        f"https://www.{site}/wp-admin/index.php",
        f"https://www.{site}/wp-admin/"
    ]
    html = None
    for url in candidates:
        resp = request_fn(session, "GET", url)
        if resp and resp.status_code == 200 and resp.text:
            html = resp.text
            break
    if not html:
        resp = request_fn(session, "GET",
                          f"https://www.{site}/wp-admin/admin.php?page=wpseo_dashboard#/first-time-configuration")
        if resp and resp.status_code == 200:
            html = resp.text
    if not html:
        if dump_on_fail:
            print(f"❌ 无法获取 Yoast 页面 HTML: {site}")
        return None
    patterns = [
        r'wpApiSettings["\']?\s*[:=]\s*{[^}]*?["\']nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
        r'window\.wpApiSettings\s*=\s*{[^}]*?["\']nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
        r'nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']',
        r'X-WP-Nonce["\']?\s*[:=]\s*["\']([a-zA-Z0-9\-_]+)["\']',
        r'window\._wpnonce\s*=\s*["\']([a-zA-Z0-9\-_]+)["\']',
        r'data-wp-nonce=["\']([a-zA-Z0-9\-_]+)["\']',
        r'data-nonce=["\']([a-zA-Z0-9\-_]+)["\']',
        r'wp_localize_script\([^\)]*?["\']wpApiSettings["\'].*?["\']nonce["\']\s*\,\s*["\']([a-zA-Z0-9\-_]+)["\']',
    ]
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or ""
        for pat in patterns:
            m = re.search(pat, text, flags=re.S)
            if m:
                return m.group(1)
    for pat in patterns:
        m = re.search(pat, html, flags=re.S)
        if m:
            return m.group(1)
    m = re.search(r'wpApiSettings\s*=\s*({.*?})\s*;', html, flags=re.S)
    if m:
        try:
            obj_text = m.group(1)
            m2 = re.search(r'["\']nonce["\']\s*:\s*["\']([a-zA-Z0-9\-_]+)["\']', obj_text)
            if m2:
                return m2.group(1)
        except Exception:
            pass
    if dump_on_fail:
        try:
            fn = f"debug_yoast_{site.replace('.', '_')}.html"
            with open(fn, "w", encoding="utf-8") as fw:
                fw.write(html[:200000])
            print(f"🔍 未找到 nonce，页面已保存至 {fn} 供人工检查")
        except Exception as e:
            print(f"⚠️ 写入调试文件失败: {e}")
    return None


def activate_plugin_by_slug(session, site, plugin_identifier):
    """
    在 plugins.php 页面找激活链接并触发激活（更通用）
    plugin_identifier 例如: 'wordpress-seo/wp-seo.php' 或 'yoast-seo-premium/yoast-seo-premium.php'
    """
    plugins_url = f"https://www.{site}/wp-admin/plugins.php"
    resp = session.get(plugins_url, verify=False, timeout=20)
    if not resp or resp.status_code != 200:
        return False
    soup = BeautifulSoup(resp.text, "html.parser")
    # 优先在 href 中找包含 activate 与插件标识的链接
    for a in soup.find_all("a", href=True):
        href = a['href']
        if "activate" in href.lower() and plugin_identifier in href:
            activate_href = href
            activate_url = activate_href if activate_href.startswith("http") else urljoin(
                f"https://www.{site}/wp-admin/", activate_href)
            try:
                session.get(activate_url, verify=False, timeout=20)
            except Exception:
                pass
            return True
    # 退而求其次：找 tr[data-plugin] 的 activate 链接
    rows = soup.select("tr[data-plugin]")
    for r in rows:
        data_plugin = r.get("data-plugin", "")
        if plugin_identifier in data_plugin or plugin_identifier.split("/")[-1] in data_plugin:
            a = r.find("a", class_="activate")
            if a and a.get("href"):
                href = a.get("href")
                activate_url = href if href.startswith("http") else urljoin(f"https://www.{site}/wp-admin/", href)
                try:
                    session.get(activate_url, verify=False, timeout=20)
                except Exception:
                    pass
                return True
    # 最后检查是否已有 deactivate 链接（表示已激活）
    for a in soup.find_all("a", href=True):
        if "deactivate" in a['href'].lower() and (
                plugin_identifier in a['href'] or plugin_identifier.split("/")[-1] in a['href']):
            return True
    return False


def activate_yoast_optimize(session, site, nonce, request_fn):    #  request_fn   带重试机制的 HTTP 请求函数       nonce  权限
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user"   # 保存后台 SEO 设置   Yoast 会通过这个 REST API 请求将配置写入数据库
    payload = {"finishedSteps": ["optimizeSeoData","siteRepresentation","socialProfiles","personalPreferences"]}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce,"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"}
    resp = request_fn(session, "POST", url, json=payload, headers=headers,cookies=LOGIN_COOKIES)
    return resp is not None

def get_logo_id(session, site, request_fn, logo_filename="logo.png"):
    url = f"https://www.{site}/wp-json/wp/v2/media?search={logo_filename}"
    resp = request_fn(session, "GET", url)
    if not resp:
        return 0
    try:
        media_list = resp.json()
        for media in media_list:
            if logo_filename in media.get("source_url", ""):
                return media.get("id", 0)
    except Exception:
        pass
    return 0


def set_yoast_site_representation(session, site, nonce, request_fn):
    logo_id = get_logo_id(session, site, request_fn, "logo.png")
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/site_representation?_locale=user"
    payload = {
        "company_or_person": "company",
        "company_name": site,
        "company_logo": f"https://www.{site}/wp-content/uploads/logo.png",
        "company_logo_id": logo_id,
        "person_logo": "",
        "person_logo_id": 0,
        "website_name": site
    }
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers,cookies=filter_cookies_by_keyword(keyword="wordpress_logged_in"))
    return resp is not None


def save_yoast_configuration_state(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user"
    payload = {"finishedSteps": ["optimizeSeoData", "siteRepresentation", "socialProfiles", "personalPreferences"]}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers,cookies=filter_cookies_by_keyword(keyword="wordpress_logged_in"))
    return resp is not None


def set_yoast_social_profiles(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/social_profiles?_locale=user"
    payload = {"facebook_site": "", "twitter_site": "", "other_social_urls": []}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers,cookies=filter_cookies_by_keyword(keyword="wordpress_logged_in"))
    return resp is not None


def set_yoast_tracking(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/enable_tracking?_locale=user"
    payload = {"tracking": 0}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers,cookies=filter_cookies_by_keyword(keyword="wordpress_logged_in"))
    return resp is not None


def process_yoast(site, session, request_fn):
    print(f"--- YOAST: {site}")
    try:
        # 激活插件页面
        wp_url = f"https://www.{site}/wp-admin/plugins.php"
        resp = request_fn(session, "GET", wp_url)
        if not resp:
            raise RuntimeError("无法访问插件页面")
        soup = BeautifulSoup(resp.text, "html.parser")

        # 激活 Free
        try:
            activate_free = soup.find('a', {'id': 'activate-wordpress-seo'})
            if activate_free and activate_free.get('href'):
                session.get(f"https://www.{site}/wp-admin/{activate_free['href']}", verify=False, timeout=20)
                print(f"🚀 尝试激活 Yoast SEO (Free) : {site}")
            else:
                print(f"🎉 SEO 已激活: {site}")
        except Exception:
            print(f"🎉 SEO 已激活: {site}")

        # 激活 Premium
        try:
            activate_premium = soup.find('a', {'id': 'activate-yoast-seo-premium'})
            if activate_premium and activate_premium.get('href'):
                session.get(f"https://www.{site}/wp-admin/{activate_premium['href']}", verify=False, timeout=20)
                print(f"🚀 尝试激活 Yoast SEO Premium: {site}")
            else:
                print(f"🎉 SEO Premium 已激活: {site}")
        except Exception:
            print(f"🎉 SEO Premium 已激活: {site}")

        # 获取 nonce
        nonce = get_yoast_nonce(session, site, request_fn)
        if not nonce:
            raise RuntimeError("获取 Yoast nonce 失败")
        # 设置失败标志
        all_ok = True
        # 执行每个步骤，并单独打印成功/失败
        if activate_yoast_optimize(session, site, nonce, request_fn):
            print(f"✅ 已标记 {site} 的 Yoast SEO 数据优化为完成")
        else:
            print(f"❌ {site} Yoast 数据优化失败")
            all_ok = False

        if set_yoast_site_representation(session, site, nonce, request_fn):
            print(f"✅ 已设置 {site} 的 Yoast 站点信息")
        else:
            print(f"❌ {site} Yoast 站点信息设置失败")
            all_ok = False

        if save_yoast_configuration_state(session, site, nonce, request_fn):
            print(f"✅ 已保存 {site} 的完成步骤")
        else:
            print(f"❌ {site} 保存完成步骤失败")
            all_ok = False

        if set_yoast_social_profiles(session, site, nonce, request_fn):
            print(f"✅ 已设置 {site} 的 Yoast 社交媒体信息")
        else:
            print(f"❌ {site} 设置社交媒体信息失败")
            all_ok = False

        if set_yoast_tracking(session, site, nonce, request_fn):
                print(f"✅ 已关闭 {site} 的 Yoast 追踪功能")
        else:
            print(f"❌ {site} 设置 tracking 失败")
            all_ok = False

        # 最终判定
        if all_ok:
            write_success(site, "YOAST", "")
        else:
            write_failed(site, "YOAST", "部分步骤执行失败")
    except Exception as e:
        write_failed(site, "YOAST", str(e))

def get_site_blogdescription(site, session, request_fn):  # 获取站点描述

    resp = request_fn(session, "GET" ,f"https://www.{site}/wp-admin/options-general.php")
    if not resp:
        print(f"❌ 站点{site}访问设置角标页面出现问题，无法获取站点描述！")
        return None

    try:
        # 2. 解析HTML
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 3. 查找name="blogdescription"的input标签
        blogdescription_input = soup.find('input', {'name': 'blogdescription'})

        if blogdescription_input:
            # 获取value属性值
            description_value = blogdescription_input.get('value', '')
            print(f"✅ 成功找到站点{site}描述!")
            return description_value
        else:
            # 可能页面结构不同，尝试其他选择器
            print(f"❌ 未找到站点{site}的描述，设置SEO第二步可能会丢失信息!")
            return None

    except Exception as e:
        print(f"解析页面时出错: {str(e)}")
        return None

def get_last_image_path(domain_folder,image_name):  # 在网站文件夹中寻找 head.png。    传入的是域名路径
    for root, dirs, files in os.walk(domain_folder):
        if image_name in files:
            return os.path.join(root, image_name)
    return None

def get_logo_id_or_upload(site_folder,session, site, request_fn):
    # ① 查询现有图标
    media_id, media_url = query_existing_image_name(session, site, "logo-2.png", request_fn)
    if media_id:
        print(f"🎉 已有 logo，id={media_id}，直接使用现有图标")
    else:
        # ② 获取本地 icon.png
        logo_path = get_last_image_path(site_folder,"logo.png")
        if not logo_path:
            raise RuntimeError("❌ 本地未找到 icon.png")

        # ③ 获取上传 nonce
        upload_nonce = get_upload_nonce(session, site, request_fn)
        if not upload_nonce:
            raise RuntimeError("❌ 获取上传 _wpnonce 失败")

        print("logo_path:",logo_path)

        # ④ 上传图标（不裁剪）
        media_id, media_url = upload_image(session, site, logo_path, upload_nonce)
        if not media_id:
            raise RuntimeError("❌ 上传 icon 失败")
    return media_id,media_url


def clean_json_string(json_str):
    """
    清理JSON字符串，修复常见问题

    参数:
        json_str (str): 需要清理的JSON字符串

    返回:
        str: 清理后的JSON字符串
    """
    # 移除尾随的逗号
    json_str = re.sub(r',\s*(\}|\])', r'\1', json_str)
    # 移除开头的逗号
    json_str = re.sub(r'^\s*,', '', json_str)
    # 将单引号替换为双引号（但排除转义的单引号）
    json_str = re.sub(r"(?<!\\)'", '"', json_str)
    # 修复可能的多余逗号
    json_str = re.sub(r',(\s*,)+', ',', json_str)

    return json_str


def extract_yoast_settings(html_source):
    """
    从HTML源码中提取Yoast SEO插件的settings值

    参数:
        html_source (str): HTML源码字符串

    返回:
        dict: 提取到的settings字典，如果未找到则返回None
    """
    try:
        # 使用正则表达式查找包含yoast-seo-new-settings-js-extra的script标签
        pattern = r'<script id="yoast-seo-new-settings-js-extra">\s*([\s\S]*?)\s*</script>'
        match = re.search(pattern, html_source)

        if not match:
            return None

        script_content = match.group(1)

        yoast_settings_wpnonce_pattern = r'"nonce"\s*:\s*"([^"]+)"'  # 更灵活的匹配（允许空格和换行）
        yoast_settings_wpnonce = re.search(yoast_settings_wpnonce_pattern, script_content)

        if not yoast_settings_wpnonce:
            return None

        LOGIN_COOKIES["yoast_settings_wpnonce"] = yoast_settings_wpnonce.group(1)

        # 提取变量赋值部分
        var_pattern = r'var\s+wpseoScriptData\s*=\s*({[\s\S]*?})\s*;'
        var_match = re.search(var_pattern, script_content)

        if not var_match:
            return None

        json_str = var_match.group(1)

        # 解析JSON
        data = json.loads(json_str)

        # 返回settings字段
        return data.get('settings')

    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}")
        return None
    except Exception as e:
        print(f"提取过程中发生错误: {e}")
        return None


def convert_wpseo_json_to_query_string(json_str):
    """
    将WPSEO配置JSON转换为URL编码的查询字符串格式

    参数:
        json_str (str): JSON字符串或已解析的字典

    返回:
        str: URL编码的查询字符串
    """
    # 如果输入是字符串，解析为字典
    if isinstance(json_str, str):
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            raise ValueError("输入的JSON字符串格式不正确")
    elif isinstance(json_str, dict):
        data = json_str
    else:
        raise TypeError("输入必须是JSON字符串或字典")

    result_parts = []

    def flatten_dict(prefix, obj, result):
        """递归展平嵌套字典"""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, dict):
                    flatten_dict(f"{prefix}[{key}]", value, result)
                else:
                    # 将值转换为字符串并进行URL编码
                    encoded_value = urllib.parse.quote(str(value))
                    encoded_key = urllib.parse.quote(f"{prefix}[{key}]")
                    result.append(f"{encoded_key}={encoded_value}")

    # 处理wpseo主键
    for main_key, main_value in data.items():
        if isinstance(main_value, dict):
            flatten_dict(main_key, main_value, result_parts)
        else:
            # 直接编码顶级非字典值
            encoded_value = urllib.parse.quote(str(main_value))
            result_parts.append(f"{urllib.parse.quote(main_key)}={encoded_value}")

    return "&".join(result_parts)

def replace(json,blogdescription,logo_id,logo_url):
    # 获取 Yoast SEO settings wpnonce
    yoast_settings_wpnonce = LOGIN_COOKIES["yoast_settings_wpnonce"]

    # 更新表单
    json["wpseo_titles"]["title-home-wpseo"] = "%%sitename%%"   # 替换 title-home-wpseo 首页标题模板  %%sitename%%
    json["wpseo_titles"]["metadesc-home-wpseo"]= blogdescription    # 描述
    json["wpseo_titles"]["open_graph_frontpage_image"]= logo_url    # 图片url
    json["wpseo_titles"]["open_graph_frontpage_image_id"] = logo_id     # 图片id
    # print("表单json形式:",json)

    url_json = convert_wpseo_json_to_query_string(json)   # 转换为url表单
    json_str = f"option_page=wpseo_page_settings&_wp_http_referer=admin.php%3Fpage%3Dwpseo_page_settings_saved&action=update&_wpnonce={yoast_settings_wpnonce}&"+url_json


    return json_str

def get_updata_json(session, site, request_fn):
    resp = request_fn(session, "GET", f"https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings")
    data_json = extract_yoast_settings(resp.text)
    if data_json:
        print("✅ 成功提取SEO请求表单!")
        return data_json
    return None

def updata_json(site, session, request_fn, data):
    headers = {
        'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
        'origin': f'https://www.{site}',
        'referer': f'https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36'
    }

    cookies = {k: v for k, v in LOGIN_COOKIES.items() if any(word in k for word in ['wordpress_logged_in', 'wordpress_sec'])}

    url = f"https://www.{site}/wp-admin/options.php"

    print("cookies",cookies)

    resp = request_fn(session, "POST", url, headers=headers,data=data)
    if resp.status_code == 200:
        print("✅ 设置SEO第二步成功!")
    else:
        print("❌ 设置SEO第二步成功!")

def process_yoast_settings(site_folder, session, request_fn):
    # 获取路径最后一个
    site = os.path.basename(site_folder)

    # 1. 获取站点描述
    blogdescription = get_site_blogdescription(site, session, request_fn)

    # 2.获取logo图片 返回的图片ID和链接
    logo_id,logo_url = get_logo_id_or_upload(site_folder,session, site, request_fn)

    # 3.提取SEO表单
    updata_json_value = get_updata_json(session, site, request_fn)

    # 4.更新参数
    resp_json = replace(updata_json_value,blogdescription,logo_id,logo_url)

    # 5.发生请求进行更新
    updata_json(site, session, request_fn,resp_json)


# ---------- 获取火箭的 nonce 提取 ----------
def extract_wpnonce_bs4(html_content):
    """
    使用BeautifulSoup提取符合条件的a标签的_wpnonce参数值
    """
    soup = BeautifulSoup(html_content, 'html.parser')

    # 查找所有符合条件的a标签
    for a_tag in soup.find_all('a'):
        # 检查class属性
        class_attr = a_tag.get('class', [])
        if not class_attr or 'ab-item' not in class_attr:
            continue

        # 检查role属性
        if a_tag.get('role') != 'menuitem':
            continue

        # 获取href属性
        href = a_tag.get('href', '')
        if 'action=purge_cache' not in href:
            continue

        # 解析URL，提取_wpnonce参数
        try:
            parsed_url = urlparse(href)
            query_params = parse_qs(parsed_url.query)
            wpnonce = query_params.get('_wpnonce', [None])[0]

            if wpnonce:
                return wpnonce
        except Exception as e:
            print(f"解析URL时出错: {e}")
            continue
    return

# ---------- WP ROCKET CACHE CLEARING ----------
def clear_wp_rocket_cache(site, session, request_fn):
    """
    清理 WP Rocket 缓存
    """
    print(f"--- 清理 WP Rocket 缓存: {site}")

    try:
        cookies = filter_cookies_by_keyword(keyword="wordpress_sec")

        # 获取nonce
        admin_page_url = f"https://www.{site}/wp-admin/index.php"
        admin_resp = request_fn(session, "GET", admin_page_url,
                                cookies={k: v for k, v in LOGIN_COOKIES.items() if
                                         any(word in k for word in ['wordpress_logged_in', 'wordpress_sec'])})

        if not admin_resp:
            print(f"❌ 获取WP火箭nonce失败: 无法访问管理页面")
            return

        nonce = extract_wpnonce_bs4(admin_resp.text)

        if nonce:
            print("✅ 获取WP火箭nonce成功！")
            wpnonce = nonce
        else:
            print("❌ 获取WP火箭nonce失败，火箭更新可能失败！")
            wpnonce = None

        params = {
            "action": "purge_cache",
            "type": "all",
            "_wp_http_referer": "/wp-admin/index.php",
            "_wpnonce": wpnonce
        }

        # 通过admin-post.php清理缓存
        purge_url = f"https://www.{site}/wp-admin/admin-post.php"
        resp = request_fn(session, "GET", purge_url, cookies=cookies, params=params)

        if resp is None:
            print(f"❌ 清理WP Rocket缓存请求失败（超时或无响应），跳过此站点: {site}")
            return

        if "Cache cleared." in resp.text:
            print("✅ 刷新WP火箭成功！")
        else:
            print("❌ 刷新WP火箭失败！请检查代码！")
    except Exception as e:
        print(f"❌ 清理WP Rocket缓存时发生异常，跳过此站点 {site}: {str(e)}")
        return

# ------------SEO 第二步--------------------
# 获取SEO wpseo[index_now_key] 的key
def get_seo_index_now_key(session, site, request_fn):
    # 请求页面
    resp = request_fn(session, "GET", f"https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings")

    # 测试正则匹配
    script_pattern = r'<script[^>]*id="yoast-seo-new-settings-js-extra"[^>]*>(.*?)</script>'
    script_match = re.search(script_pattern, resp.text, re.DOTALL | re.IGNORECASE)

    if script_match:
        script_content = script_match.group(1)

        # 查找 index_now_key
        key_pattern = r'"index_now_key"\s*:\s*"([^"]+)"'
        key_match = re.search(key_pattern, script_content)

        # 查找 nonce
        nonce_pattern = r'"nonce"\s*:\s*"([^"]+)"'
        nonce_match = re.search(nonce_pattern, script_content)

        if key_match:
            print(f"✅ 测试成功: 找到key: {key_match.group(1)}")
            LOGIN_COOKIES["index_now_key"] = key_match.group(1)

            if nonce_match:
                print(f"✅ 找到nonce: {nonce_match.group(1)}")
                LOGIN_COOKIES["wpnonce"] = nonce_match.group(1)

            # 可以返回多个值
            return {
                "index_now_key": key_match.group(1),
                "nonce": nonce_match.group(1) if nonce_match else None
            }
        else:
            print("❌ 测试失败: 未找到key")
    else:
        print("❌ 测试失败: 未找到script标签")
    return

def get_return_Shipping_url(session, site, request_fn):
    '''
    return: [{'decoded_href': 'https://www.puretumblr.com/wp-admin/post.php?post=11&action=edit', 'decoded_text': 'Refund and Returns Policy'}, {'decoded_href': 'https://www.puretumblr.com/wp-admin/post.php?post=70&action=edit', 'decoded_text': 'Return & Refund Policy'}, {'decoded_href': 'https://www.puretumblr.com/wp-admin/post.php?post=68&action=edit', 'decoded_text': 'Shipping and delivery'}]
    返回的是一个列表字典
    '''

    # 发送请求
    url = f"https://www.{site}/wp-admin/edit.php?post_type=page"
    resp = request_fn(session, "GET", url)

    # 检查响应
    if resp is None or resp.status_code != 200:
        print("❌ 访问页面page失败!")
        return []

    print("✅ 访问页面page成功!")

    # 获取响应文本
    html_content = resp.text

    # 定义要匹配的aria-label值（注意：这里保持原始HTML实体）
    target_aria_labels = [
        '&#8220;Shipping and delivery&#8221; (Edit)',
        '&#8220;Refund and Returns Policy&#8221; (Edit)',
        '&#8220;Return &amp; Refund Policy&#8221; (Edit)'
    ]

    # 创建匹配所有目标的正则表达式
    target_patterns = '|'.join([re.escape(label) for label in target_aria_labels])
    pattern = rf'<a\s+[^>]*aria-label\s*=\s*["\']({target_patterns})["\'][^>]*>.*?</a>'

    # 查找所有匹配
    matches = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)

    # 存储匹配的a标签
    matched_links = []

    for aria_label in matches:
        # 找到完整的a标签
        full_tag_pattern = rf'<a\s+[^>]*aria-label\s*=\s*["\']{re.escape(aria_label)}["\'][^>]*>.*?</a>'
        full_tag_match = re.search(full_tag_pattern, html_content, re.DOTALL | re.IGNORECASE)

        if not full_tag_match:
            continue

        full_tag = full_tag_match.group(0)

        # 提取href
        href_match = re.search(r'href\s*=\s*["\']([^"\']+)["\']', full_tag, re.IGNORECASE)
        href = href_match.group(1) if href_match else ''

        # 提取标签内容
        content_match = re.search(r'>(.*?)</a>', full_tag, re.DOTALL)
        if content_match:
            raw_text = content_match.group(1)
            # 去除内部HTML标签
            text = re.sub(r'<[^>]+>', '', raw_text).strip()
        else:
            text = ''

        # 对提取的数据进行HTML实体解码
        decoded_href = html.unescape(href)
        decoded_text = html.unescape(text)

        link_info = {
            'decoded_href': decoded_href,  # 解码后的链接
            'decoded_text': decoded_text,  # 解码后的文本
        }
        matched_links.append(link_info)
        print(f"✅ 找到匹配的退货和运输政策链接:")
        print(f"----解码后链接: {decoded_href}")

    # 输出统计信息
    if matched_links:
        print(f"✅ 总共找到 {len(matched_links)} 个匹配的链接")
        return matched_links
    else:
        print("❌ 未找到匹配的链接")
        return []


def get_moban_json(moban_name, url_id):
    """
    从指定文件夹中随机选择一个JSON文件，修改id参数为url_id值，然后返回JSON数据
    """
    try:
        # 1. 构建文件夹路径
        moban_name_path = os.path.join(MOBAN_JSON_PATH,moban_name)
        folder_path = Path(moban_name_path)

        # 检查文件夹是否存在
        if not folder_path.exists() or not folder_path.is_dir():
            print(f"❌ 文件夹 '{moban_name_path}' 不存在或不是一个目录")
            return None

        # 2. 获取所有JSON文件
        json_files = list(folder_path.glob("*.json"))

        # 检查是否有JSON文件
        if not json_files:
            print(f"❌ 文件夹 '{moban_name_path}' 中没有找到JSON文件")
            return None

        # 3. 随机选择一个JSON文件
        selected_file = random.choice(json_files)
        print(f"随机选择{moban_name}的文件: {selected_file.name}")

        # 4. 读取JSON文件
        with open(selected_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 5. 修改id参数
        data["id"] = url_id

        return data

    except json.JSONDecodeError:
        print(f"错误：文件 {selected_file.name} 不是有效的JSON格式")
        return None
    except KeyError:
        print(f"错误：JSON文件缺少 'id' 或 'content' 字段")
        return None
    except Exception as e:
        print(f"发生未知错误: {e}")
        return None


def updata_returns_Shipping(session,site,url_list_dict,request_fn):
    # 获取 nonce
    nonce = get_yoast_nonce(session, site, request_fn)
    if not nonce:
        raise RuntimeError("获取 Yoast nonce 失败")

    headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "x-http-method-override": "PUT",
    "x-wp-nonce": nonce
}
    # filter_cookies_by_keyword(keyword="wordpress_logged_in")
    cookies = {}
    cookies[list(filter_cookies_by_keyword(keyword="wordpress_logged_in"))[0]] = filter_cookies_by_keyword(keyword="wordpress_logged_in").get(list(filter_cookies_by_keyword(keyword="wordpress_logged_in"))[0])
    params = {
        "_locale": "user"
    }

    # 这个版本专门匹配post=后面的数字
    pattern = r'post=(\d+)'
    flag = True
    Refund_Returns_id = None

    for i,dict in enumerate(url_list_dict):
        if "Return & Refund Policy" in dict["decoded_text"]:
            id = re.search(pattern, dict["decoded_href"]).group(1)
            url = f"https://www.{site}/wp-json/wp/v2/pages/{id}"
            flag = False
            print(f"⏳ 正在请求更改 {site} 的 Return & Refund Policy->",url)
            json = get_moban_json("return",url_id=id)
            if json:
                resp = request_fn(session, "POST", url,headers=headers,params=params,cookies=cookies,json=json)
                if resp.status_code == 200:
                    print(f"✅ 更新 {site} 的 Return & Refund Policy 成功!")
                else:
                    print(f"❌ 更新 {site} 的 Return & Refund Policy 失败!  错误码为:{resp.status_code} - {resp.reason}")
            else:
                print("❌ 没有找到json退货与运输文本,请检查路径!")
        elif "Shipping and delivery" in dict["decoded_text"]:
            id = re.search(pattern, dict["decoded_href"]).group(1)
            url = f"https://www.{site}/wp-json/wp/v2/pages/{id}"
            print(f"⏳ 正在请求更改 {site} 的 Shipping and delivery->url",url)
            json = get_moban_json("shipping", url_id=id)
            if json:
                resp = request_fn(session,"POST",url, headers=headers, params=params, cookies=cookies, json=json)
                if resp.status_code == 200:
                    print(f"✅ 更新 {site} 的 Shipping and delivery 成功!")
                else:
                    print(f"❌ 更新 {site} 的 Shipping and delivery 失败!  错误码为:{resp.status_code} - {resp.reason}")
            else:
                print("❌ 没有找到json退货与运输文本,请检查路径!")
        else:
            Refund_Returns_id = i

    if flag:
        print(f"⚠️ ----站点 {site} 没有 Return & Refund Policy 选项, 将变更为更新 Refund and Returns Policy 退货政策!")
        id = re.search(pattern, url_list_dict[Refund_Returns_id]["decoded_href"]).group(1)
        url = f"https://www.{site}/wp-json/wp/v2/pages/{id}"
        print(f"⏳ 正在请求更改 {site} 的 Refund and Returns Policy->url", url)
        json = get_moban_json("return", url_id=id)
        if json:
            resp = request_fn(session,"POST", url, headers=headers, params=params, cookies=cookies, json=json)
            if resp.status_code == 200:
                print(f"✅ 更新 {site} 的 Refund and Returns Policy 成功!")
            else:
                print(f"❌ 更新 {site} 的 Refund and Returns Policy 失败!  错误码为:{resp.status_code} - {resp.reason}")
        else:
            print("❌ 没有找到json退货与运输文本,请检查路径!")

# ---------- SITE PROCESS ----------
def process_site(site_folder, args):  # site_folder 域名路径   args 参数     # 主处理站点代码
    site = os.path.basename(site_folder)
    print(f"\n==== 处理站点: {site} ====")
    try:  # 登录 WordPress 后台
        session = login(site, password=args.password)
    except Exception as e:
        write_failed(site, "登录", str(e))
        return



    only = args.only or ""  # 解析参数 --only    如LOGIN_COOKIES果没传，则执行所有模块。
    only_set = set(x.strip().lower() for x in only.split(",") if x.strip()) if only else None

    if (only_set is None) or ("icon" in only_set):  # 执行 ICON 模块
        try:
            process_icon(site_folder, session, request_with_retry, date_format=args.date_format,
                         time_format=args.time_format, week_starts_on=args.week_starts_on)
        except Exception as e:
            write_failed(site, "ICON", str(e))

    if (only_set is None) or ("rocket" in only_set):   #  WP ROCKET 模块
        try:
            process_rocket(site, session, request_with_retry)
        except Exception as e:
            write_failed(site, "WP_ROCKET", str(e))

    if (only_set is None) or ("banner" in only_set):  # 执行 BANNER 模块    更换成 上传 banner.jpg 但改名为 域名的.jpg 具体参考  ICON 模块 上传图片
        try:
            process_banner(site_folder, session, request_with_retry)
        except Exception as e:
            write_failed(site, "BANNER", str(e))

    if (only_set is None) or ("yoast" in only_set):  # 执行 YOAST 模块   已解决
        try:
            process_yoast(site, session, request_with_retry)
        except Exception as e:
            write_failed(site, "YOAST", str(e))



    # 退货与运输文本设置
    if (only_set is None) or ("return_Shipping" in only_set):  # 执行 退货与运输文本设置 步骤    可用
        try:
            print(f"\n📝 正在编辑 {site} 的退货与运输政策文本")
            url_list_dict = get_return_Shipping_url(session, site, request_with_retry)   # 获取  退货与运输文本设置  的链接
            updata_returns_Shipping(session, site, url_list_dict, request_with_retry)  # 更新 退货与运输政策文本
        except Exception as e:
            write_failed(site, "return_Shipping", str(e))

    # 清理 WP Rocket 缓存
    clear_wp_rocket_cache(site, session,request_with_retry)

# ---------- MAIN ----------
def main():
    # 交互式获取 BASE_DIR
    print("🎉 退货与运输政策json文本路径若已更改,记得代码同步更改!")
    base_dir = input("请输入站点根目录路径（例如：D:\\web\\11-3周\\1）: ").strip()
    if not base_dir or not os.path.isdir(base_dir):
        print(f"❌ 无效的目录路径: {base_dir}")
        sys.exit(1)

    # 交互式获取要处理的域名
    print(f"\n目录 {base_dir} 下的所有站点:")
    all_sites_in_dir = [n for n in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, n))]
    for i, site in enumerate(all_sites_in_dir, 1):
        print(f"  {i}. {site}")

    domain_input = input("\n请输入要处理的域名（多个域名用逗号分隔，输入 # 处理全部）: ").strip()

    if domain_input == "#":
        # 处理全部站点
        sites_to_process = [os.path.join(base_dir, n) for n in all_sites_in_dir]
        print(f"🔄 将处理所有 {len(sites_to_process)} 个站点")
    else:
        # 处理指定站点
        selected_domains = [d.strip() for d in domain_input.split(",") if d.strip()]
        sites_to_process = []
        for domain in selected_domains:
            domain_path = os.path.join(base_dir, domain)
            if os.path.isdir(domain_path):
                sites_to_process.append(domain_path)
            else:
                print(f"⚠️ 警告：域名目录不存在，跳过: {domain}")

        if not sites_to_process:
            print("❌ 没有找到有效的域名目录，程序退出")
            sys.exit(1)

        print(f"🔄 将处理 {len(sites_to_process)} 个指定站点: {', '.join(selected_domains)}")

    # 解析其他命令行参数
    parser = argparse.ArgumentParser(description="批量处理 WP: ICON / WP_ROCKET / BANNER / YOAST")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="WP 登录密码（默认从环境变量 WP_PASSWORD）")
    parser.add_argument("--date-format", default="F j, Y", help='WP 日期格式，默认 "F j, Y"')
    parser.add_argument("--time-format", default="g:i a", help='WP 时间格式，默认 "g:i a"')
    parser.add_argument("--week-starts-on", default=1, type=int, choices=range(0, 7), help="周起始日 0=周日,1=周一 ...")
    parser.add_argument("--sleep", default=2, type=float, help="站点间隔秒数")
    parser.add_argument("--only", default="", help="只运行哪些模块，例如: icon,rocket,banner,yoast")
    parser.add_argument("--clear-logs", action="store_true", help="运行前清空 success/failed 日志")
    args = parser.parse_args()

    # 设置 BASE_DIR 到 args 中，供其他函数使用
    args.base_dir = base_dir

    if args.clear_logs:
        open(SUCCESS_LOG, "w", encoding="utf-8").close()
        open(FAILED_LOG, "w", encoding="utf-8").close()

    # 如果失败日志存在，则只处理失败站点（仅在处理全部站点时生效）
    if os.path.exists(FAILED_LOG):
        with open(FAILED_LOG, "r", encoding="utf-8") as f:
            failed_lines = f.readlines()
        failed_sites = set()
        for line in failed_lines:
            parts = line.strip().split("\t")
            if parts:
                failed_sites.add(os.path.join(base_dir, parts[0]))
        if failed_sites:
            print(f"🔄 检测到失败日志，重新处理 {len(failed_sites)} 个失败站点")
            sites_to_process = [s for s in sites_to_process if s in failed_sites]

    # 每次运行前清空成功日志
    open(SUCCESS_LOG, "w", encoding="utf-8").close()

    # 处理站点
    for s in sites_to_process:
        process_site(s, args)
        time.sleep(args.sleep)

    print("✅ 本轮处理完成")

if __name__ == "__main__":
    main()   # allbowlingnow.com
