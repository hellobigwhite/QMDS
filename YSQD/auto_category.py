import re
import time
import urllib3

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup


REMOTE_CATEGORY_API = "https://www.bashwheels.com/cf-updata/category/categorySearch.php"
MAIN_CATEGORY_SET_API = "https://www.bashwheels.com/cf-updata/category/mainCategorySet.php"

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


class AutoCategoryConfigurator:
    def __init__(self, password):
        self._password = password
        self._session = requests.Session()
        self._session.headers.update(WP_ADMIN_HEADERS)

    def _request(self, method, url, **kwargs):
        for _ in range(3):
            try:
                start = time.time()
                resp = self._session.request(method, url, timeout=20, verify=False, **kwargs)
                elapsed = time.time() - start
                if resp is not None and resp.status_code in (200, 201, 302):
                    return resp
            except requests.exceptions.RequestException:
                pass
        return None

    def _login(self, domain):
        login_url = f"https://www.{domain}/bbwllogin/"
        name = domain.replace(".com", "").strip()
        data = {
            "log": f"Ad{name}min",
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"https://www.{domain}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data, allow_redirects=True)
        if resp is None:
            raise RuntimeError("WP login failed")
        if any("wordpress_logged_in" in c.name for c in self._session.cookies):
            return True
        admin_check = self._request("GET", f"https://www.{domain}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            return True
        raise RuntimeError("WP login failed")

    def _fetch_all_remote_categories(self, limit=25):
        all_rows = []
        page = 1
        while True:
            resp = self._request(
                "POST",
                REMOTE_CATEGORY_API,
                headers=HEADERS,
                data={"page": page, "limit": limit},
            )
            if not resp:
                raise RuntimeError("获取远程分类列表失败")
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
        unique = {}
        for row in all_rows:
            unique[row["term_id"]] = row
        return list(unique.values())

    def _normalize_category_path(self, text):
        if not text:
            return []
        cleaned = text.strip()
        if "主打分类为：" in cleaned:
            cleaned = cleaned.split("主打分类为：", 1)[1].strip()
        cleaned = cleaned.strip(" ,，")
        parts = [part.strip() for part in cleaned.split("|||")]
        return [part for part in parts if part]

    def _resolve_term_id(self, rows, main_category):
        parts = self._normalize_category_path(main_category)
        if not parts:
            raise RuntimeError("主打类目为空")

        def norm(value):
            return (value or "").strip().lower()

        rows_by_parent = {}
        for row in rows:
            parent_id = str(row.get("parent_id", "0"))
            rows_by_parent.setdefault(parent_id, []).append(row)

        if len(parts) == 1:
            name = norm(parts[0])
            matches = [row for row in rows if norm(row.get("term_name")) == name]
            if len(matches) == 1:
                return matches[0]["term_id"]
            if len(matches) > 1:
                raise RuntimeError("主打类目重名，请填写完整路径")
            raise RuntimeError("主打类目未匹配到分类ID")

        parent_id = "0"
        current = None
        for name in parts:
            name_key = norm(name)
            candidates = [
                row
                for row in rows_by_parent.get(parent_id, [])
                if norm(row.get("term_name")) == name_key
            ]
            if not candidates:
                raise RuntimeError(f"主打类目路径未匹配到分类：{name}")
            if len(candidates) > 1:
                raise RuntimeError(f"主打类目路径存在重名：{name}")
            current = candidates[0]
            parent_id = str(current["term_id"])
        if not current:
            raise RuntimeError("主打类目未匹配到分类ID")
        return current["term_id"]

    def set_main_category(self, domain, main_category):
        """设置主分类（完整的菜单配置）"""
        self._login(domain)
        
        # 步骤1: 获取菜单配置参数
        params = self._get_menu_params(domain)
        if not params:
            raise RuntimeError("获取菜单参数失败")
        
        # 步骤2: 激活分类列表
        activation_success = self._activate_category_list(domain, params["closedpostboxesnonce"])
        if not activation_success:
            raise RuntimeError("激活分类列表失败")
        
        # 步骤3: 获取远程分类数据
        rows = self._fetch_all_remote_categories()
        hierarchy = self._build_category_hierarchy(rows)
        
        # 步骤4: 获取站点现有分类
        categories = self._fetch_site_categories(domain)
        if not categories:
            raise RuntimeError("获取站点分类失败")
        
        # 步骤5: 获取站点页面
        pages = self._fetch_site_pages(domain)
        
        # 步骤6: 构建添加分类的表单数据
        add_form = self._build_add_menu_items_form(hierarchy, categories, pages, set())
        if not add_form:
            return True
        
        # 步骤7: 添加菜单项
        add_data = {
            "action": "add-menu-item",
            "menu": params["menu"],
            "menu-settings-column-nonce": params["menu_settings_column_nonce"],
        }
        add_data.update(add_form)
        
        ajax_url = f"https://www.{domain}/wp-admin/admin-ajax.php"
        add_response = self._request("POST", ajax_url, data=add_data)
        if not add_response:
            raise RuntimeError("添加菜单项失败")
        
        # 步骤8: 解析新菜单项
        new_inputs = self._parse_menu_item_inputs_from_html(add_response.text)
        if not new_inputs:
            raise RuntimeError("解析新菜单项失败")
        
        # 步骤9: 构建父级更新
        title_to_db_id = {}
        title_to_db_id.update(self._extract_title_map_from_inputs({}))
        title_to_db_id.update(self._extract_title_map_from_inputs(new_inputs))
        parent_updates = self._build_parent_updates(hierarchy, title_to_db_id)
        
        # 步骤10: 保存菜单
        self._save_menu(domain, params, {}, new_inputs, parent_updates)
        return True
    
    def _get_menu_params(self, domain):
        """获取菜单配置参数"""
        menus_resp = self._request("GET", f"https://www.{domain}/wp-admin/nav-menus.php")
        if not menus_resp or menus_resp.status_code != 200:
            raise RuntimeError("获取菜单页面失败")
        
        html = menus_resp.text
        soup = BeautifulSoup(html, 'html.parser')
        
        # 获取各种nonce值
        def _input_value(elem_id=None, name=None):
            if elem_id:
                tag = soup.find('input', {'id': elem_id})
                if tag and tag.get('value'):
                    return tag.get('value')
            if name:
                tag = soup.find('input', {'name': name})
                if tag and tag.get('value'):
                    return tag.get('value')
            return None
        
        closedpostboxesnonce = _input_value(elem_id='closedpostboxesnonce')
        meta_box_order_nonce = _input_value(elem_id='meta-box-order-nonce')
        update_nav_menu_nonce = _input_value(elem_id='update-nav-menu-nonce')
        menu = _input_value(elem_id='nav-menu-meta-object-id', name='menu')
        menu_settings_column_nonce = _input_value(elem_id='menu-settings-column-nonce')
        referer = _input_value(name='_wp_http_referer')
        menu_name = _input_value(elem_id='menu-name')
        
        # 获取Home菜单项
        home_inputs = soup.find('input', attrs={'class': 'widefat edit-menu-item-title', 'value': 'Home'})
        if not home_inputs:
            raise RuntimeError("未找到菜单中设置的Home")
        home_id = home_inputs.get('id')
        id_match = re.search(r'edit-menu-item-title-(\d+)', home_id)
        if not id_match:
            raise RuntimeError("无法从Home输入框ID中解析出db_id")
        home_db = id_match.group(1)
        
        # 获取Shop菜单项
        shop_title_input = soup.select_one('input.menu-item-title[value="Shop"]')
        if not shop_title_input:
            raise RuntimeError("未找到菜单中设置的Shop")
        parent_li = shop_title_input.find_parent('li')
        if not parent_li:
            raise RuntimeError("无法找到Shop菜单项的父级列表项")
        shop_id_elem = parent_li.find('input', class_='menu-item-checkbox')
        if not shop_id_elem:
            raise RuntimeError("无法找到Shop菜单项的ID")
        shop_id = shop_id_elem.get("value")
        
        if not all([closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu_settings_column_nonce, menu, home_db, shop_id]):
            raise RuntimeError("未能获取所有必需的配置参数")
        
        return {
            "closedpostboxesnonce": closedpostboxesnonce,
            "meta_box_order_nonce": meta_box_order_nonce,
            "update_nav_menu_nonce": update_nav_menu_nonce,
            "menu_settings_column_nonce": menu_settings_column_nonce,
            "menu": menu,
            "home_db": home_db,
            "shop_id": shop_id,
            "referer": referer,
            "menu_name": menu_name
        }
    
    def _activate_category_list(self, domain, closedpostboxesnonce):
        """激活分类列表"""
        data = {
            'action': 'closed-postboxes',
            'hidden': 'add-post-type-product,add-post_tag,add-product_brand,add-product_tag',
            'closedpostboxesnonce': closedpostboxesnonce,
            'page': 'nav-menus'
        }
        response = self._request("POST", f"https://www.{domain}/wp-admin/admin-ajax.php", data=data)
        return response and response.status_code == 200

    def _build_category_hierarchy(self, rows):
        by_id = {str(row["term_id"]): row for row in rows}
        children = {}
        for row in rows:
            parent_id = str(row.get("parent_id", "0"))
            term_id = str(row["term_id"])
            children.setdefault(parent_id, []).append(term_id)

        result = []

        def walk(parent_id):
            for child_id in children.get(parent_id, []):
                row = by_id.get(child_id)
                if not row:
                    continue
                parent_title = None
                if parent_id != "0":
                    parent_row = by_id.get(parent_id)
                    if parent_row:
                        parent_title = parent_row.get("term_name")
                result.append(
                    {
                        "title": row.get("term_name"),
                        "parent_title": parent_title,
                    }
                )
                walk(child_id)

        walk("0")
        return result



    def _collect_menu_item_inputs(self, soup):
        data = {}
        for input_tag in soup.find_all("input"):
            name = input_tag.get("name")
            if not name:
                continue
            if not name.startswith("menu-item-"):
                continue
            value = input_tag.get("value", "")
            data[name] = value
        return data

    def _fetch_site_categories(self, domain):
        data = {
            "product_cat-tab": "all",
            "paged": "1",
            "item-type": "taxonomy",
            "item-object": "product_cat",
            "action": "menu-get-metabox",
        }
        url = f"https://www.{domain}/wp-admin/admin-ajax.php"
        items = []
        page = 1
        db_id = -1
        max_pages = 100
        seen_keys = set()
        last_markup = None

        while True:
            if page > max_pages:
                break
            current = data.copy()
            current["paged"] = str(page)
            response = self._request("POST", url, data=current)
            if not response:
                raise RuntimeError("Failed to fetch category list")

            response_text = response.text
            if "No items" in response_text:
                break

            try:
                payload = response.json()
                html_content = payload.get("markup", "")
            except ValueError:
                raise RuntimeError("Invalid category list response")
            if html_content == last_markup:
                break
            last_markup = html_content

            soup = BeautifulSoup(html_content, "html.parser")
            checklist = soup.find("ul", id="product_catchecklist")
            if not checklist:
                raise RuntimeError("Category list markup not found")

            li_items = checklist.find_all("li")
            if not li_items:
                break

            before = len(seen_keys)
            for li in li_items:
                title_elem = li.find("input", {"class": "menu-item-title"})
                checkbox_elem = li.find("input", {"class": "menu-item-checkbox"})
                url_elem = li.find("input", {"class": "menu-item-url"})
                if not all([title_elem, checkbox_elem, url_elem]):
                    continue
                key = (title_elem.get("value"), checkbox_elem.get("value"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                items.append(
                    {
                        "title": title_elem.get("value"),
                        "object_id": checkbox_elem.get("value"),
                        "url": url_elem.get("value"),
                        "db_id": str(db_id),
                    }
                )
                db_id -= 1
            if len(seen_keys) == before:
                break
            page += 1

        return items

    def _fetch_site_pages(self, domain):
        data = {
            "posttype-page-tab": "all",
            "paged": "1",
            "item-type": "post_type",
            "item-object": "page",
            "action": "menu-get-metabox",
        }
        url = f"https://www.{domain}/wp-admin/admin-ajax.php"
        items = []
        page = 1
        db_id = -1000
        max_pages = 100
        seen_keys = set()
        last_markup = None

        while True:
            if page > max_pages:
                break
            current = data.copy()
            current["paged"] = str(page)
            response = self._request("POST", url, data=current)
            if not response:
                raise RuntimeError("Failed to fetch page list")

            response_text = response.text
            if "No items" in response_text:
                break

            try:
                payload = response.json()
                html_content = payload.get("markup", "")
            except ValueError:
                raise RuntimeError("Invalid page list response")
            if html_content == last_markup:
                break
            last_markup = html_content

            soup = BeautifulSoup(html_content, "html.parser")
            checklist = soup.find("ul", id=re.compile(r"page.*checklist"))
            if not checklist:
                raise RuntimeError("Page list markup not found")

            li_items = checklist.find_all("li")
            if not li_items:
                break

            before = len(seen_keys)
            for li in li_items:
                title_elem = li.find("input", {"class": "menu-item-title"})
                checkbox_elem = li.find("input", {"class": "menu-item-checkbox"})
                url_elem = li.find("input", {"class": "menu-item-url"})
                if not all([title_elem, checkbox_elem, url_elem]):
                    continue
                key = (title_elem.get("value"), checkbox_elem.get("value"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                items.append(
                    {
                        "title": title_elem.get("value"),
                        "object_id": checkbox_elem.get("value"),
                        "url": url_elem.get("value"),
                        "db_id": str(db_id),
                    }
                )
                db_id -= 1
            if len(seen_keys) == before:
                break
            page += 1

        return items

    def _build_add_menu_items_form(self, hierarchy, categories, pages, existing_titles):
        category_map = {item["title"]: item for item in categories}
        page_map = {item["title"]: item for item in pages}
        used_db_ids = set()
        menu_items = {}
        db_id = -1

        for item in hierarchy:
            title = item["title"]
            if title in existing_titles:
                continue
            detail = category_map.get(title)
            if not detail:
                continue

            while str(db_id) in used_db_ids:
                db_id -= 1
            used_db_ids.add(str(db_id))

            menu_items[f"menu-item[{db_id}][menu-item-object-id]"] = detail["object_id"]
            menu_items[f"menu-item[{db_id}][menu-item-db-id]"] = "0"
            menu_items[f"menu-item[{db_id}][menu-item-object]"] = "product_cat"
            menu_items[f"menu-item[{db_id}][menu-item-parent-id]"] = "0"
            menu_items[f"menu-item[{db_id}][menu-item-type]"] = "taxonomy"
            menu_items[f"menu-item[{db_id}][menu-item-title]"] = title
            menu_items[f"menu-item[{db_id}][menu-item-url]"] = detail["url"]
            menu_items[f"menu-item[{db_id}][menu-item-status]"] = "publish"

            db_id -= 1

        if "Shop" not in existing_titles:
            shop = page_map.get("Shop")
            if shop:
                while str(db_id) in used_db_ids:
                    db_id -= 1
                used_db_ids.add(str(db_id))

                menu_items[f"menu-item[{db_id}][menu-item-object-id]"] = shop["object_id"]
                menu_items[f"menu-item[{db_id}][menu-item-db-id]"] = "0"
                menu_items[f"menu-item[{db_id}][menu-item-object]"] = "page"
                menu_items[f"menu-item[{db_id}][menu-item-parent-id]"] = "0"
                menu_items[f"menu-item[{db_id}][menu-item-type]"] = "post_type"
                menu_items[f"menu-item[{db_id}][menu-item-title]"] = "Shop"
                menu_items[f"menu-item[{db_id}][menu-item-url]"] = shop["url"]
                menu_items[f"menu-item[{db_id}][menu-item-target]"] = ""
                menu_items[f"menu-item[{db_id}][menu-item-attr-title]"] = ""
                menu_items[f"menu-item[{db_id}][menu-item-classes]"] = ""
                menu_items[f"menu-item[{db_id}][menu-item-xfn]"] = ""

        return menu_items

    def _extract_title_map_from_inputs(self, inputs):
        title_map = {}
        for name, value in inputs.items():
            if not name.startswith("menu-item-title["):
                continue
            match = re.search(r"menu-item-title\[(\d+)\]", name)
            if match:
                title_map[value] = match.group(1)
        return title_map

    def _build_parent_updates(self, hierarchy, title_to_db_id):
        updates = {}
        for item in hierarchy:
            parent_title = item.get("parent_title")
            if not parent_title:
                continue
            child_db = title_to_db_id.get(item.get("title"))
            parent_db = title_to_db_id.get(parent_title)
            if not child_db or not parent_db:
                continue
            updates[f"menu-item-parent[{child_db}]"] = str(parent_db)
            updates[f"menu-item-parent-id[{child_db}]"] = str(parent_db)
        return updates

    def _parse_menu_item_inputs_from_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        return self._collect_menu_item_inputs(soup)

    def _save_menu(self, domain, params, existing_inputs, new_inputs, parent_updates):
        data = {
            "closedpostboxesnonce": params["closedpostboxesnonce"],
            "meta-box-order-nonce": params["meta_box_order_nonce"],
            "update-nav-menu-nonce": params["update_nav_menu_nonce"],
            "_wp_http_referer": params["referer"] or "/wp-admin/nav-menus.php",
            "action": "update",
            "menu": params["menu"],
            "menu-name": params["menu_name"] or "primary-menu",
            "save_menu": "Save Menu",
        }
        data.update(existing_inputs)
        data.update(new_inputs)
        data.update(parent_updates)

        url = f"https://www.{domain}/wp-admin/nav-menus.php"
        response = self._request("POST", url, data=data)
        if not response:
            raise RuntimeError("Failed to save menu")
        return True

    def configure(self, domain):
        self._login(domain)
        params = self._get_menu_params(domain)
        html, soup = self._get_menu_page(domain)
        existing_inputs = self._collect_menu_item_inputs(soup)
        existing_titles = {
            input_tag.get("value")
            for input_tag in soup.find_all("input")
            if input_tag.get("name", "").startswith("menu-item-title[")
        }

        rows = self._fetch_all_remote_categories()
        hierarchy = self._build_category_hierarchy(rows)
        categories = self._fetch_site_categories(domain)
        pages = self._fetch_site_pages(domain)
        add_form = self._build_add_menu_items_form(hierarchy, categories, pages, existing_titles)

        if not add_form:
            return True

        add_data = {
            "action": "add-menu-item",
            "menu": params["menu"],
            "menu-settings-column-nonce": params["menu_settings_column_nonce"],
        }
        add_data.update(add_form)

        ajax_url = f"https://www.{domain}/wp-admin/admin-ajax.php"
        add_response = self._request("POST", ajax_url, data=add_data)
        if not add_response:
            raise RuntimeError("Failed to add menu items")

        new_inputs = self._parse_menu_item_inputs_from_html(add_response.text)
        if not new_inputs:
            raise RuntimeError("Failed to parse new menu items")

        title_to_db_id = {}
        title_to_db_id.update(self._extract_title_map_from_inputs(existing_inputs))
        title_to_db_id.update(self._extract_title_map_from_inputs(new_inputs))
        parent_updates = self._build_parent_updates(hierarchy, title_to_db_id)

        self._save_menu(domain, params, existing_inputs, new_inputs, parent_updates)
        return True
