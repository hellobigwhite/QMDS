# integrated_wp_tool.py
import os
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- CONFIG ----------
BASE_DIR = r"C:\Users\Administrator\Desktop\logo\未建站"  # <-- 修改为你的目录或通过命令行覆盖
SUCCESS_LOG = "success.log"
FAILED_LOG = "failed.log"
DEFAULT_PASSWORD = os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")
ICON_NAME = "icon.png"
WP_BANNER_NAMES = ["banner.jpg", "banner.webp", "bannerstore.jpg", "banner-scaled.jpg"]
MAX_KB = 300

# ---------- UTIL ----------
def write_success(site, action, extra=""):
    with open(SUCCESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t成功\t{extra}\n")
    print(f"✅ [{site}] {action} 成功 {extra}")

def write_failed(site, action, reason=""):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t失败\t{reason}\n")
    print(f"❌ [{site}] {action} 失败: {reason}")

def request_with_retry(session, method, url, retries=3, delay=5, verify_ssl=False, **kwargs):
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=25, verify=verify_ssl, **kwargs)
            if resp is not None and resp.status_code in (200, 201):
                return resp
            else:
                print(f"⚠️ 状态码 {getattr(resp,'status_code',None)} 第 {i+1}/{retries} 次重试: {url}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ 请求异常: {e}，{method} {url}，第 {i+1}/{retries} 次重试")
        time.sleep(delay)
    return None

# ---------- AUTH ----------
def login(site, session=None, password=None):
    if password is None:
        password = DEFAULT_PASSWORD
    if session is None:
        session = requests.Session()
    login_url = f"https://www.{site}/bbwllogin/"
    name = site.replace('.com', '').strip()
    data = {
        'log': f'Ad{name}min',
        'pwd': password,
        'wp-submit': 'Log In',
        'redirect_to': f"https://www.{site}/wp-admin/",
        'testcookie': '1'
    }
    try:
        resp = session.post(login_url, data=data, allow_redirects=True, verify=False, timeout=20)
    except Exception as e:
        raise RuntimeError(f"登录请求异常: {e}")
    if any("wordpress_logged_in" in c.name for c in session.cookies):
        return session
    try:
        admin_check = session.get(f"https://www.{site}/wp-admin/", verify=False, timeout=15)
        if admin_check is not None and admin_check.status_code == 200:
            return session
    except Exception:
        pass
    raise RuntimeError("登录失败：未检测到登录 cookie 或无法访问 /wp-admin/")

# ---------- ICON FUNCTIONS ----------
def get_upload_nonce(session, site, request_fn):
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

def query_existing_icon(session, site, request_fn):
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
        text = resp.text  # 或 await resp.text()
        text = text.lstrip('\ufeff')  # 去掉 BOM
        js = json.loads(text)
        if js and js.get("data"):
            media = js["data"][0]
            return media.get("id"), media.get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ query_existing_icon JSON 解析出错: {e}")
    return None, None

def get_last_icon_path(domain_folder):
    for root, dirs, files in os.walk(domain_folder):
        if ICON_NAME in files:
            return os.path.join(root, ICON_NAME)
    return None

def upload_icon(session, site, icon_path, upload_nonce):
    upload_url = f"https://www.{site}/wp-admin/async-upload.php"
    with open(icon_path, "rb") as f:
        files = {"async-upload": (ICON_NAME, f, "image/png")}
        data = {
            "action": "upload-attachment",
            "_wpnonce": upload_nonce,
            "_wp_http_referer": "/wp-admin/media-new.php",
            "name": ICON_NAME
        }
        headers = {"Accept": "*/*", "Origin": f"https://www.{site}", "Referer": f"https://www.{site}/wp-admin/media-new.php", "User-Agent": "Mozilla/5.0"}
        resp = session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)
    if resp is None:
        return None, None
    try:
        text = resp.text  # 或 await resp.text()
        text = text.lstrip('\ufeff')  # 去掉 BOM
        js = json.loads(text)
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
    except Exception as e:
        print(f"⚠️ upload_icon JSON 解析出错: {e}")
    return None, None

