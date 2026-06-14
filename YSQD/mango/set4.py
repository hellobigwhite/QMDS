import os
import sys
import time
import re
import argparse
import json
import urllib3
import requests
import traceback
from bs4 import BeautifulSoup
from PIL import Image
from urllib.parse import urljoin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- CONFIG ----------
BASE_DIR = r"C:\Users\Administrator\Desktop\logo\未建站\开始建站"  # <-- 修改为你的目录或通过命令行覆盖
SUCCESS_LOG = "success.log"
FAILED_LOG = "failed.log"
# 固定密码配置（优先级：命令行 --password > 环境变量 WP_PASSWORD > 固定值）
DEFAULT_PASSWORD = os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")  # 🔴 你的固定密码
ICON_NAME = "icon.png"
WP_BANNER_NAMES = ["banner.jpg", "banner.webp", "bannerstore.jpg", "banner-scaled.jpg"]
MAX_KB = 300
MAX_ICON_SIZE = 5 * 1024 * 1024  # 5MB 最大icon文件限制


# ---------- UTIL ----------
def write_success(site, action, extra=""):
    with open(SUCCESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t成功\t{extra}\n")
    print(f"✅ [{site}] {action} 成功 {extra}")


def write_failed(site, action, reason=""):
    stack = traceback.format_exc() if reason else ""
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{site}\t{action}\t失败\t{reason}\t{stack}\n")
    print(f"❌ [{site}] {action} 失败: {reason}")


def request_with_retry(session, method, url, retries=3, delay=5, verify_ssl=False, **kwargs):
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=25, verify=verify_ssl, **kwargs)
            if resp is not None and resp.status_code in (200, 201):
                return resp
            else:
                print(f"⚠️ 状态码 {getattr(resp, 'status_code', None)} 第 {i + 1}/{retries} 次重试: {url}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ 请求异常: {e}，{method} {url}，第 {i + 1}/{retries} 次重试")
        time.sleep(delay)
    return None


def safe_json_loads(json_str):
    """安全解析 JSON 字符串，自动去除 UTF-8 BOM 并处理解析错误"""
    try:
        # 去除开头的 UTF-8 BOM
        if json_str.startswith('\ufeff'):
            json_str = json_str[1:]
        # 处理可能的空白字符
        json_str = json_str.strip()
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON 解析失败（BOM/格式错误）: {e}")
    except Exception as e:
        raise RuntimeError(f"JSON 解析异常: {e}")


def resp_to_json(resp):
    """从 requests 响应中安全解析 JSON（处理 BOM）"""
    if not resp:
        return None
    try:
        return safe_json_loads(resp.text)
    except RuntimeError as e:
        print(f"⚠️ 响应 JSON 解析失败: {e}")
        return None
    except Exception as e:
        print(f"⚠️ 响应解析未知错误: {e}")
        return None


