import os
import re
import time
import json
import html
import requests
from collections import Counter

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXCLUDED = {
    "best seller", "featured", "accessories", "other", "new arrival",
    "exclusive", "limited edition", "hot sale", "most popular",
    "trending", "special offer", "flash sale", "ACCESSORIES",
    "book", "books", "novel", "novels", "fiction", "movie", "movies",
    "music", "video", "videos", "dvd", "cd", "art", "poster", "posters",
    "gift card", "gift cards", "gift certificate", "magazine", "magazines",
}
BROAD_KEYWORDS = {"gear", "accessories", "parts", "components", "equipment", "supplies", "tools", "sets", "kits"}
CATCHALL_KEYWORDS = {"gear", "accessories", "equipment", "supplies"}
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "are", "not", "but", "all", "can", "has", "its", "was"}


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


def normalize_word(w):
    if len(w) <= 3:
        return w
    if w.endswith('ies'):
        return w[:-3] + 'y'
    if w.endswith('ves'):
        return w[:-3] + 'f'
    if w.endswith('s') and not w.endswith('ss'):
        base = w[:-1]
        if len(base) > 2:
            return base
    return w


def extract_words(name):
    words = re.findall(r'[a-z]+', name.strip().lower())
    return [normalize_word(w) for w in words if len(w) > 2 and w not in STOP_WORDS]


def clean_slug(name):
    slug = name.lower().replace(" ", "-").replace("/", "-").replace("&", "and")
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug


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


def fetch_site_categories_with_counts(session, site_url):
    """Fetch WP product categories with product counts and term IDs from edit-tags.php.
    Returns: (Counter, dict) — Counter maps name->count, dict maps name->term_id
    """
    counter = Counter()
    term_ids = {}
    for page in range(1, 100):
        url = f"{site_url}/wp-admin/edit-tags.php?taxonomy=product_cat&post_type=product&paged={page}"
        r = session.get(url)
        if r is None or r.status_code != 200 or "edit-tags" not in r.url:
            break
        tr_pattern = r'<tr[^>]*?id="tag-(\d+)"[^>]*>(.*?)</tr>'
        trs = re.findall(tr_pattern, r.text, re.S)
        if not trs:
            break
        for tid, tr_content in trs:
            name_m = re.search(r'<a[^>]*class="row-title"[^>]*>([^<]+)</a>', tr_content)
            if not name_m:
                continue
            name = html.unescape(name_m.group(1)).strip()
            count_m = re.search(r'class="posts[^"]*column-count[^"]*"[^>]*>.*?(\d+).*?</td>', tr_content, re.S)
            count = int(count_m.group(1)) if count_m else 0
            if name.lower() not in EXCLUDED:
                counter[name] = counter.get(name, 0) + count
                term_ids[name] = int(tid)
        if len(trs) < 20:
            break
    return counter, term_ids


