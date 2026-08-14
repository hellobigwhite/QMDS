import os, re, time, json, html, sys
import requests
import openpyxl
import argparse

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

XLSX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), r"D:\Downloads\get_cs_ids(1)\建站域名管理.xlsx")

def load_domain_info():
    """Load 建站域名管理.xlsx → {domain: {build_time, main_cat, big_cat}}"""
    if not os.path.exists(XLSX_PATH):
        return {}
    wb = openpyxl.load_workbook(XLSX_PATH)
    ws = wb.active
    info = {}
    for r in range(2, ws.max_row + 1):
        domain = ws.cell(r, 1).value
        if not domain:
            continue
        domain = str(domain).strip().lower()
        mc = ws.cell(r, 12).value
        if mc and isinstance(mc, str) and '|||' in mc:
            mc = mc.split('|||')[-1].strip()
        elif mc:
            mc = str(mc).strip()
        else:
            mc = ''
        bc = ws.cell(r, 7).value
        bc = str(bc).strip() if bc else ''
        bt = ws.cell(r, 10).value
        bt = str(bt).strip() if bt else ''
        info[domain] = {"build_time": bt, "main_cat": mc, "big_cat": bc}
    return info

EXCLUDED = {
    "best seller", "featured", "accessories", "other", "new arrival",
    "exclusive", "limited edition", "hot sale", "most popular",
    "trending", "special offer", "flash sale", "ACCESSORIES", "general"
}
BROAD_KEYWORDS = {"gear", "accessories", "parts", "components", "equipment", "supplies", "tools", "sets", "kits"}
CATCHALL_KEYWORDS = {"gear", "accessories", "equipment", "supplies"}
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "are", "not", "but", "all", "can", "has", "its", "was"}

def safe_json(resp):
    """Parse JSON from response, stripping leading/trailing non-JSON content (e.g. PHP warnings)."""
    text = resp.text.strip()
    for prefix, suffix in (('{', '}'), ('[', ']')):
        start = text.find(prefix)
        if start >= 0:
            end = text.rfind(suffix)
            if end > start:
                text = text[start:end+1]
                break
    return json.loads(text)

def request_with_retry(session, method, url, retries=3, delay=3, **kwargs):
    for i in range(retries):
        try:
            resp = session.request(method, url, timeout=120, **kwargs)
            if resp is not None:
                return resp
            print(f"    [WARN] {method} {url[:50]} returned None")
        except requests.exceptions.RequestException as e:
            print(f"    [req err] {type(e).__name__}: {e}")
        except Exception as e:
            print(f"    [unexpected] {type(e).__name__}: {e}")
        if i < retries - 1:
            time.sleep(delay)
    return None

def extract_words(name):
    words = re.findall(r'[a-z]+', name.strip().lower())
    return [w for w in words if len(w) > 2 and w not in STOP_WORDS]

def clean_slug(name):
    slug = name.lower().replace("\xa0", "-").replace(" ", "-").replace("/", "-").replace("&", "and")
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug

def norm_name(name):
    n = name.lower().strip().replace("\xa0", " ")
    if n.endswith('s') and len(n) > 3:
        return n[:-1]
    return n

def ci_find(name, items):
    nn = norm_name(name)
    for i, (n, p, f) in enumerate(items):
        if norm_name(n) == nn:
            return i, (n, p, f)
    return None, None

def derive_site_url(folder_name):
    domain = folder_name.strip().lower()
    if not domain.startswith("www."):
        domain = "www." + domain
    return f"https://{domain}"

DEFAULT_PASSWORD = os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")

def wp_login(session, site_url):
    domain = site_url.replace("https://www.", "").replace("/", "")
    name = domain.replace('.com', '').strip()
    username = f"Ad{name}Min"
    password = DEFAULT_PASSWORD
    login_url = f"{site_url}/bbwllogin/"
    data = {"log": username, "pwd": password, "wp-submit": "Log In", "redirect_to": f"{site_url}/wp-admin/", "testcookie": "1"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}
    session.post(login_url, data=data, headers=headers, verify=False, timeout=20)
    logged_in = any("wordpress_logged_in" in c.name for c in session.cookies)
    if not logged_in:
        try:
            check = session.get(f"{site_url}/wp-admin/", verify=False, timeout=15)
            logged_in = check.status_code == 200 and "wp-admin" in check.url
        except:
            pass
    if not logged_in:
        raise RuntimeError("Login failed")
    print(f"  Login OK as {username}")

