"""WordPress 站群菜单打开器

从站群网址打开 Chrome 扩展中提取的核心功能：
- 域名规范化（去除协议、www 前缀，统一为 www.[domain]）
- 用户名推导（Ad[domain-without-.com]min）
- WordPress 登录 URL 与菜单 URL 构建
- 带重试的登录请求

在 Web 应用中通过浏览器端自动提交登录表单的方式实现：
服务器渲染一个包含登录表单的页面，页面加载后自动提交到
WordPress 的 /bbwllogin/ 登录端点，登录成功后浏览器跳转到
/wp-admin/nav-menus.php 菜单页面。
"""

import os
import time
from urllib.parse import urlparse

import requests
import urllib3

from qmds.utils.logger import get_logger

log = get_logger("wp_menu_opener")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 默认 WordPress 登录密码（与扩展及 SiteOperator 保持一致）
DEFAULT_WP_PASSWORD = "f!XsS$J2WneOkMyUgQ"

# 登录路径（与项目其他模块一致）
LOGIN_PATH = "/bbwllogin/"

# WordPress 后台路径
ADMIN_PATH = "/wp-admin/"

# 菜单页面路径
MENU_PATH = "/wp-admin/nav-menus.php"

# 登录请求重试次数
RETRY_COUNT = 3

# 重试间隔（毫秒，递增）
RETRY_DELAY_MS = 800


def normalize_site(raw_site_url: str) -> dict:
    """规范化站点 URL

    与扩展 background.js 中的 normalizeSite 逻辑一致：
    - 去除协议和路径，仅保留主机名
    - 去除 www 前缀
    - 推导用户名部分（去除 .com 后缀）

    Args:
        raw_site_url: 原始输入，可以是域名或完整 URL

    Returns:
        {"domain": str, "hostname": str, "origin": str, "name_part": str, "username": str}

    Raises:
        ValueError: 输入不是有效域名
    """
    raw = str(raw_site_url or "").strip()
    if not raw:
        raise ValueError("请输入有效域名，例如 example.com。")

    if "://" not in raw:
        url = urlparse(f"https://{raw}")
    else:
        url = urlparse(raw)

    hostname = (url.hostname or "").lower()
    host_parts = [p for p in hostname.split(".") if p]

    if len(host_parts) < 2:
        raise ValueError("请输入有效域名，例如 example.com。")

    domain_parts = host_parts[1:] if host_parts[0] == "www" else host_parts
    domain = ".".join(domain_parts)
    name_part = domain
    if name_part.lower().endswith(".com"):
        name_part = name_part[:-4]

    full_hostname = f"www.{domain}"
    origin = f"https://{full_hostname}"
    username = f"Ad{name_part}min"

    return {
        "domain": domain,
        "hostname": full_hostname,
        "origin": origin,
        "name_part": name_part,
        "username": username,
    }


def build_login_form_data(site_info: dict, password: str) -> dict:
    """构建 WordPress 登录表单数据

    注意：不包含 testcookie 字段。WordPress 仅在 POST 数据中存在
    testcookie 时才会校验浏览器是否预先设置了 wordpress_test_cookie
    （该 Cookie 由登录页加载时通过 JS 写入）。由于本流程是浏览器
    直接 POST 登录端点而未先访问登录页，跳过该字段可避免
    "Cookies are blocked or not supported by your browser" 错误。

    redirect_to 直接指向菜单页面，登录成功后由 WordPress 跳转。

    Args:
        site_info: normalize_site 返回的站点信息
        password: WordPress 登录密码

    Returns:
        登录表单字段字典
    """
    menu_url = build_menu_url(site_info)
    return {
        "log": site_info["username"],
        "pwd": password,
        "wp-submit": "Log In",
        "redirect_to": menu_url,
    }


def build_menu_url(site_info: dict) -> str:
    """构建菜单页面 URL

    Args:
        site_info: normalize_site 返回的站点信息

    Returns:
        /wp-admin/nav-menus.php 的完整 URL
    """
    return f"{site_info['origin']}{MENU_PATH}"


def build_login_url(site_info: dict) -> str:
    """构建登录端点 URL

    Args:
        site_info: normalize_site 返回的站点信息

    Returns:
        /bbwllogin/ 的完整 URL
    """
    return f"{site_info['origin']}{LOGIN_PATH}"


def is_login_page_url(url: str) -> bool:
    """判断 URL 是否仍为登录页面

    Args:
        url: 请求最终跳转的 URL

    Returns:
        True 表示仍在登录页面（未登录成功）
    """
    return LOGIN_PATH in url or "wp-login.php" in url


def prepare_menu_url(raw_site_url: str, password: str) -> dict:
    """服务端预检：尝试登录并返回菜单页面 URL

    与扩展 prepareMenuUrl 逻辑一致，用于验证登录是否成功。
    在浏览器自动提交表单前，服务端先验证域名可访问且凭据有效。

    Args:
        raw_site_url: 原始域名或 URL
        password: WordPress 登录密码

    Returns:
        {"success": bool, "menu_url": str, "error": str}
    """
    try:
        site_info = normalize_site(raw_site_url)
    except ValueError as e:
        return {"success": False, "menu_url": "", "error": str(e)}

    login_url = build_login_url(site_info)
    admin_url = f"{site_info['origin']}{ADMIN_PATH}"
    menu_url = build_menu_url(site_info)
    form_data = build_login_form_data(site_info, password)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/136.0.0.0 Safari/537.36",
    })
    session.verify = False

    response = _request_with_retry(
        session, "POST", login_url,
        data=form_data, allow_redirects=True
    )

    if response is None:
        return {"success": False, "menu_url": "", "error": "登录请求失败，已重试。请检查域名是否可访问。"}

    if not response.ok:
        return {"success": False, "menu_url": "", "error": f"登录请求失败：HTTP {response.status_code}"}

    if not is_login_page_url(response.url):
        return {"success": True, "menu_url": menu_url, "error": ""}

    admin_check = _request_with_retry(session, "GET", admin_url, allow_redirects=True)

    if admin_check is not None and admin_check.ok and not is_login_page_url(admin_check.url):
        return {"success": True, "menu_url": menu_url, "error": ""}

    return {"success": False, "menu_url": "", "error": "登录失败，无法访问 /wp-admin/。请检查域名、用户名规则或密码。"}


def _request_with_retry(session, method, url, retries=RETRY_COUNT, **kwargs):
    """带重试机制的 HTTP 请求

    与扩展 requestWithRetry 逻辑一致。
    """
    for attempt in range(1, retries + 1):
        try:
            return session.request(method, url, timeout=30, **kwargs)
        except requests.exceptions.RequestException as e:
            log.warning(f"请求异常 ({attempt}/{retries}): {method} {url} - {e}")
            if attempt == retries:
                return None
            time.sleep(RETRY_DELAY_MS * attempt / 1000)
    return None


def resolve_wp_password(site_db) -> str:
    """从数据库配置解析 WordPress 密码

    优先使用数据库中的 wp_password 配置，为空时回退到环境变量，
    最后使用默认密码。

    Args:
        site_db: SiteDBClient 实例

    Returns:
        WordPress 登录密码
    """
    try:
        pwd = site_db.get_setting("wp_password")
        if pwd:
            return pwd
    except Exception as e:
        log.warning(f"读取 wp_password 配置失败: {e}")

    env_pwd = os.environ.get("WP_PASSWORD", "")
    if env_pwd:
        return env_pwd

    return DEFAULT_WP_PASSWORD