# ---------- AUTH ----------
def login(site, session=None, password=None):
    """
    登录WP后台
    :param site: 站点域名
    :param session: requests会话（可选）
    :param password: 登录密码（优先使用，否则用DEFAULT_PASSWORD）
    """
    if password is None:
        password = DEFAULT_PASSWORD  # 使用固定密码
    if session is None:
        session = requests.Session()
    login_url = f"https://www.{site}/bbwllogin/"
    name = site.replace('.com', '').strip()
    data = {
        'log': f'Ad{name}min',
        'pwd': password,  # 🔴 使用固定密码登录
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
    """增强版nonce获取，增加重试逻辑"""
    for _ in range(2):
        url = f"https://www.{site}/wp-admin/media-new.php"
        resp = request_fn(session, "GET", url)
        if not resp:
            time.sleep(1)
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        # 优先找input标签的nonce
        input_nonce = soup.find("input", {"id": "_wpnonce"})
        if input_nonce:
            return input_nonce.get("value")
        # 从script中找nonce
        for script in soup.find_all("script"):
            if script.string and "_wpnonce" in script.string:
                m = re.search(r'_wpnonce[\'"]?\s*:\s*[\'"]([a-zA-Z0-9]+)', script.string)
                if m:
                    return m.group(1)
        time.sleep(1)
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
        # 使用安全解析函数处理BOM
        js = resp_to_json(resp)
        if js and js.get("data"):
            media = js["data"][0]
            return media.get("id"), media.get("nonces", {}).get("edit")
    except RuntimeError as e:
        print(f"⚠️ query_existing_icon JSON 解析出错: {e}")
    except Exception as e:
        print(f"⚠️ query_existing_icon 未知错误: {e}")
    return None, None


def get_last_icon_path(domain_folder):
    """获取本地icon路径，增加文件大小检查"""
    for root, dirs, files in os.walk(domain_folder):
        if ICON_NAME in files:
            path = os.path.join(root, ICON_NAME)
            # 检查文件大小
            file_size = os.path.getsize(path)
            if file_size > MAX_ICON_SIZE:
                print(f"⚠️ icon文件过大({file_size / 1024 / 1024:.2f}MB)，超过限制: {path}")
                return None
            return path
    return None


def upload_icon(session, site, icon_path, upload_nonce):
    upload_url = f"https://www.{site}/wp-admin/async-upload.php"
    try:
        with open(icon_path, "rb") as f:
            files = {"async-upload": (ICON_NAME, f, "image/png")}
            data = {
                "action": "upload-attachment",
                "_wpnonce": upload_nonce,
                "_wp_http_referer": "/wp-admin/media-new.php",
                "name": ICON_NAME
            }
            headers = {
                "Accept": "*/*",
                "Origin": f"https://www.{site}",
                "Referer": f"https://www.{site}/wp-admin/media-new.php",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = session.post(upload_url, data=data, files=files, headers=headers, verify=False, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"⚠️ upload_icon 请求失败: {e}")
        return None, None

    if resp is None:
        return None, None

    try:
        # 使用安全解析函数处理BOM
        js = resp_to_json(resp)
        if js.get("success") and js.get("data", {}).get("id"):
            return js["data"]["id"], js["data"].get("nonces", {}).get("edit")
    except RuntimeError as e:
        print(f"⚠️ upload_icon JSON 解析出错: {e}")
    except Exception as e:
        print(f"⚠️ upload_icon 未知错误: {e}")
    return None, None


def crop_icon(session, site, media_id, crop_nonce):
    """
    裁剪图标，使用 "full" 参数，让 WordPress 自动处理完整图片并缩放到 512x512
    与第一个脚本行为一致（不强制固定 50x50 裁剪区域）
    """
    ajax_url = f"https://www.{site}/wp-admin/admin-ajax.php"
    crop_data = {
        "_wpnonce": crop_nonce,
        "id": media_id,
        "context": "site-icon",

        # 使用 full，让 WP 自动居中/缩放处理（推荐方式）
        "cropDetails[x1]": 0,
        "cropDetails[y1]": 0,
        "cropDetails[x2]": "full",
        "cropDetails[y2]": "full",
        "cropDetails[width]": "full",
        "cropDetails[height]": "full",

        # 目标输出尺寸
        "cropDetails[dst_width]": 512,
        "cropDetails[dst_height]": 512,

        "action": "crop-image"
    }

    headers = {
        "Referer": f"https://www.{site}/wp-admin/media-new.php",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    resp = session.post(ajax_url, data=crop_data, headers=headers, verify=False, timeout=20)

    if resp and resp.status_code == 200:
        try:
            js = resp_to_json(resp)
            if js and js.get("success") and js.get("data", {}).get("id"):
                return js["data"]["id"]
            else:
                print(f"⚠️ crop_icon 返回 success=false 或无 id: {site}")
        except Exception as e:
            print(f"⚠️ crop_icon JSON 解析出错: {e}")
    else:
        print(f"⚠️ crop_icon 请求失败，状态码: {getattr(resp, 'status_code', '无响应')}")

    return None


def save_wp_settings(session, site, request_fn, site_icon_id=None, date_format=None, time_format=None,
                     week_starts_on=None):
    # （这个函数基本不变，但可以加一点调试输出，便于排查）
    options_url = f"https://www.{site}/wp-admin/options-general.php"
    resp = request_fn(session, "GET", options_url)
    if not resp:
        print(f"⚠️ 无法获取 options-general.php: {site}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"action": "options.php"})
    if not form:
        print(f"⚠️ 未找到 options 表单: {site}")
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
            form_data[name] = textarea_tag.text.strip()

    if site_icon_id:
        form_data["site_icon"] = site_icon_id

    if date_format:
        form_data["date_format"] = date_format
    if time_format:
        form_data["time_format"] = time_format
    if week_starts_on is not None:
        form_data["start_of_week"] = str(week_starts_on)

    # 强制设置 whl_page（防万一）
    form_data["whl_page"] = "bbwllogin"

    save_url = f"https://www.{site}/wp-admin/options.php"
    headers = {"Referer": options_url, "User-Agent": "Mozilla/5.0"}

    resp2 = session.post(save_url, data=form_data, headers=headers, verify=False,
                         allow_redirects=True, timeout=25)

    if resp2 and resp2.status_code in (200, 302):
        return True
    else:
        print(f"⚠️ 保存设置失败，状态码: {getattr(resp2, 'status_code', '无响应')}")
        return False


def process_icon(site_folder, session, request_fn, date_format="F j, Y", time_format="g:i a", week_starts_on=1):
    site = os.path.basename(site_folder)
    print(f"--- ICON: {site}")

    try:
        media_id, crop_nonce = query_existing_icon(session, site, request_fn)

        if media_id and crop_nonce:
            print(f"已有 icon，id={media_id}，尝试裁剪更新尺寸")
            final_id = crop_icon(session, site, media_id, crop_nonce) or media_id
        else:
            icon_path = get_last_icon_path(site_folder)
            if not icon_path:
                raise RuntimeError("本地未找到 icon.png 或文件过大（>5MB）")

            upload_nonce = get_upload_nonce(session, site, request_fn)
            if not upload_nonce:
                raise RuntimeError("获取上传 _wpnonce 失败")

            media_id, crop_nonce = upload_icon(session, site, icon_path, upload_nonce)
            if not media_id:
                raise RuntimeError("上传 icon 失败")

            print(f"上传成功，id={media_id}，开始裁剪/生成 512x512 版本")
            final_id = crop_icon(session, site, media_id, crop_nonce) or media_id

        print(f"最终图标 ID: {final_id}，准备保存到站点设置")
        ok = save_wp_settings(
            session, site, request_fn,
            site_icon_id=final_id,
            date_format=date_format,
            time_format=time_format,
            week_starts_on=week_starts_on
        )

        if not ok:
            raise RuntimeError("保存 WP 设置失败（可能是表单提交被拦截或网络问题）")

        # 成功日志可加更多信息，便于排查
        write_success(site, "ICON", f"id={final_id}")
        print(f"✅ ICON 处理完成: {site}")
        return True

    except Exception as e:
        write_failed(site, "ICON", str(e))
        return False

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
            js = resp_to_json(resp)
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
        js = resp_to_json(resp)
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
        media_list = resp_to_json(resp)
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
    resp = request_fn(session, "POST", url, json=payload, headers=headers)
    return resp is not None


def save_yoast_configuration_state(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user"
    payload = {"finishedSteps": ["optimizeSeoData", "siteRepresentation", "socialProfiles", "personalPreferences"]}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers)
    return resp is not None


def set_yoast_social_profiles(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/social_profiles?_locale=user"
    payload = {"facebook_site": "", "twitter_site": "", "other_social_urls": []}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers)
    return resp is not None


def set_yoast_tracking(session, site, nonce, request_fn):
    url = f"https://www.{site}/wp-json/yoast/v1/configuration/enable_tracking?_locale=user"
    payload = {"tracking": 0}
    headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce}
    resp = request_fn(session, "POST", url, json=payload, headers=headers)
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

def get_wpseo_page_settings_data(session, site, request_fn):
    """
    从 wpseo_page_settings 页面提取必要的动态参数
    返回 dict 或 None（失败时）
    """
    url = f"https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings#/homepage"
    resp = request_fn(session, "GET", url)
    if not resp or resp.status_code != 200:
        print(f"❌ 无法访问 wpseo_page_settings 页面: {site} (状态码: {getattr(resp, 'status_code', '无响应')})")
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
        'company_logo_id': r'"company_logo_id":([^,]+),',  # 可能是数字或字符串
        'company_name': r'"company_name":"([^"]+)"',
        'blogdescription': r'"blogdescription":"([^"]*)"',  # 允许空字符串
    }

    result = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            result[key] = m.group(1).strip()
        else:
            print(f"  ⚠️ 未提取到 {key} (可能页面结构变化)")

    # 规范化 company_logo 中的转义斜杠
    if 'company_logo' in result:
        result['company_logo'] = result['company_logo'].replace('\\/', '/')

    # 必须字段检查（至少这些不能缺）
    required = {'nonce', 'index_now_key', 'version', 'company_logo', 'company_logo_id', 'company_name', 'website_name'}
    missing = required - set(result.keys())
    if missing:
        print(f"❌ 关键参数缺失，无法继续高级 Yoast 配置: {missing}")
        return None

    print(f"  已提取 {len(result)} 个 Yoast 参数")
    return result


def process_yoast_advanced(site, session, request_fn):
    """
    执行详细的 Yoast SEO 配置提交（完整表单字段，防止清空）
    """
    print(f"--- 开始高级 Yoast 配置 (防止清空) : {site} ---")

    params = get_wpseo_page_settings_data(session, site, request_fn)
    if not params:
        write_failed(site, "YOAST_ADVANCED", "无法提取 wpseo_page_settings 参数")
        return False

    # ── 下面是第二个脚本里完整的 data 字典 ──
    # 只替换动态部分，其余保持原样
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

        # 标题 & 元描述核心部分
        'wpseo_titles[forcerewritetitle]': 'false',
        'wpseo_titles[separator]': 'sc-dash',
        'wpseo_titles[title-home-wpseo]': '%%sitename%% ',
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
        'wpseo_titles[metadesc-home-wpseo]': '%%sitedesc%% ',
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
        'wpseo_titles[website_name]': params.get('website_name', site),
        'wpseo_titles[person_name]': '',
        'wpseo_titles[person_logo]': '',
        'wpseo_titles[person_logo_id]': '0',
        'wpseo_titles[alternate_website_name]': '',
        'wpseo_titles[company_logo]': params.get('company_logo', ''),
        'wpseo_titles[company_logo_id]': params.get('company_logo_id', '0'),
        'wpseo_titles[company_name]': params.get('company_name', site),
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

        # 可以根据需要在这里继续添加更多字段（如果你的第二个脚本还有遗漏的）
    }

    url = f"https://www.{site}/wp-admin/options.php"
    headers = {
        "Referer": f"https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = session.post(url, data=data, headers=headers, verify=False, timeout=40, allow_redirects=True)
        if resp and resp.status_code in (200, 302):
            print(f"✅ 高级 Yoast 配置提交成功: {site}")
            write_success(site, "YOAST_ADVANCED", f"完整表单提交")
            return True
        else:
            status = getattr(resp, 'status_code', '无响应')
            print(f"❌ 提交失败 - 状态码: {status}")
            write_failed(site, "YOAST_ADVANCED", f"状态码 {status}")
            return False
    except Exception as e:
        write_failed(site, "YOAST_ADVANCED", f"请求异常: {str(e)}")
        return False

# ---------- SITE PROCESS ----------
def process_site(site_folder, args):
    site = os.path.basename(site_folder)
    print(f"\n==== 处理站点: {site} ====")
    try:
        session = login(site, password=args.password)  # 🔴 使用固定密码（或命令行传入的密码）
    except Exception as e:
        write_failed(site, "登录", str(e))
        return

    only = args.only or ""
    only_set = set(x.strip().lower() for x in only.split(",") if x.strip()) if only else None

    if (only_set is None) or ("icon" in only_set):
        try:
            process_icon(site_folder, session, request_with_retry,
                         date_format=args.date_format,
                         time_format=args.time_format,
                         week_starts_on=args.week_starts_on)
        except Exception as e:
            write_failed(site, "ICON", str(e))

    if (only_set is None) or ("banner" in only_set):
        try:
            process_banner(site_folder, session, request_with_retry)
        except Exception as e:
            write_failed(site, "BANNER", str(e))

    if (only_set is None) or ("yoast" in only_set):
        try:
            process_yoast(site, session, request_with_retry)
        except Exception as e:
            write_failed(site, "YOAST", str(e))

        try:
            process_yoast_advanced(site, session, request_with_retry)
        except Exception as e:
            write_failed(site, "YOAST_ADVANCED", str(e))


# ---------- MAIN ----------
def main():
    parser = argparse.ArgumentParser(description="批量处理 WP: ICON / BANNER / YOAST")
    parser.add_argument("--base-dir", default=BASE_DIR, help="站点根目录，子文件夹为域名")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="WP 登录密码（默认使用固定值 f!XsS$J2WneOkMyUgQ）")
    parser.add_argument("--date-format", default="F j, Y", help='WP 日期格式，默认 "F j, Y"')
    parser.add_argument("--time-format", default="g:i a", help='WP 时间格式，默认 "g:i a"')
    parser.add_argument("--week-starts-on", default=1, type=int, choices=range(0, 7), help="周起始日 0=周日,1=周一 ...")
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