def crop_icon(session, site, media_id, crop_nonce):
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    crop_data = {
        "_wpnonce": crop_nonce,
        "id": media_id,
        "context": "site-icon",

        # 裁剪整张图片
        "cropDetails[x1]": 0,
        "cropDetails[y1]": 0,
        "cropDetails[x2]": "full",
        "cropDetails[y2]": "full",
        "cropDetails[width]": "full",
        "cropDetails[height]": "full",

        # 输出尺寸保持不变
        "cropDetails[dst_width]": 512,
        "cropDetails[dst_height]": 512,

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

def save_wp_settings(session, site, request_fn, site_icon_id=None, date_format=None, time_format=None, week_starts_on=None):
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

def process_icon(site_folder, session, request_fn, date_format="F j, Y", time_format="g:i a", week_starts_on=1):
    site = os.path.basename(site_folder)
    print(f"--- ICON: {site}")
    media_id, crop_nonce = query_existing_icon(session, site, request_fn)
    if media_id and crop_nonce:
        print(f"已有 icon，id={media_id}，尝试裁剪")
        final_id = crop_icon(session, site, media_id, crop_nonce) or media_id
    else:
        icon_path = get_last_icon_path(site_folder)
        if not icon_path:
            raise RuntimeError("本地未找到 icon.png")
        upload_nonce = get_upload_nonce(session, site, request_fn)
        if not upload_nonce:
            raise RuntimeError("获取上传 _wpnonce 失败")
        media_id, crop_nonce = upload_icon(session, site, icon_path, upload_nonce)
        if not media_id:
            raise RuntimeError("上传 icon 失败")
        final_id = crop_icon(session, site, media_id, crop_nonce) or media_id
    ok = save_wp_settings(session, site, request_fn, site_icon_id=final_id, date_format=date_format, time_format=time_format, week_starts_on=week_starts_on)
    if not ok:
        raise RuntimeError("保存 WP 设置失败")
    write_success(site, "ICON", f"id={final_id}")
    return True

# ---------- BANNER FUNCTIONS ----------
def compress_image_to_max_size(input_path, max_kb=MAX_KB):
    img = Image.open(input_path)
    img_format = img.format or "JPEG"
    quality = 95
    temp_path = input_path
    img.save(temp_path, format=img_format, quality=quality)
    size_kb = os.path.getsize(temp_path) / 1024
    while size_kb > max_kb and quality > 10:
        quality -= 5
        img.save(temp_path, format=img_format, quality=quality)
        size_kb = os.path.getsize(temp_path) / 1024
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
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    existing = {}
    for name in WP_BANNER_NAMES:
        data = {
            "action": "query-attachments",
            "post_id": 0,
            "query[post_mime_type]": "image",
            "query[s]": name,
            "query[posts_per_page]": 40
        }
        resp = request_fn(session, "POST", ajax_url, data=data)
        if not resp:
            continue
        try:
            text = resp.text  # 或 await resp.text()
            text = text.lstrip('\ufeff')  # 去掉 BOM
            js = json.loads(text)
            if js.get("data"):
                media = js["data"][0]
                media_id = media.get("id")
                delete_nonce = media.get("nonces", {}).get("delete")
                if media_id and delete_nonce:
                    existing[name] = {"id": media_id, "delete_nonce": delete_nonce}
        except Exception:
            continue
    return existing

def delete_banner(session, site, name, media_id, delete_nonce):
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    data = {"action": "delete-post", "id": media_id, "_wpnonce": delete_nonce}
    resp = session.post(ajax_url, data=data, verify=False, timeout=20)
    return resp is not None and resp.status_code == 200

def upload_banner(session, site, local_path, target_name, upload_nonce):
    upload_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    with open(local_path, "rb") as f:
        mime_type = "image/webp" if target_name.lower().endswith(".webp") else "image/jpeg"
        files = {"async-upload": (target_name, f, mime_type)}
        data = {"action": "upload-attachment", "_wpnonce": upload_nonce, "_wp_http_referer": "/wp-admin/media-new.php"}
        resp = session.post(upload_url, data=data, files=files, verify=False, timeout=30)
    if not resp:
        return None
    try:
        text = resp.text  # 或 await resp.text()
        text = text.lstrip('\ufeff')  # 去掉 BOM
        js = json.loads(text)
        if js.get("success") and "data" in js:
            return js["data"].get("url", "")
    except Exception:
        pass
    return None

def process_banner(site_folder, session, request_fn):
    site = os.path.basename(site_folder)
    print(f"--- BANNER: {site}")
    upload_nonce = get_banner_upload_nonce(session, site, request_fn)
    if not upload_nonce:
        raise RuntimeError("获取 banner 上传 nonce 失败")
    existing = query_existing_banners(session, site, request_fn)
    upload_targets = []
    local_jpg = os.path.join(site_folder, "banner.jpg")
    local_webp = os.path.join(site_folder, "banner.webp")
    local_png = os.path.join(site_folder, "banner.png")
    for name, info in existing.items():
        try:
            deleted = delete_banner(session, site, name, info["id"], info["delete_nonce"])
        except Exception:
            deleted = False
        if deleted:
            ext = name.split(".")[-1].lower()
            target_tmp = os.path.join(site_folder, f"temp_{name}")
            local_file = None
            if name == "banner-scaled.jpg":
                target_tmp = os.path.join(site_folder, "banner.jpg")
                local_file = None
                if os.path.exists(local_jpg):
                    local_file = local_jpg
                elif os.path.exists(local_webp):
                    local_file = convert_image_format(local_webp, "jpg", target_tmp)
                elif os.path.exists(local_png):
                    local_file = convert_image_format(local_png, "jpg", target_tmp)
                if local_file and os.path.exists(local_file):
                    # 固定上传为 banner.jpg
                    upload_targets.append(("banner.jpg", local_file))
                continue
            if ext == "jpg":
                if os.path.exists(local_jpg):
                    local_file = local_jpg
                elif os.path.exists(local_webp):
                    local_file = convert_image_format(local_webp, "jpg", target_tmp)
                elif os.path.exists(local_png):
                    local_file = convert_image_format(local_png, "jpg", target_tmp)
            elif ext == "webp":
                if os.path.exists(local_webp):
                    local_file = local_webp
                elif os.path.exists(local_jpg):
                    local_file = convert_image_format(local_jpg, "webp", target_tmp)
                elif os.path.exists(local_png):
                    local_file = convert_image_format(local_png, "webp", target_tmp)
            elif ext == "png":
                if os.path.exists(local_png):
                    local_file = local_png
                elif os.path.exists(local_jpg):
                    local_file = convert_image_format(local_jpg, "png", target_tmp)
                elif os.path.exists(local_webp):
                    local_file = convert_image_format(local_webp, "png", target_tmp)
            if local_file and os.path.exists(local_file):
                upload_targets.append((name, local_file))
    for target_name, local_path in upload_targets:
        path_to_upload = local_path
        if os.path.getsize(local_path) / 1024 > MAX_KB:
            path_to_upload = compress_image_to_max_size(local_path, MAX_KB)
        url = upload_banner(session, site, path_to_upload, target_name, upload_nonce)
        if not url:
            raise RuntimeError(f"上传 banner 失败: {target_name}")
        write_success(site, "BANNER", f"{target_name} -> {url}")
        time.sleep(1)

    # # --- ✅ 上传财神图片 ---
    # CAISHEN_PATH = r"E:\9月1\10月13\素材\caishen.jpg"
    # if os.path.exists(CAISHEN_PATH):
    #     try:
    #         path_to_upload = CAISHEN_PATH
    #         if os.path.getsize(CAISHEN_PATH) / 1024 > MAX_KB:
    #             path_to_upload = compress_image_to_max_size(CAISHEN_PATH, MAX_KB)
    #         caishen_url = upload_banner(session, site, path_to_upload, "caishen.jpg", upload_nonce)
    #         if caishen_url:
    #             write_success(site, "CAISHEN", f"caishen.jpg -> {caishen_url}")
    #         else:
    #             write_failed(site, "CAISHEN", "上传失败（无返回 URL）")
    #     except Exception as e:
    #         write_failed(site, "CAISHEN", str(e))
    # else:
    #     print(f"⚠️ 未找到财神图片: {CAISHEN_PATH}")

    return True

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
        resp = request_fn(session, "GET", f"https://www.{site}/wp-admin/admin.php?page=wpseo_dashboard#/first-time-configuration")
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
            activate_url = activate_href if activate_href.startswith("http") else urljoin(f"https://www.{site}/wp-admin/", activate_href)
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
        if "deactivate" in a['href'].lower() and (plugin_identifier in a['href'] or plugin_identifier.split("/")[-1] in a['href']):
            return True
    return False

def activate_yoast_optimize(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user"
    payload = {"finishedSteps": ["optimizeSeoData"]}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers)
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

# def set_yoast_site_representation(session, site, nonce, request_fn):
#     logo_id = get_logo_id(session, site, request_fn, "logo.png")
#     url = f"https://www.{site}/wp-json/yoast/v1/configuration/site_representation?_locale=user"
#     payload = {
#         "company_or_person": "company",
#         "company_name": site,
#         "company_logo": f"https://www.{site}/wp-content/uploads/logo.png",
#         "company_logo_id": logo_id,
#         "person_logo": "",
#         "person_logo_id": 0,
#         "website_name": site
#     }
#     headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
#     resp = request_fn(session, "POST", url, json=payload, headers=headers)
#     return resp is not None
#
# def save_yoast_configuration_state(session, site, nonce, request_fn):
#     url = f"https://www.{site}/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user"
#     payload = {"finishedSteps": ["optimizeSeoData", "siteRepresentation", "socialProfiles", "personalPreferences"]}
#     headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
#     resp = request_fn(session, "POST", url, json=payload, headers=headers)
#     return resp is not None
#
# def set_yoast_social_profiles(session, site, nonce, request_fn):
#     url = f"https://www.{site}/wp-json/yoast/v1/configuration/social_profiles?_locale=user"
#     payload = {"facebook_site": "", "twitter_site": "", "other_social_urls": []}
#     headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
#     resp = request_fn(session, "POST", url, json=payload, headers=headers)
#     return resp is not None
#
# def set_yoast_tracking(session, site, nonce, request_fn):
#     url = f"https://www.{site}/wp-json/yoast/v1/configuration/enable_tracking?_locale=user"
#     payload = {"tracking": 0}
#     headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
#     resp = request_fn(session, "POST", url, json=payload, headers=headers)
#     return resp is not None
#
#
# def process_yoast(site, session, request_fn):
#     print(f"--- YOAST: {site}")
#     try:
#         # 激活插件页面
#         wp_url = f"https://www.{site}/wp-admin/plugins.php"
#         resp = request_fn(session, "GET", wp_url)
#         if not resp:
#             raise RuntimeError("无法访问插件页面")
#         soup = BeautifulSoup(resp.text, "html.parser")
#
#         # 激活 Free
#         try:
#             activate_free = soup.find('a', {'id': 'activate-wordpress-seo'})
#             if activate_free and activate_free.get('href'):
#                 session.get(f"https://www.{site}/wp-admin/{activate_free['href']}", verify=False, timeout=20)
#                 print(f"🚀 尝试激活 Yoast SEO (Free) : {site}")
#             else:
#                 print(f"🎉 SEO 已激活: {site}")
#         except Exception:
#             print(f"🎉 SEO 已激活: {site}")
#
#         # 激活 Premium
#         try:
#             activate_premium = soup.find('a', {'id': 'activate-yoast-seo-premium'})
#             if activate_premium and activate_premium.get('href'):
#                 session.get(f"https://www.{site}/wp-admin/{activate_premium['href']}", verify=False, timeout=20)
#                 print(f"🚀 尝试激活 Yoast SEO Premium: {site}")
#             else:
#                 print(f"🎉 SEO Premium 已激活: {site}")
#         except Exception:
#             print(f"🎉 SEO Premium 已激活: {site}")
#
#         # 获取 nonce
#         nonce = get_yoast_nonce(session, site, request_fn)
#         if not nonce:
#             raise RuntimeError("获取 Yoast nonce 失败")
#         # 设置失败标志
#         all_ok = True
#         # 执行每个步骤，并单独打印成功/失败
#         if activate_yoast_optimize(session, site, nonce, request_fn):
#             print(f"✅ 已标记 {site} 的 Yoast SEO 数据优化为完成")
#         else:
#             print(f"❌ {site} Yoast 数据优化失败")
#             all_ok = False
#
#         if set_yoast_site_representation(session, site, nonce, request_fn):
#             print(f"✅ 已设置 {site} 的 Yoast 站点信息")
#         else:
#             print(f"❌ {site} Yoast 站点信息设置失败")
#             all_ok = False
#
#         if save_yoast_configuration_state(session, site, nonce, request_fn):
#             print(f"✅ 已保存 {site} 的完成步骤")
#         else:
#             print(f"❌ {site} 保存完成步骤失败")
#             all_ok = False
#
#         if set_yoast_social_profiles(session, site, nonce, request_fn):
#             print(f"✅ 已设置 {site} 的 Yoast 社交媒体信息")
#         else:
#             print(f"❌ {site} 设置社交媒体信息失败")
#             all_ok = False
#
#         if set_yoast_tracking(session, site, nonce, request_fn):
#             print(f"✅ 已关闭 {site} 的 Yoast 追踪功能")
#         else:
#             print(f"❌ {site} 设置 tracking 失败")
#             all_ok = False
#
#
#         # 最终判定
#         if all_ok:
#             write_success(site, "YOAST", "")
#         else:
#             write_failed(site, "YOAST", "部分步骤执行失败")
#     except Exception as e:
#         write_failed(site, "YOAST", str(e))

# ---------- SITE PROCESS ----------
def process_site(site_folder, args):
    site = os.path.basename(site_folder)
    print(f"\n==== 处理站点: {site} ====")
    try:
        session = login(site, password=args.password)
    except Exception as e:
        write_failed(site, "登录", str(e))
        return

    only = args.only or ""
    only_set = set(x.strip().lower() for x in only.split(",") if x.strip()) if only else None

    if (only_set is None) or ("icon" in only_set):
        try:
            process_icon(site_folder, session, request_with_retry, date_format=args.date_format, time_format=args.time_format, week_starts_on=args.week_starts_on)
        except Exception as e:
            write_failed(site, "ICON", str(e))

    if (only_set is None) or ("banner" in only_set):
        try:
            process_banner(site_folder, session, request_with_retry)
        except Exception as e:
            write_failed(site, "BANNER", str(e))

    # if (only_set is None) or ("yoast" in only_set):
    #     try:
    #         process_yoast(site, session, request_with_retry)
    #     except Exception as e:
    #         write_failed(site, "YOAST", str(e))

# ---------- MAIN ----------
def main():
    parser = argparse.ArgumentParser(description="批量处理 WP: ICON / BANNER / YOAST")
    parser.add_argument("--base-dir", default=BASE_DIR, help="站点根目录，子文件夹为域名")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="WP 登录密码（默认从环境变量 WP_PASSWORD）")
    parser.add_argument("--date-format", default="F j, Y", help='WP 日期格式，默认 "F j, Y"')
    parser.add_argument("--time-format", default="g:i a", help='WP 时间格式，默认 "g:i a"')
    parser.add_argument("--week-starts-on", default=1, type=int, choices=range(0,7), help="周起始日 0=周日,1=周一 ...")
    parser.add_argument("--sleep", default=2, type=float, help="站点间隔秒数")
    parser.add_argument("--only", default="", help="只运行哪些模块，例如: icon,banner,yoast")
    parser.add_argument("--clear-logs", action="store_true", help="运行前清空 success/failed 日志")
    args = parser.parse_args()

    base_dir = args.base_dir
    if not os.path.isdir(base_dir):
        print(f"❌ 无效的 base-dir: {base_dir}")
        sys.exit(1)

    if args.clear_logs:
        open(SUCCESS_LOG, "w", encoding="utf-8").close()
        open(FAILED_LOG, "w", encoding="utf-8").close()

    # 获取所有站点
    all_sites = [os.path.join(base_dir, n) for n in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, n))]

    # 如果失败日志存在，则只处理失败站点
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
            sites_to_process = [s for s in all_sites if s in failed_sites]
        else:
            sites_to_process = all_sites
    else:
        sites_to_process = all_sites

    # 每次运行前清空成功日志
    open(SUCCESS_LOG, "w", encoding="utf-8").close()

    for s in sites_to_process:
        process_site(s, args)
        time.sleep(args.sleep)

    print("✅ 本轮处理完成")

if __name__ == "__main__":
    main()