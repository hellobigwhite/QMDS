import os
import re
import time
import json
import random
import hashlib
import requests

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXCLUDED = {
    "best seller", "featured", "accessories", "other", "new arrival",
    "exclusive", "limited edition", "hot sale", "most popular",
    "trending", "special offer", "flash sale", "ACCESSORIES", "general"
}
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "are", "not", "but", "all", "can", "has", "its", "was"}


def safe_json(resp):
    text = resp.text.strip()
    for prefix, suffix in (('{', '}'), ('[', ']')):
        start = text.find(prefix)
        if start >= 0:
            end = text.rfind(suffix)
            if end > start:
                text = text[start:end + 1]
                break
    return json.loads(text)


def request_with_retry(session, method, url, retries=3, delay=3, **kwargs):
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=120, **kwargs)
            if resp is not None:
                return resp
        except requests.exceptions.RequestException:
            pass
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(delay)
    return None


def extract_words(name):
    words = re.findall(r'[a-z]+', name.strip().lower())
    return [w for w in words if len(w) > 2 and w not in STOP_WORDS]


def norm_name(name):
    n = name.lower().strip().replace("\xa0", " ")
    if n.endswith('s') and len(n) > 3:
        return n[:-1]
    return n


def derive_site_url(domain):
    d = domain.strip().lower()
    if not d.startswith("www."):
        d = "www." + d
    return f"https://{d}"


def wp_login(session, site_url, password=None):
    domain = site_url.replace("https://www.", "").replace("/", "")
    name = domain.replace('.com', '').strip()
    username = f"Ad{name}Min"
    if password is None:
        password = os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")
    login_url = f"{site_url}/bbwllogin/"
    data = {"log": username, "pwd": password, "wp-submit": "Log In",
            "redirect_to": f"{site_url}/wp-admin/", "testcookie": "1"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}
    session.post(login_url, data=data, headers=headers, verify=False, timeout=20)
    logged_in = any("wordpress_logged_in" in c.name for c in session.cookies)
    if not logged_in:
        try:
            check = session.get(f"{site_url}/wp-admin/", verify=False, timeout=15)
            logged_in = check.status_code == 200 and "wp-admin" in check.url
        except Exception:
            pass
    if not logged_in:
        raise RuntimeError("Login failed")


