import requests
import time
from collections import defaultdict

# ================== 配置区 ==================

USE_REMOTE_CATEGORIES_ONLY = True

REMOTE_CATEGORY_API = "https://www.bashwheels.com/cf-updata/category/categorySearch.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

WP_ADMIN_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
}

REQUEST_TIMEOUT = 20

# ================== 分类获取 ==================

def fetch_all_remote_categories(limit=25):
    """自动翻页获取全部分类"""
    all_rows = []
    page = 1

    while True:
        resp = requests.post(
            REMOTE_CATEGORY_API,
            headers=HEADERS,
            data={"page": page, "limit": limit},
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("code") != 0:
            raise RuntimeError(payload)

        rows = payload.get("data", [])
        if not rows:
            break

        all_rows.extend(rows)

        total = int(payload.get("count", 0))
        if len(all_rows) >= total:
            break

        page += 1

    # term_id 去重
    unique = {}
    for r in all_rows:
        unique[r["term_id"]] = r

    print(f"[OK] 远端分类获取完成，总数：{len(unique)}")
    return list(unique.values())

# ================== 构造父子路径 ==================

def build_parent_child_paths(rows):
    """构造 父|||子"""
    parent_map = {r["term_id"]: r for r in rows if r["parent_id"] == "0"}
    result = []

    for r in rows:
        if r["parent_id"] != "0":
            parent = parent_map.get(r["parent_id"])
            if parent:
                result.append({
                    "parent": parent["term_name"],
                    "child": r["term_name"],
                    "slug": r.get("slug", "")
                })

    print(f"[OK] 构造父子分类路径：{len(result)} 条")
    return result

# ================== WP 菜单操作 ==================

def get_wp_nonce_and_menu(session, domain):
    url = f"https://www.{domain}/wp-admin/nav-menus.php"
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html = r.text

    def extract(name):
        key = f'name="{name}" value="'
        start = html.find(key)
        if start == -1:
            return None
        start += len(key)
        end = html.find('"', start)
        return html[start:end]

    return {
        "_wpnonce": extract("_wpnonce"),
        "menu": extract("menu"),
    }

def get_product_categories(session, domain):
    """从 WP 后台拉取现有 product_cat"""
    url = f"https://www.{domain}/wp-admin/admin-ajax.php"
    data = {
        "action": "menu-get-metabox",
        "metabox": "product_cat",
        "page": 1,
    }
    r = session.post(url, headers=WP_ADMIN_HEADERS, data=data, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

def add_menu_item(session, domain, menu_id, nonce, title, slug):
    """添加一个分类菜单项"""
    url = f"https://www.{domain}/wp-admin/admin-ajax.php"
    data = {
        "action": "add-menu-item",
        "menu": menu_id,
        "_wpnonce": nonce,
        "menu-item[-1][menu-item-type]": "taxonomy",
        "menu-item[-1][menu-item-object]": "product_cat",
        "menu-item[-1][menu-item-title]": title,
        "menu-item[-1][menu-item-url]": f"/product-category/{slug}/",
        "menu-item[-1][menu-item-status]": "publish",
    }
    r = session.post(url, headers=WP_ADMIN_HEADERS, data=data, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

# ================== 主流程 ==================

def process_domain(domain, cookies):
    session = requests.Session()
    session.headers.update(WP_ADMIN_HEADERS)
    session.cookies.update(cookies)

    nonce_info = get_wp_nonce_and_menu(session, domain)
    menu_id = nonce_info["menu"]
    nonce = nonce_info["_wpnonce"]

    if not menu_id or not nonce:
        raise RuntimeError("获取 menu_id 或 nonce 失败")

    print(f"[{domain}] menu_id={menu_id}")

    rows = fetch_all_remote_categories()
    paths = build_parent_child_paths(rows)

    for item in paths:
        print(f"[ADD] {item['parent']}|||{item['child']}")
        add_menu_item(
            session,
            domain,
            menu_id,
            nonce,
            item["child"],
            item["slug"]
        )
        time.sleep(0.3)

    print(f"[{domain}] 菜单添加完成")

# ================== 入口 ==================

if __name__ == "__main__":
    """
    你需要提前准备好：
    1. 已登录 WP 后台的 cookie
    2. domain（不带 www）
    """

    DOMAIN = "example.com"  # 改成你的站点
    COOKIES = {
        # 从浏览器复制 wp-admin 登录后的 cookie
        # 'wordpress_logged_in_xxx': 'xxxx'
    }

    process_domain(DOMAIN, COOKIES)