def fetch_all_categories(session, site_url):
    """Fetch all categories from cf-updata/category/categorySearch.php with pagination.
    Returns list of {term_id, term_name, parent_id, slug, product_count, is_main}."""
    all_items = []
    search_url = f"{site_url}/cf-updata/category/categorySearch.php"
    for page in range(1, 500):
        resp = request_with_retry(
            session, "POST", search_url,
            data={"page": str(page), "limit": "25", "category_name": ""},
            headers={"User-Agent": "Mozilla/5.0"}, retries=2
        )
        if resp is None:
            print(f"  [WARN] categorySearch.php page {page}: no response")
            break
        if resp.status_code != 200:
            print(f"  [WARN] categorySearch.php page {page}: HTTP {resp.status_code}")
            break
        try:
            data = safe_json(resp)
        except Exception as e:
            print(f"  [WARN] categorySearch.php page {page}: JSON parse error: {e}")
            print(f"  [WARN] response: {resp.text[:300]}")
            break
        if data.get("code") != 0:
            print(f"  [WARN] categorySearch.php page {page}: code={data.get('code')}")
            break
        items = data.get("data", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < 25:
            break
    print(f"  Fetched {len(all_items)} categories from categorySearch.php")
    return all_items


def fetch_category_tree(session, site_url):
    """Fetch category tree from edit_category.php by parsing var allCategoryData.
    Returns list of {id, title, children: [...]}."""
    resp = request_with_retry(
        session, "GET", f"{site_url}/cf-updata/category/edit_category.php",
        headers={"User-Agent": "Mozilla/5.0"}, retries=2
    )
    if resp is None or resp.status_code != 200:
        print(f"  [WARN] Failed to fetch edit_category.php")
        return []
    m = re.search(r'var\s+allCategoryData\s*=\s*(\[.*?\]);\s*\n', resp.text, re.S)
    if not m:
        print(f"  [WARN] allCategoryData not found in edit_category.php")
        return []
    try:
        tree = json.loads(m.group(1))
    except json.JSONDecodeError:
        try:
            tree = json.loads(m.group(1).replace('\\"', '"'))
        except Exception:
            print(f"  [WARN] Failed to parse allCategoryData JSON")
            return []
    print(f"  Parsed {len(tree)} top-level categories from edit_category.php")
    return tree


def build_menu_list(all_cats, tree_data, site_name="", main_cat=None, nav_width=1140):
    """Build menuList JSON structure.
    - Real secondary: from tree_data (edit_category.php allCategoryData)
    - Fake secondary: child name must contain parent's name as whole word (>= 16 products)
    - main_cat: if provided, place it (and its parent) right after Home
    - site_name: used as randomization seed for menu variation between sites
    Returns: list of {id, title, children: [{id, title, children: []}]}
    """
    import random
    import hashlib

    # Use domain as randomization seed for consistent but different menus per site
    seed = int(hashlib.md5(site_name.encode()).hexdigest()[:8], 16) if site_name else 0
    rng = random.Random(seed)

    # Build lookup maps from all_cats (categorySearch.php data)
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

    # Filter out excluded categories
    excluded_norm = {norm_name(n) for n in EXCLUDED}
    filtered = {tid: info for tid, info in id_to_info.items()
                if norm_name(info["term_name"]) not in excluded_norm
                and info["term_name"].lower() not in EXCLUDED}

    # Build tree id->info map from edit_category.php
    tree_id_to_node = {}
    def flatten_tree(nodes):
        for node in nodes:
            nid = str(node.get("id", ""))
            if nid:
                tree_id_to_node[nid] = node
            if node.get("children"):
                flatten_tree(node["children"])
    flatten_tree(tree_data)

    # Real secondary: from tree structure (parent-child in allCategoryData)
    # Flatten: for multi-level trees, only use the deepest level as children
    def get_leaf_nodes(node):
        """Recursively get leaf nodes (deepest level) from a tree node."""
        children = node.get("children", [])
        if not children:
            return [str(node.get("id", ""))]
        leaves = []
        for child in children:
            leaves.extend(get_leaf_nodes(child))
        return leaves

    real_children_of = {}  # parent_tid -> [deepest_child_tid]
    tree_child_ids = set()
    has_tree_children = set()  # categories that originally have children in tree (regardless of filtering)
    for nid, node in tree_id_to_node.items():
        children = node.get("children", [])
        if not children:
            continue
        has_tree_children.add(nid)  # mark as having tree children
        leaf_ids = get_leaf_nodes(node)
        valid_leaves = [lid for lid in leaf_ids if lid and lid in filtered and lid != nid]
        if valid_leaves:
            real_children_of[nid] = valid_leaves
            tree_child_ids.update(valid_leaves)
        for child in children:
            child_id = str(child.get("id", ""))
            if child_id:
                tree_child_ids.add(child_id)

    # Top-level: categories in filtered that are NOT tree children
    top_level = [(tid, info) for tid, info in filtered.items() if tid not in tree_child_ids]
    top_level.sort(key=lambda x: -x[1]["product_count"])

    # Determine target top count
    target_top = max(5, min(10, nav_width // 130))

    # Collect candidates (>= 500 products)
    candidates_with_children = [(tid, info) for tid, info in top_level
                               if tid in real_children_of and info["product_count"] >= 500]
    candidates_without_children = [(tid, info) for tid, info in top_level
                                  if tid not in real_children_of and info["product_count"] >= 500]
    candidates = candidates_with_children + candidates_without_children

    # Weighted random selection for all slots - no fixed categories
    # Higher product count = higher probability, but not guaranteed
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

    # Select top-level menu items
    used_ids = set()
    menu_top = []
    for tid, info in candidates:
        if len(menu_top) >= target_top:
            break
        if tid in used_ids:
            continue
        menu_top.append(tid)
        used_ids.add(tid)

    # Fake secondary: from ALL filtered categories (>= 16 products), not just candidates
    # This allows categories with 16-499 products to be fake secondaries
    # Also checks menu_top items - if they contain a shorter menu_top item's name,
    # they should be fake secondaries instead (e.g., "Architectural Hardware" under "Hardware")
    fake_children = {}
    assigned_as_child = set()

    # Build parent noun candidates from menu_top items (1-2 word names)
    parent_noun_map = {}  # normalized_name -> parent_tid
    for tid in menu_top:
        info = filtered.get(tid)
        if not info:
            continue
        words = extract_words(info["term_name"])
        if 1 <= len(words) <= 2:
            parent_noun_map[info["term_name"].lower().strip()] = tid

    # Match fake secondaries from ALL filtered categories (including menu_top items)
    # A menu_top item can become a fake child of another menu_top item
    for tid, info in filtered.items():
        if info["product_count"] < 16:
            continue
        if tid in has_tree_children:
            continue  # has real tree children, never be a fake secondary
        if tid in tree_child_ids:
            continue  # already a real secondary under another parent, skip
        child_name = info["term_name"].lower().strip()
        child_len = len(child_name)
        for parent_name, parent_tid in parent_noun_map.items():
            if parent_tid == tid:
                continue  # can't be child of itself
            parent_len = len(parent_name)
            if parent_len >= child_len:
                continue
            if parent_name not in child_name:
                continue
            # Whole word check
            import re as _re
            if not _re.search(r'\b' + _re.escape(parent_name) + r'\b', child_name):
                continue
            # Parent must not have real children
            if parent_tid in real_children_of:
                continue
            fake_children.setdefault(parent_tid, []).append(tid)
            assigned_as_child.add(tid)
            break

    # Remove items from menu_top that were assigned as fake children
    # (they might have been added to menu_top before fake secondary matching)
    menu_top = [tid for tid in menu_top if tid not in assigned_as_child]

    # Restore: if we lost top-level items due to fake secondary assignment, add back from candidates
    if len(menu_top) < target_top:
        current_top_set = set(menu_top)
        for tid, info in candidates:
            if len(menu_top) >= target_top:
                break
            if tid in current_top_set:
                continue  # already in menu_top
            if tid in assigned_as_child:
                continue  # matched as fake secondary, skip
            menu_top.append(tid)
            current_top_set.add(tid)

    # Build final menuList
    menu_list = []

    for tid in menu_top:
        info = filtered.get(tid)
        if not info:
            continue
        entry = {"id": tid, "title": info["term_name"], "children": []}

        # Real children (from tree structure)
        for child_tid in real_children_of.get(tid, []):
            cinfo = filtered.get(child_tid)
            if cinfo:
                entry["children"].append({"id": child_tid, "title": cinfo["term_name"], "children": []})

        # Fake children (from strict matching)
        for child_tid in fake_children.get(tid, []):
            cinfo = filtered.get(child_tid)
            if cinfo:
                if not any(c["id"] == child_tid for c in entry["children"]):
                    entry["children"].append({"id": child_tid, "title": cinfo["term_name"], "children": []})

        menu_list.append(entry)

    # Print summary
    real_count = sum(1 for e in menu_list if e["children"])
    total_children = sum(len(e["children"]) for e in menu_list)
    print(f"  Menu: {len(menu_list)} top-level, {real_count} with children, {total_children} total children")

    return menu_list


def apply_main_cat(menu_list, main_cat_name, all_cats):
    """Place main_cat (and its parent) right after the first position.
    Returns modified menuList."""
    if not main_cat_name:
        return menu_list

    # Find main_cat in all_cats
    main_info = None
    for cat in all_cats:
        if cat.get("term_name", "").lower() == main_cat_name.lower():
            main_info = cat
            break
    if not main_info:
        # Fuzzy match
        for cat in all_cats:
            if norm_name(cat.get("term_name", "")) == norm_name(main_cat_name):
                main_info = cat
                break
    if not main_info:
        print(f"  [WARN] Main category '{main_cat_name}' not found in categories")
        return menu_list

    main_tid = str(main_info.get("term_id", ""))
    main_name = main_info.get("term_name", main_cat_name)
    parent_id = str(main_info.get("parent_id", "0"))

    # Find parent info
    parent_info = None
    if parent_id and parent_id != "0":
        for cat in all_cats:
            if str(cat.get("term_id", "")) == parent_id:
                parent_info = cat
                break

    # Remove main_cat and its parent from current positions
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

    # Insert after Home (index 0)
    insert_idx = 1

    if parent_info:
        parent_tid = str(parent_info.get("term_id", ""))
        parent_name = parent_info.get("term_name", "")
        # Insert parent as top-level, main_cat as its child
        parent_entry = {"id": parent_tid, "title": parent_name, "children": [
            {"id": main_tid, "title": main_name, "children": []}
        ]}
        menu_list.insert(insert_idx, parent_entry)
        print(f"  Main cat: {main_name} (under parent: {parent_name})")
    else:
        # Insert as top-level
        main_entry = {"id": main_tid, "title": main_name, "children": []}
        menu_list.insert(insert_idx, main_entry)
        print(f"  Main cat: {main_name} (top-level)")

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
        return {"error": True, "msg": "No response from updateCategory.php"}
    try:
        result = safe_json(resp)
        if result.get("code") == 0 or result.get("error") == False:
            return {"error": False, "msg": result.get("msg", "Menu updated successfully")}
        return {"error": True, "msg": result.get("msg", f"HTTP {resp.status_code}")}
    except Exception:
        if resp.status_code == 200:
            return {"error": False, "msg": "Menu updated (status 200)"}
        return {"error": True, "msg": f"HTTP {resp.status_code}: {resp.text[:200]}"}

def get_rest_api_nonce(session, site_url):
    r = session.get(f"{site_url}/wp-admin/", headers={"User-Agent": "Mozilla/5.0"})
    m = re.search(r'wpApiSettings[^}]+nonce["\': ]+([a-f0-9]+)', r.text, re.I)
    if m:
        return m.group(1)
    raise RuntimeError("Cannot find REST API nonce")

def detect_nav_width(session, site_url):
    r = session.get(site_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if r.status_code != 200:
        return 1140
    html = r.text
    # Search for nav container selectors in inline CSS
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
        m = re.search(pat, html, re.I)
        if m:
            w = int(m.group(1))
            if 600 <= w <= 2000:
                return w
    return 1140

def get_term_link(session, site_url, term_id, nonce=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    if nonce:
        headers["X-WP-Nonce"] = nonce
    for base in [f"{site_url}/wp-json/wp/v2/product_cat/{term_id}",
                 f"{site_url}/?rest_route=/wp/v2/product_cat/{term_id}"]:
        try:
            r = session.get(base, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.json().get("link", "")
        except Exception:
            continue
    return ""

def process_one_site(site_name, folder=None, auto_yes=False, dry_run=False, main_cat=None):
    site_url = derive_site_url(site_name)
    print(f"{'='*60}\nSite: {site_name}\nURL:  {site_url}")

    session = requests.Session()
    session.verify = False

    print("\n[Login]")
    wp_login(session, site_url)

    print("\n[Fetch categories from categorySearch.php]")
    all_cats = fetch_all_categories(session, site_url)
    if not all_cats:
        raise RuntimeError("No categories fetched from main_category.php")

    print("\n[Fetch category tree from edit_category.php]")
    tree_data = fetch_category_tree(session, site_url)

    print("\n[Build Menu List]")
    nav_width = detect_nav_width(session, site_url)
    print(f"  Nav width: {nav_width}px")
    menu_list = build_menu_list(all_cats, tree_data, site_name=site_name, main_cat=main_cat, nav_width=nav_width)

    if main_cat and main_cat.lower() not in ("home", "shop"):
        print(f"\n[Apply main category: {main_cat}]")
        menu_list = apply_main_cat(menu_list, main_cat, all_cats)

    print(f"\n  Final menuList ({len(menu_list)} top-level):")
    for entry in menu_list:
        child_count = len(entry.get("children", []))
        print(f"    {entry['title']} (id={entry['id']}, children={child_count})")

    if dry_run:
        print("\n  [dry-run] Skipping push")
        return menu_list

    if not auto_yes:
        try:
            yn = input(f"\nSend menuList to WordPress? (y/n): ").strip().lower() or 'y'
        except (EOFError, OSError):
            yn = 'y'
        if yn != 'y':
            print("Skipped")
            return menu_list

    print("\n[POST menuList to updateCategory.php]")
    result = post_menu_list(session, site_url, menu_list)
    if result.get("error"):
        print(f"  [X] {result.get('msg')}")
    else:
        print(f"  [OK] {result.get('msg')}")

    return menu_list


def rocket_setting(site_name, log_func=print):
    site_url = derive_site_url(site_name)
    log_func(f"  URL: {site_url}")

    session = requests.Session()
    session.verify = False

    log_func("  [登录]")
    try:
        wp_login(session, site_url)
    except RuntimeError as e:
        log_func(f"  ❌ 登录失败: {e}")
        return {"error": True, "msg": f"登录失败: {e}"}

    log_func("  [激活 WP Rocket]")
    r = session.get(f"{site_url}/wp-admin/plugins.php")
    if r.status_code not in (200, 302):
        log_func("  ⚠️ 无法访问插件页面")
        return {"error": True, "msg": "无法访问插件页面"}

    # 按 data-slug 或者 href 中包含 wp-rocket 和 activate 来匹配
    activated = "wp-rocket/wp-rocket.php" in r.text and " Deactivate" in r.text
    if not activated:
        m = re.search(r'<a[^>]*href="([^"]*action=activate[^"]*wp-rocket[^"]*)"', r.text)
        if m:
            activate_url = m.group(1).replace('&amp;', '&')
            if not activate_url.startswith("http"):
                activate_url = f"{site_url}/wp-admin/{activate_url}"
            session.post(activate_url)
            log_func("  ✅ WP Rocket 已激活")
            time.sleep(1)
        else:
            log_func("  ℹ️ WP Rocket 可能已激活，继续配置")
    else:
        log_func("  ℹ️ WP Rocket 已激活")

    log_func("  [配置 WP Rocket 设置]")
    for attempt in range(3):
        st_r = session.get(f"{site_url}/wp-admin/options-general.php?page=wprocket")
        if st_r.status_code == 200:
            break
        st_r = session.get(f"{site_url}/wp-admin/admin.php?page=wprocket")
        if st_r.status_code == 200:
            break
        if attempt < 2:
            time.sleep(2)

    fields = {
        "_wpnonce": r'id="_wpnonce"[^>]*value="([^"]+)"',
        "secret_key": r'id="secret_key"[^>]*value="([^"]*)"',
        "minify_js_key": r'id="minify_js_key"[^>]*value="([^"]*)"',
        "consumer_email": r'id="consumer_email"[^>]*value="([^"]*)"',
        "consumer_key": r'id="consumer_key"[^>]*value="([^"]*)"',
        "version": r'id="version"[^>]*value="([^"]*)"',
        "minify_css_key": r'id="minify_css_key"[^>]*value="([^"]*)"',
    }
    extracted = {}
    for name, pat in fields.items():
        mm = re.search(pat, st_r.text)
        if mm:
            extracted[name] = mm.group(1)

    if "_wpnonce" not in extracted:
        log_func("  ⚠️ 无法获取设置页面信息 (status=" + str(st_r.status_code) + ", url=" + st_r.url + ")")
        log_func("  ✅ WP Rocket 已激活，设置需在后台手动配置")
        return {"error": False, "msg": "WP Rocket 已激活"}

    setting_data = {
        "option_page": "wprocket",
        "action": "update",
        "_wpnonce": extracted["_wpnonce"],
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
        "wp_rocket_settings[consumer_key]": extracted.get("consumer_key", ""),
        "wp_rocket_settings[consumer_email]": extracted.get("consumer_email", ""),
        "wp_rocket_settings[secret_key]": extracted.get("secret_key", ""),
        "wp_rocket_settings[license]": "",
        "wp_rocket_settings[secret_cache_key]": "",
        "wp_rocket_settings[minify_css_key]": extracted.get("minify_css_key", ""),
        "wp_rocket_settings[minify_js_key]": extracted.get("minify_js_key", ""),
        "wp_rocket_settings[version]": extracted.get("version", ""),
        "wp_rocket_settings[cloudflare_old_settings]": "",
        "wp_rocket_settings[cache_ssl]": "1",
        "wp_rocket_settings[minify_google_fonts]": "0",
        "wp_rocket_settings[emoji]": "0",
        "wp_rocket_settings[remove_unused_css]": "1",
        "wp_rocket_settings[async_css]": "0",
        "wp_rocket_settings[async_css_mobile]": "",
    }

    pr = session.post(f"{site_url}/wp-admin/options.php", data=setting_data)
    if pr.status_code == 200:
        log_func("  ✅ WP Rocket 设置成功")
        return {"error": False, "msg": "设置成功"}
    log_func(f"  ❌ 设置失败: {pr.status_code}")
    return {"error": True, "msg": f"设置失败: {pr.status_code}"}


def clear_rocket_cache(site_name, log_func=print):
    site_url = derive_site_url(site_name)
    log_func(f"  URL: {site_url}")

    session = requests.Session()
    session.verify = False

    log_func("  [登录]")
    try:
        wp_login(session, site_url)
    except RuntimeError as e:
        log_func(f"  ❌ 登录失败: {e}")
        return {"error": True, "msg": f"登录失败: {e}"}

    log_func("  [清理 WP Rocket 缓存]")
    r = session.get(f"{site_url}/wp-admin/")
    if r.status_code != 200:
        log_func("  ⚠️ 无法访问后台")
        return {"error": True, "msg": "无法访问后台"}

    idx = r.text.find('purge_cache')
    if idx < 0:
        log_func("  ⚠️ 未找到清理缓存按钮")
        return {"error": True, "msg": "未找到清理按钮"}
    start = r.text.rindex('href="', 0, idx) + 6
    end = r.text.index('"', start)
    purge_url = r.text[start:end]
    if not purge_url.startswith("http"):
        purge_url = f"{site_url}/wp-admin/{purge_url}"
    purge_url = purge_url.replace('&amp;', '&')
    pr = session.get(purge_url)
    if pr.status_code in (200, 302):
        log_func("  ✅ 缓存已清理")
        return {"error": False, "msg": "缓存已清理"}
    log_func(f"  ❌ 清理失败: {pr.status_code}")
    return {"error": True, "msg": f"清理失败: {pr.status_code}"}


def set_main_category(site_name, folder, category_name, log_func=print):
    """Set main category on a WordPress site via categorySearch & mainCategorySet."""
    site_url = derive_site_url(site_name)
    log_func(f"Site: {site_name} | 分类: {category_name}")
    log_func(f"  URL: {site_url}")

    if category_name.strip().lower() == "none":
        return {"error": False, "msg": "已跳过", "skipped": True}

    session = requests.Session()
    session.verify = False

    log_func("  [登录]")
    try:
        wp_login(session, site_url)
    except RuntimeError as e:
        return {"error": True, "msg": f"登录失败: {e}"}

    search_url = f"{site_url}/cf-updata/category/categorySearch.php"
    set_url = f"{site_url}/cf-updata/category/mainCategorySet.php"

    # Search for the category (paginate if needed) — collect all exact matches (duplicate-aware)
    found_items = []
    already_main = False
    for page in range(1, 11):
        log_func(f"  搜索第 {page} 页...")
        resp = request_with_retry(session, "POST", search_url,
                                   data={"page": str(page), "limit": "25", "category_name": category_name},
                                   headers={"User-Agent": "Mozilla/5.0"}, retries=2)
        if resp is None or resp.status_code != 200:
            log_func(f"  [WARN] 搜索请求失败")
            break
        try:
            data = safe_json(resp)
        except:
            log_func(f"  [WARN] 响应不是 JSON: {resp.text[:200]}")
            break
        if data.get("code") != 0:
            log_func(f"  [WARN] 搜索返回 code={data.get('code')}")
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
        log_func(f"  -> {category_name} 已是主分类")
        return {"error": False, "msg": f"{category_name} 已是主分类"}

    if len(found_items) == 0:
        log_func(f"  [X] 未找到匹配的分类: {category_name}")
        return {"error": True, "msg": f"未找到分类: {category_name}"}

    if len(found_items) > 1:
        log_func(f"  [⚠] 同名分类 {len(found_items)} 个，需手动选择")
        return {"error": False, "duplicates": True, "results": found_items, "msg": f"同名分类 {len(found_items)} 个"}

    term_id = found_items[0]["term_id"]
    log_func(f"  [OK] 找到: {found_items[0]['term_name']} (term_id={term_id})")

    # Set as main category
    log_func(f"  设置中...")
    resp = request_with_retry(session, "POST", set_url,
                               data={"term_id": term_id},
                               headers={"User-Agent": "Mozilla/5.0"}, retries=2)
    if resp is None:
        return {"error": True, "msg": "设置请求无响应"}

    try:
        result = safe_json(resp)
    except:
        return {"error": True, "msg": f"响应不是 JSON: {resp.text[:200]}"}

    if result.get("error"):
        msg = result.get("msg", ["设置失败"])
        log_func(f"  [X] {msg}")
        return {"error": True, "msg": str(msg)}
    else:
        msg = result.get("msg", ["设置成功"])
        log_func(f"  [OK] {msg}")
        # Fetch and return the category link
        nonce = get_rest_api_nonce(session, site_url)
        link = get_term_link(session, site_url, term_id, nonce=nonce) if term_id else ""
        return {"error": False, "msg": str(msg), "link": link}


def main():
    parser = argparse.ArgumentParser(description="Batch WordPress menu builder")
    parser.add_argument("--site", nargs="+", help="Site domain(s) to process (space-separated)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, skip push")
    parser.add_argument("--list", action="store_true", help="List available sites and exit")
    args = parser.parse_args()

    SITE_LIST = [
        "www.abrasivekit.com",
        "www.officesupplyglobal.com",
    ]

    if args.list:
        print(f"Available sites ({len(SITE_LIST)}):")
        for name in SITE_LIST:
            print(f"  {name}")
        return

    auto_yes = True
    if args.site:
        selected = [s for s in args.site if s in SITE_LIST]
        not_found = [s for s in args.site if s not in SITE_LIST]
        if not_found:
            print(f"Unknown sites: {not_found}")
        if not selected:
            return
    else:
        selected = SITE_LIST

    print(f"Processing {len(selected)} site(s)...")
    for site_name in selected:
        print(f"\n{'='*60}")
        print(f"Site: {site_name}")
        try:
            process_one_site(site_name, auto_yes=auto_yes, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ERROR on {site_name}: {e}")
            continue

    print(f"\n{'='*60}\nBatch done. {len(selected)} site(s) processed.")

if __name__ == "__main__":
    main()