def build_menu_structure(counter, target_top=8):
    target_top = max(5, min(10, target_top))
    hier_parent_of = {}
    counts = {}
    for cat, cnt in counter.most_common():
        if "|||" in cat:
            parts = [p.strip() for p in cat.split("|||") if p.strip()]
            for i, p in enumerate(parts):
                if p not in hier_parent_of:
                    hier_parent_of[p] = parts[i - 1] if i > 0 else None
                counts[p] = counts.get(p, 0) + cnt
        else:
            counts[cat] = counts.get(cat, 0) + cnt
    hier_parents = {p for c, p in hier_parent_of.items() if p}
    hier_children = {c for c, p in hier_parent_of.items() if p}
    pure_parents = hier_parents - hier_children
    all_items = sorted(counts.items(), key=lambda x: -x[1])
    flat = [(n, c) for n, c in all_items if n not in hier_parents and n not in hier_children]
    real_parents = [(n, c) for n, c in all_items if n in pure_parents]
    real_children = [(n, c, hier_parent_of[n]) for n, c in all_items if n in hier_children]
    word_candidates = flat + real_parents

    def is_valid_child(parent, child):
        p_words = extract_words(parent)
        c_words = extract_words(child)
        if any(kw in parent.lower() for kw in CATCHALL_KEYWORDS):
            return len(set(p_words) & set(c_words)) >= 1
        if len(p_words) >= 2:
            prefix = ' '.join(p_words[:2])
            if prefix in child.lower():
                return True
        if len(p_words) == 1:
            pw = p_words[0]
            if not child.lower().strip().endswith(pw):
                return False
            cw = extract_words(child)
            if cw and cw[0] != pw:
                first_word_cats = [cn for cn, cc in counts.items() if cn.lower() == cw[0] and cc >= 50]
                if first_word_cats:
                    return False
            return True
        return False

    word_groups = {}
    for name, cnt in word_candidates:
        for w in extract_words(name):
            if name.lower().startswith(w):
                word_groups.setdefault(w, []).append((name, cnt))
    used = set()
    hierarchies = []
    for w, members in sorted(word_groups.items(), key=lambda x: -sum(m[1] for m in x[1])):
        avail = sorted([(n, c) for n, c in members if n not in used], key=lambda x: -x[1])
        if len(avail) >= 3:
            broad = [(n, c) for n, c in avail if any(kw in n.lower() for kw in BROAD_KEYWORDS)]
            parent = broad[0][0] if broad else avail[0][0]
            children = [(n, c) for n, c in avail if n != parent and is_valid_child(parent, n)]
            if children:
                hierarchies.append((parent, children))
                used.add(parent)
                for n, c in children:
                    used.add(n)

    word_to_cats = {}
    for name, cnt in word_candidates:
        if name in used:
            continue
        for w in extract_words(name):
            if len(w) > 2 and w not in STOP_WORDS:
                word_to_cats.setdefault(w, []).append((name, cnt))
    for w in sorted(word_to_cats, key=lambda x: -sum(c for n, c in word_to_cats[x])):
        avail = [(n, c) for n, c in word_to_cats[w] if n not in used]
        if len(avail) < 3:
            continue
        avail_dict = dict(avail)
        word_parents = [n for n, c in avail if n.lower() == w or n.lower().startswith(w)]
        if not word_parents:
            continue
        parent = max(word_parents, key=lambda n: avail_dict.get(n, 0))
        children = [(n, c) for n, c in avail if n != parent and (w in extract_words(n) or w in n.lower())]
        children = [(n, c) for n, c in children if is_valid_child(parent, n) and n not in used]
        if children:
            hierarchies.append((parent, children))
            used.add(parent)
            for n, c in children:
                used.add(n)

    hier_parent_set = {p for p, c in hierarchies}
    boosted = {}
    for p, children in hierarchies:
        total = counts.get(p, 0)
        for n, c in children:
            total += c
        boosted[p] = total
    candidates = {}
    for p, c in hierarchies:
        candidates[p] = boosted.get(p, counts.get(p, 0))
    for n, c in flat:
        if n not in used and n not in hier_parent_set and n not in candidates:
            candidates[n] = c
    for n, c in real_parents:
        if n not in used and n not in hier_parent_set and n not in candidates:
            candidates[n] = c
    cand_sorted = sorted(candidates.items(), key=lambda x: -x[1])

    all_category_words = set()
    for name in counts:
        for w in extract_words(name):
            all_category_words.add(w)
    cand_sorted = [(n, c) for n, c in cand_sorted
                   if any(w in all_category_words for w in extract_words(n))]

    top_names, menu = set(), []
    menu.append(("Home", None, False))
    top_names.add("Home")
    slots = target_top - 1
    for name, cnt in cand_sorted:
        if len(menu) - 1 >= slots:
            break
        if name not in top_names:
            top_names.add(name)
            menu.append((name, None, False))
    menu.append(("Shop", None, False))
    top_names.add("Shop")
    for parent, children in hierarchies:
        if parent in top_names:
            for n, c in children:
                if n not in top_names:
                    menu.append((n, parent, False))
    for parent, children in hierarchies:
        if parent not in top_names:
            if len([x for x in menu if x[1] is None]) >= target_top:
                break
            menu.append((parent, None, False))
            top_names.add(parent)
            for n, c in children:
                if n not in top_names and n not in [i[0] for i in menu]:
                    menu.append((n, parent, False))
                    break
    for name, cnt, parent in sorted(real_children, key=lambda x: -x[1]):
        if parent in top_names and name not in top_names:
            menu.append((name, parent, False))
    for name, cnt in sorted(real_parents, key=lambda x: -x[1]):
        if name in top_names:
            continue
        if len([x for x in menu if x[1] is None]) >= target_top:
            break
        children = [(n, c) for n, c, p in real_children if p == name]
        if children:
            menu.append((name, None, False))
            for n, c in sorted(children, key=lambda x: -x[1]):
                if n not in [i[0] for i in menu]:
                    menu.append((n, name, False))
                    break
    seen = set()
    deduped = []
    for item in menu:
        if item[0] not in seen:
            seen.add(item[0])
            deduped.append(item)
    return deduped


def get_rest_api_nonce(session, site_url):
    r = session.get(f"{site_url}/wp-admin/", headers={"User-Agent": "Mozilla/5.0"})
    m = re.search(r'wpApiSettings[^}]+nonce["\': ]+([a-f0-9]+)', r.text, re.I)
    if m:
        return m.group(1)
    raise RuntimeError("Cannot find REST API nonce")