def fetch_all_categories(session, site_url):
    """Fetch all categories from cf-updata/category/categorySearch.php with pagination.
    Returns list of {term_id, term_name, parent_id, slug, product_count, is_main}.
    """
    all_items = []
    search_url = f"{site_url}/cf-updata/category/categorySearch.php"
    for page in range(1, 500):
        resp = request_with_retry(
            session, "POST", search_url,
            data={"page": str(page), "limit": "25", "category_name": ""},
            headers={"User-Agent": "Mozilla/5.0"}, retries=2
        )
        if resp is None or resp.status_code != 200:
            break
        try:
            data = safe_json(resp)
        except Exception:
            break
        if data.get("code") != 0:
            break
        items = data.get("data", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < 25:
            break
    return all_items


def fetch_category_tree(session, site_url):
    """Fetch category tree from edit_category.php by parsing var allCategoryData.
    Returns list of {id, title, children: [...]}.
    """
    resp = request_with_retry(
        session, "GET", f"{site_url}/cf-updata/category/edit_category.php",
        headers={"User-Agent": "Mozilla/5.0"}, retries=2
    )
    if resp is None or resp.status_code != 200:
        return []
    m = re.search(r'var\s+allCategoryData\s*=\s*(\[.*?\]);\s*\n', resp.text, re.S)
    if not m:
        return []
    try:
        tree = json.loads(m.group(1))
    except json.JSONDecodeError:
        try:
            tree = json.loads(m.group(1).replace('\\"', '"'))
        except Exception:
            return []
    return tree


def detect_nav_width(session, site_url):
    r = session.get(site_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if r.status_code != 200:
        return 1140
    patterns = [
        r'\.site-header[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'#masthead[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'\.container[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'\.nav[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'\.primary-menu[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'\.menu[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'#primary[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'\.wrapper[\s\S]{0,500}?max-width\s*:\s*(\d+)px',
        r'max-width\s*:\s*(\d+)px',
    ]
    for pat in patterns:
        m = re.search(pat, r.text, re.I)
        if m:
            w = int(m.group(1))
            if 600 <= w <= 2000:
                return w
    return 1140


def build_menu_list(all_cats, tree_data, site_name="", nav_width=1140):
    """Build menuList JSON structure.
    - Real secondary: from tree_data (edit_category.php allCategoryData)
    - Fake secondary: child name must contain parent's name as whole word (>= 16 products)
    - site_name: used as randomization seed for menu variation between sites
    Returns: list of {id, title, children: [{id, title, children: []}]}
    """
    seed = int(hashlib.md5(site_name.encode()).hexdigest()[:8], 16) if site_name else 0
    rng = random.Random(seed)

    id_to_info = {}
    for cat in all_cats:
        tid = str(cat.get("term_id", ""))
        if not tid:
            continue
        id_to_info[tid] = {
            "term_id": tid,
            "term_name": cat.get("term_name", ""),
            "parent_id": str(cat.get("parent_id", "0")),
            "product_count": int(cat.get("product_count", 0)),
            "slug": cat.get("slug", "")
        }

    excluded_norm = {norm_name(n) for n in EXCLUDED}
    filtered = {tid: info for tid, info in id_to_info.items()
                if norm_name(info["term_name"]) not in excluded_norm
                and info["term_name"].lower() not in EXCLUDED}

    tree_id_to_node = {}

    def flatten_tree(nodes):
        for node in nodes:
            nid = str(node.get("id", ""))
            if nid:
                tree_id_to_node[nid] = node
            if node.get("children"):
                flatten_tree(node["children"])

    flatten_tree(tree_data)

    def get_leaf_nodes(node):
        children = node.get("children", [])
        if not children:
            return [str(node.get("id", ""))]
        leaves = []
        for child in children:
            leaves.extend(get_leaf_nodes(child))
        return leaves

    real_children_of = {}
    tree_child_ids = set()
    has_tree_children = set()
    for nid, node in tree_id_to_node.items():
        children = node.get("children", [])
        if not children:
            continue
        has_tree_children.add(nid)
        leaf_ids = get_leaf_nodes(node)
        valid_leaves = [lid for lid in leaf_ids if lid and lid in filtered and lid != nid]
        if valid_leaves:
            real_children_of[nid] = valid_leaves
            tree_child_ids.update(valid_leaves)
        for child in children:
            child_id = str(child.get("id", ""))
            if child_id:
                tree_child_ids.add(child_id)

    top_level = [(tid, info) for tid, info in filtered.items() if tid not in tree_child_ids]
    top_level.sort(key=lambda x: -x[1]["product_count"])

    target_top = max(5, min(10, nav_width // 130))

    candidates_with_children = [(tid, info) for tid, info in top_level
                                if tid in real_children_of and info["product_count"] >= 500]
    candidates_without_children = [(tid, info) for tid, info in top_level
                                   if tid not in real_children_of and info["product_count"] >= 500]
    candidates = candidates_with_children + candidates_without_children

    def add_variation(cands):
        if len(cands) <= target_top:
            return cands
        selected = []
        remaining = list(cands)
        for _ in range(target_top):
            if not remaining:
                break
            weights = [info["product_count"] for _, info in remaining]
            total = sum(weights)
            pick = rng.random() * total
            cumulative = 0
            for idx, (tid, info) in enumerate(remaining):
                cumulative += info["product_count"]
                if cumulative >= pick:
                    selected.append((tid, info))
                    remaining.pop(idx)
                    break
        return selected

    candidates = add_variation(candidates)

    used_ids = set()
    menu_top = []
    for tid, info in candidates:
        if len(menu_top) >= target_top:
            break
        if tid in used_ids:
            continue
        menu_top.append(tid)
        used_ids.add(tid)

    fake_children = {}
    assigned_as_child = set()

    parent_noun_map = {}
    for tid in menu_top:
        info = filtered.get(tid)
        if not info:
            continue
        words = extract_words(info["term_name"])
        if 1 <= len(words) <= 2:
            parent_noun_map[info["term_name"].lower().strip()] = tid

    for tid, info in filtered.items():
        if info["product_count"] < 16:
            continue
        if tid in has_tree_children:
            continue
        if tid in tree_child_ids:
            continue
        child_name = info["term_name"].lower().strip()
        child_len = len(child_name)
        for parent_name, parent_tid in parent_noun_map.items():
            if parent_tid == tid:
                continue
            parent_len = len(parent_name)
            if parent_len >= child_len:
                continue
            if parent_name not in child_name:
                continue
            if not re.search(r'\b' + re.escape(parent_name) + r'\b', child_name):
                continue
            if parent_tid in real_children_of:
                continue
            fake_children.setdefault(parent_tid, []).append(tid)
            assigned_as_child.add(tid)
            break

    menu_top = [tid for tid in menu_top if tid not in assigned_as_child]

    if len(menu_top) < target_top:
        current_top_set = set(menu_top)
        for tid, info in candidates:
            if len(menu_top) >= target_top:
                break
            if tid in current_top_set:
                continue
            if tid in assigned_as_child:
                continue
            menu_top.append(tid)
            current_top_set.add(tid)

    menu_list = []

    for tid in menu_top:
        info = filtered.get(tid)
        if not info:
            continue
        entry = {"id": tid, "title": info["term_name"], "children": []}

        for child_tid in real_children_of.get(tid, []):
            cinfo = filtered.get(child_tid)
            if cinfo:
                entry["children"].append({"id": child_tid, "title": cinfo["term_name"], "children": []})

        for child_tid in fake_children.get(tid, []):
            cinfo = filtered.get(child_tid)
            if cinfo:
                if not any(c["id"] == child_tid for c in entry["children"]):
                    entry["children"].append({"id": child_tid, "title": cinfo["term_name"], "children": []})

        menu_list.append(entry)

    return menu_list


def apply_main_cat(menu_list, main_cat_name, all_cats):
    """Place main_cat (and its parent) right after the first position.
    Returns modified menuList.
    """
    if not main_cat_name:
        return menu_list

    main_info = None
    for cat in all_cats:
        if cat.get("term_name", "").lower() == main_cat_name.lower():
            main_info = cat
            break
    if not main_info:
        for cat in all_cats:
            if norm_name(cat.get("term_name", "")) == norm_name(main_cat_name):
                main_info = cat
                break
    if not main_info:
        return menu_list

    main_tid = str(main_info.get("term_id", ""))
    main_name = main_info.get("term_name", main_cat_name)
    parent_id = str(main_info.get("parent_id", "0"))

    parent_info = None
    if parent_id and parent_id != "0":
        for cat in all_cats:
            if str(cat.get("term_id", "")) == parent_id:
                parent_info = cat
                break

    def remove_from_list(ml, target_id):
        for item in ml:
            if item["id"] == target_id:
                ml.remove(item)
                return item
            for child in item.get("children", []):
                if child["id"] == target_id:
                    item["children"].remove(child)
                    return child
        return None

    remove_from_list(menu_list, main_tid)
    if parent_info:
        remove_from_list(menu_list, str(parent_info.get("term_id", "")))

    insert_idx = 1

    if parent_info:
        parent_tid = str(parent_info.get("term_id", ""))
        parent_name = parent_info.get("term_name", "")
        parent_entry = {"id": parent_tid, "title": parent_name, "children": [
            {"id": main_tid, "title": main_name, "children": []}
        ]}
        menu_list.insert(insert_idx, parent_entry)
    else:
        main_entry = {"id": main_tid, "title": main_name, "children": []}
        menu_list.insert(insert_idx, main_entry)

    return menu_list


def post_menu_list(session, site_url, menu_list):
    """POST menuList to updateCategory.php."""
    url = f"{site_url}/cf-updata/category/updateCategory.php"
    menu_json = json.dumps(menu_list, ensure_ascii=False)
    resp = request_with_retry(
        session, "POST", url,
        data={"menuList": menu_json},
        headers={"User-Agent": "Mozilla/5.0"}, retries=3
    )
    if resp is None:
        return {"success": False, "message": "updateCategory.php 无响应"}
    try:
        result = safe_json(resp)
        if result.get("code") == 0 or result.get("error") is False:
            return {"success": True, "message": result.get("msg", "菜单更新成功")}
        return {"success": False, "message": result.get("msg", f"HTTP {resp.status_code}")}
    except Exception:
        if resp.status_code == 200:
            return {"success": True, "message": "菜单更新成功 (status 200)"}
        return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}


class WpMenuConfigurator:
    """Configure WordPress navigation menu via menuList JSON submission.

    Fetches categories from categorySearch.php and edit_category.php,
    builds menu structure via weighted-random algorithm with per-site
    variation, applies main category placement, and submits via
    updateCategory.php.
    """

    def __init__(self, password=None):
        self._password = password
        self._session = requests.Session()
        self._session.verify = False

    def configure(self, domain, main_cat=None, progress_callback=None):
        """配置菜单

        Args:
            domain: 域名
            main_cat: 主分类名称（含|||时取最后一段），置顶到菜单
            progress_callback: 进度回调函数

        Returns:
            {"success": bool, "message": str, "menu_list": list}
        """
        site_url = derive_site_url(domain)

        if progress_callback:
            progress_callback("登录站点...")
        wp_login(self._session, site_url, self._password)

        if progress_callback:
            progress_callback("获取分类数据...")
        all_cats = fetch_all_categories(self._session, site_url)
        if not all_cats:
            return {"success": False, "message": "未获取到分类数据", "menu_list": []}

        if progress_callback:
            progress_callback("获取分类树结构...")
        tree_data = fetch_category_tree(self._session, site_url)

        if progress_callback:
            progress_callback("构建菜单结构...")
        nav_width = detect_nav_width(self._session, site_url)
        menu_list = build_menu_list(all_cats, tree_data,
                                    site_name=domain, nav_width=nav_width)

        if main_cat and main_cat.lower() not in ("home", "shop", "none", ""):
            cat_name = main_cat.rsplit("|||", 1)[-1].strip() if "|||" in main_cat else main_cat.strip()
            if progress_callback:
                progress_callback(f"应用主分类: {cat_name}...")
            menu_list = apply_main_cat(menu_list, cat_name, all_cats)

        if progress_callback:
            progress_callback(f"提交菜单({len(menu_list)}个顶级)...")
        result = post_menu_list(self._session, site_url, menu_list)

        return {"success": result["success"], "message": result["message"], "menu_list": menu_list}