def clear_menu_items_via_admin(session, site_url, menu_id):
    nav_url = f"{site_url}/wp-admin/nav-menus.php?action=edit&menu={menu_id}"
    r = session.get(nav_url)
    if r.status_code != 200:
        return
    ids = list(re.finditer(r'menu-item-(\d+)', r.text))
    if not ids:
        return
    deleted = 0
    for m in ids:
        iid = m.group(1)
        pat = rf'href="([^"]*action=delete-menu-item[^"]*menu-item={iid}[^"]*)"'
        dm = re.search(pat, r.text)
        if dm:
            delete_url = dm.group(1).replace('&amp;', '&')
            r2 = session.get(delete_url)
            if r2.status_code in (200, 302):
                deleted += 1


def add_rest_menu_item(session, site_url, api_nonce, menu_id, title, obj_type, obj_id, parent_id=0):
    headers = {"User-Agent": "Mozilla/5.0", "X-WP-Nonce": api_nonce,
               "Content-Type": "application/json"}
    base = f"{site_url}/wp-json/wp/v2"
    slug = clean_slug(title)
    body = {"title": title, "type": obj_type, "menus": str(menu_id), "parent": parent_id}
    if obj_type == "taxonomy":
        body["object"] = "product_cat"
        body["object_id"] = str(obj_id)
    elif obj_type == "custom":
        body["object"] = "custom"
        body["object_id"] = 0
        if title.lower() == "home":
            body["url"] = site_url + "/"
        else:
            body["url"] = f"{site_url}/product-category/{slug}/"
    resp = request_with_retry(session, "POST", f"{base}/menu-items", json=body, headers=headers, retries=3)
    if resp is not None and resp.status_code in (200, 201):
        data = resp.json()
        return data["id"]
    return None


class WpMenuConfigurator:
    """Configure WordPress navigation menu using smart category grouping.
    Fetches categories from the site (not Excel), builds menu structure via
    word-grouping algorithm, and uploads via REST API.
    """

    def __init__(self, password):
        self._password = password
        self._session = requests.Session()
        self._session.verify = False

    def configure(self, domain):
        site_url = derive_site_url(domain)

        # 1. Login
        wp_login(self._session, site_url, self._password)

        # 2. Fetch categories from the site (replaces Excel reading)
        counter, wp_term_ids = fetch_site_categories_with_counts(self._session, site_url)

        # 3. Build menu using smart algorithm
        menu = build_menu_structure(counter, target_top=8)

        # 4. Get REST API nonce
        api_nonce = get_rest_api_nonce(self._session, site_url)

        # 5. Find/create menu via REST API
        menu_id = self._find_or_create_menu(site_url, api_nonce)

        # 6. Clear existing menu items
        clear_menu_items_via_admin(self._session, site_url, menu_id)

        # 7. Add menu items via REST API (two passes)
        self._add_all_menu_items(site_url, api_nonce, menu_id, menu, wp_term_ids)

    def _find_or_create_menu(self, site_url, api_nonce):
        headers = {"User-Agent": "Mozilla/5.0", "X-WP-Nonce": api_nonce}
        base = f"{site_url}/wp-json/wp/v2"
        r_menus = self._session.get(f"{base}/menus", headers=headers)
        if r_menus.status_code == 200:
            menus = r_menus.json()
            for menu_obj in menus:
                if "primary" in menu_obj["name"].lower():
                    return menu_obj["id"]
        r_new = self._session.post(f"{base}/menus", json={"name": "primary-menu"},
                                   headers=headers)
        if r_new.status_code in (200, 201):
            return r_new.json()["id"]
        raise RuntimeError(f"Cannot create menu: HTTP {r_new.status_code}")

    def _add_all_menu_items(self, site_url, api_nonce, menu_id, menu, wp_term_ids):
        created_items = {}
        term_ids = {}
        for name, parent, _ in menu:
            if name in wp_term_ids:
                term_ids[name] = wp_term_ids[name]
            else:
                for k, v in wp_term_ids.items():
                    if k.lower() == name.lower():
                        term_ids[name] = v
                        break

        def is_shop(name):
            return name.lower() == "shop"

        for name, parent_name, _ in menu:
            if parent_name:
                continue
            tid = term_ids.get(name)
            if tid is not None and not is_shop(name):
                mid = add_rest_menu_item(self._session, site_url, api_nonce,
                                         menu_id, name, "taxonomy", tid, 0)
            else:
                mid = add_rest_menu_item(self._session, site_url, api_nonce,
                                         menu_id, name, "custom", 0, 0)
            if mid:
                created_items[name] = mid
            time.sleep(0.3)

        for name, parent_name, _ in menu:
            if not parent_name:
                continue
            pid = created_items.get(parent_name, 0)
            tid = term_ids.get(name)
            if tid is not None and not is_shop(name):
                mid = add_rest_menu_item(self._session, site_url, api_nonce,
                                         menu_id, name, "taxonomy", tid, pid)
            else:
                mid = add_rest_menu_item(self._session, site_url, api_nonce,
                                         menu_id, name, "custom", 0, pid)
            if mid:
                created_items[name] = mid
            time.sleep(0.3)
