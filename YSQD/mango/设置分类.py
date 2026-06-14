import requests
import time
import urllib3
from bs4 import BeautifulSoup
from openpyxl import load_workbook
import re
import html
import pandas as pd
from pathlib import Path
import json
import sys
import os
from src.utils.logger import setup_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration
DEFAULT_PASSWORD = "f!XsS$J2WneOkMyUgQ"
FAILED_LOG_FILE = r"D:\python_work\work_wool\建站\设置分类\failed_domains.log"

# 设置日志
logger = setup_logger('set_category', FAILED_LOG_FILE)



def find_domain_xlsx_paths():
    """Find Excel files based on domain status from the main Excel file."""
    print("--- 步骤 1: 递归查找待处理的域名和数据文件 ---")
    main_file_path = Path(r"C:\Users\Administrator\Desktop\建站域名管理.xlsx")
    target_directory = Path(r"D:\上传数据")

    if not main_file_path.exists():
        print(f"❌ 错误：找不到主文件 {main_file_path}")
        return []

    try:
        df = pd.read_excel(main_file_path, sheet_name=0)
    except Exception as e:
        print(f"❌ 读取 Excel 文件时出错: {e}")
        return []

    required_columns = ['域名', '是否设置分类']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        print(f"❌ 错误：Excel 文件中缺少以下列: {missing_columns}")
        return []

    filtered_domains = df[df['是否设置分类'].astype(str).str.contains('否|否 ', na=False)]['域名']
    target_domains = filtered_domains.tolist()

    print(f"🔍 找到 {len(target_domains)} 个未设置分类的域名: {target_domains}")

    found_paths = []
    target_domains_set = set(target_domains)

    for xlsx_file in target_directory.rglob('*.xlsx'):
        file_name = xlsx_file.name
        parent_dir_name = xlsx_file.parent.name

        if file_name == f"{parent_dir_name}.xlsx" and parent_dir_name in target_domains_set:
            found_paths.append(str(xlsx_file))
            print(f"  ✅ 找到文件: {xlsx_file}")
            target_domains_set.discard(parent_dir_name)

    missing_domains = list(target_domains_set)
    if missing_domains:
        for domain in missing_domains:
            print(f"  ⚠️ 警告: 未找到域名 '{domain}' 对应的 Excel 文件。")

    if not found_paths:
        print("⚠️ 未找到任何符合条件的文件。")
    else:
        print(f"✅ 成功找到 {len(found_paths)} 个待处理文件。")
    return found_paths


def main_data(file_path):
    """Parse the Excel file to build the category tree structure."""
    print(f"--- 步骤 2: 解析主数据文件 '{file_path}' ---")
    try:
        df = pd.read_excel(file_path, engine='openpyxl')
    except FileNotFoundError:
        print(f"❌ 错误: 文件未找到 - {file_path}")
        return {}
    except Exception as e:
        print(f"❌ 读取文件时发生错误: {e}")
        return {}

    if "Categories" not in df.columns:
        print("❌ 错误: 文件中未找到名为 'Categories' 的列。")
        return {}

    categories_series = df["Categories"].dropna()

    tree_dict = {}
    path_to_info = {}
    path_to_row_index = {}

    for idx, row in categories_series.items():
        if pd.isna(row) or row == "":
            continue
        parts = [part.strip() for part in row.split('|||') if part.strip()]
        if not parts:
            continue

        full_path = row
        path_to_row_index[full_path] = idx

        current_path_parts = []
        for i, part in enumerate(parts):
            current_path_parts.append(part)
            current_full_path = "|||".join(current_path_parts)

            if current_full_path not in tree_dict:
                tree_dict[current_full_path] = {"children": []}

            if i < len(parts) - 1:
                next_path_parts = current_path_parts + [parts[i + 1]]
                next_full_path = "|||".join(next_path_parts)
                if next_full_path not in tree_dict[current_full_path]["children"]:
                    tree_dict[current_full_path]["children"].append(next_full_path)

    absolute_counter = 1

    def dfs_calculate_positions(node_path, parent_path=None):
        nonlocal absolute_counter
        node_name = node_path.split('|||')[-1]
        parent_keyword = None
        if parent_path:
            parent_keyword = parent_path.split('|||')[-1]

        relative_pos = 1
        if parent_path:
            if node_path in tree_dict[parent_path]["children"]:
                relative_pos = tree_dict[parent_path]["children"].index(node_path) + 1
        else:
            root_nodes = [p for p in tree_dict.keys() if
                          not any(node_path in tree_dict[k].get("children", []) for k in tree_dict)]
            if node_path in root_nodes:
                relative_pos = sorted(root_nodes).index(node_path) + 1

        absolute_pos = absolute_counter
        absolute_counter += 1

        path_to_info[node_path] = {
            "full_path": node_path,
            "keyword": node_name,
            "parent_keyword": parent_keyword,
            "relative_pos": relative_pos,
            "absolute_pos": absolute_pos
        }

        for child_path in tree_dict.get(node_path, {}).get("children", []):
            dfs_calculate_positions(child_path, node_path)

    all_nodes = set(tree_dict.keys())
    all_child_nodes = set()
    for node_info in tree_dict.values():
        all_child_nodes.update(node_info["children"])
    root_nodes = sorted(list(all_nodes - all_child_nodes))

    for root in root_nodes:
        dfs_calculate_positions(root)

    print(f"✅ 成功解析 {len(path_to_info)} 个分类项。")
    return path_to_info


def request_with_retry(session, method, url, retries=3, delay=5, verify_ssl=False, **kwargs):
    """Make a request with retry logic."""
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


def category_dic(session, domain):
    """Fetch existing categories from the site."""
    print(f"--- 步骤 3: 获取站点 '{domain}' 的现有分类列表 ---")
    data = {
        "product_cat-tab": "all",
        "paged": '1',
        "item-type": "taxonomy",
        "item-object": "product_cat",
        'action': 'menu-get-metabox'
    }
    url = f"https://www.{domain}/wp-admin/admin-ajax.php"
    items = []
    paged = 1
    db_id = -1
    while True:
        current_data = data.copy()
        current_data['paged'] = str(paged)
        response = request_with_retry(session, 'POST', url, data=current_data)
        if not response:
            print(f"❌ 从站点 '{domain}' 获取分类列表时发生致命错误。")
            return []

        response_text = response.text
        if 'No items' in response_text:
            break
        try:
            response_json = json.loads(response_text)
            html_content = response_json['markup']
        except (json.JSONDecodeError, KeyError):
            print(f"❌ 解析分类列表响应时出错。响应内容: {response_text[:200]}...")
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        checklist_ul = soup.find('ul', id='product_catchecklist')
        if not checklist_ul:
             print(f"❌ 在分类列表响应中未找到 'product_catchecklist' 元素。")
             return []
        li_elements = checklist_ul.find_all('li')
        for li in li_elements:
            title_elem = li.find('input', {'class': "menu-item-title"})
            checkbox_elem = li.find('input', {'class': "menu-item-checkbox"})
            url_elem = li.find('input', {'class': "menu-item-url"})

            if not all([title_elem, checkbox_elem, url_elem]):
                print(f"⚠️ 跳过一个格式不完整的分类项。")
                continue

            title = title_elem.get('value')
            fenlei_id = checkbox_elem.get('value')
            fenlei_url = url_elem.get('value')

            item_dict = {
                'title': title,
                'fenlei_id': fenlei_id,
                'db_id': str(db_id),
                'fenlei_url': fenlei_url
            }
            items.append(item_dict)
            db_id -= 1
        paged += 1
    print(f"✅ 成功获取 {len(items)} 个现有分类。")
    return items


def login_site(domain, password=None):
    """Login to the WordPress site."""
    print(f"--- 步骤 0: 登录站点 '{domain}' ---")
    if password is None:
        password = DEFAULT_PASSWORD

    session = requests.Session()
    login_url = f"https://www.{domain}/bbwllogin/"
    name = domain.replace('.com', '').strip()
    username = f'Ad{name}min'

    data = {
        'log': username,
        'pwd': password,
        'wp-submit': 'Log In',
        'redirect_to': f"https://www.{domain}/wp-admin/",
        'testcookie': '1'
    }

    try:
        print(f"  正在尝试登录 {domain}，用户名: {username}...")
        resp = request_with_retry(session, 'POST', login_url, data=data)
        if not resp:
            print(f"❌ {domain} 登录请求失败 (重试后)")
            return None

        print(f"  登录响应状态码: {resp.status_code}")

        if any("wordpress_logged_in" in c.name for c in session.cookies):
            print(f"  ✅ {domain} 登录成功 (找到 wordpress_logged_in cookie)")
            return session
        else:
            print(f"  ⚠️ {domain} 未找到 wordpress_logged_in cookie，正在检查 /wp-admin/ 访问权限...")

        admin_check_resp = request_with_retry(session, 'GET', f"https://www.{domain}/wp-admin/")
        if admin_check_resp and admin_check_resp.status_code == 200:
            print(f"  ✅ {domain} 登录成功 (可以访问 /wp-admin/)")
            return session
        else:
            print(f"  ❌ 无法访问 /wp-admin/，状态码: {admin_check_resp.status_code if admin_check_resp else 'None'}")

    except Exception as e:
        print(f"❌ {domain} 登录过程中发生未知错误: {e}")

    print(f"❌ {domain} 登录失败")
    return None


def get_canshu(session, domain):
    """Get necessary parameters from the site's menu page."""
    print(f"--- 步骤 4: 获取站点 '{domain}' 的菜单配置参数 ---")
    menus_resp = request_with_retry(session, 'GET', f"https://www.{domain}/wp-admin/nav-menus.php")
    if not menus_resp or menus_resp.status_code != 200:
        print(f"❌ 获取菜单页面失败，状态码: {menus_resp.status_code if menus_resp else 'None'}")
        return None

    soup = BeautifulSoup(menus_resp.text, 'html.parser')
    input_tag1 = soup.find('input', {'id': 'closedpostboxesnonce'})
    input_tag2 = soup.find('input', {'id': 'meta-box-order-nonce'})
    input_tag3 = soup.find('input', {'id': 'update-nav-menu-nonce'})
    input_tag4 = soup.find('input', {'id': 'nav-menu-meta-object-id'})
    input_tag5 = soup.find('input', {'id': 'menu-settings-column-nonce'})
    home_inputs = soup.find('input', attrs={'class': 'widefat edit-menu-item-title', 'value': 'Home' })
    shop_title_input = soup.select_one('input.menu-item-title[value="Shop"]')

    if not shop_title_input:
        print("❌ 未找到菜单中设置的Shop，请先在WordPress后台创建Shop页面并添加到菜单。")
        return None

    parent_li = shop_title_input.find_parent('li')
    if not parent_li:
        print("❌ 无法找到Shop菜单项的父级列表项。")
        return None

    shop_id_elem = parent_li.find('input', class_='menu-item-checkbox')
    if not shop_id_elem:
        print("❌ 无法找到Shop菜单项的ID。")
        return None

    shop_id = shop_id_elem.get("value")

    closedpostboxesnonce = input_tag1.get('value') if input_tag1 else None
    meta_box_order_nonce = input_tag2.get('value') if input_tag2 else None
    update_nav_menu_nonce = input_tag3.get('value') if input_tag3 else None
    menu = input_tag4.get('value') if input_tag4 else None
    menu_settings_column_nonce = input_tag5.get('value') if input_tag5 else None

    if home_inputs:
        home_id = home_inputs.get('id')
        id_match = re.search(r'edit-menu-item-title-(\d+)', home_id)
        if id_match:
            home_db = id_match.group(1)
        else:
            print("❌ 无法从Home输入框ID中解析出db_id。")
            return None
    else:
        print("❌ 未找到菜单中设置的Home，请自行添加Home菜单")
        return None

    if not all([closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu_settings_column_nonce, menu, home_db, shop_id]):
        print("❌ 未能获取所有必需的配置参数。")
        return None

    print(f"✅ 成功获取所有配置参数。Home_db_id: {home_db}, Shop_id: {shop_id}")
    return closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu_settings_column_nonce, menu, home_db, shop_id


def at_type(session, domain, closedpostboxesnonce):
    """Activate the category list."""
    print(f"--- 步骤 5: 激活站点 '{domain}' 的分类列表 ---")
    if session and domain:
        data = {
            'action': 'closed-postboxes',
            'hidden': 'add-post-type-product,add-post_tag,add-product_brand,add-product_tag',
            'closedpostboxesnonce': f'{closedpostboxesnonce}',
            'page': 'nav-menus'
        }
        response = request_with_retry(session, 'POST', f"https://www.{domain}/wp-admin/admin-ajax.php", data=data)
        if response and response.status_code == 200:
            print(f"✅ 激活分类列表成功")
        else:
            print(f"❌ 激活分类列表失败，状态码: {response.status_code if response else 'None'}")
            return False
    else:
        print("❌ 无效的session 或域名，无法激活分类列表")
        return False
    return True


def create_add_category_form(main_data, category, menu, menu_settings_column_nonce, shop_id):
    """Create the form data for adding categories."""
    print(f"--- 步骤 6: 构建添加分类的表单数据并更新 main_data ---")
    
    available_categories = category.copy()
    used_db_ids = set()
    add_category_form = []

    for full_path, info in main_data.items():
        keyword = info['keyword']
        matched_detail = None

        for cat_item in available_categories:
            if cat_item['title'] == keyword:
                matched_detail = cat_item
                available_categories.remove(cat_item)
                break
        
        if not matched_detail:
            print(f"⚠️ 警告: 未找到关键词 '{keyword}' 对应的未使用详情信息，跳过。")
            continue

        db_id_str = matched_detail['db_id']
        if db_id_str in used_db_ids:
             print(f"❌ 严重错误: 找到的 category 项的 db_id '{db_id_str}' 已存在，这不应该发生。请检查 category 数据。")
             continue
        used_db_ids.add(db_id_str)

        db_id = db_id_str
        fenlei_id = matched_detail['fenlei_id']
        fenlei_url = matched_detail['fenlei_url']

        main_data[full_path]['fenlei_id'] = fenlei_id

        menu_item_data = {
            f'menu-item[{db_id}][menu-item-object-id]': fenlei_id,
            f'menu-item[{db_id}][menu-item-db-id]': '0',
            f'menu-item[{db_id}][menu-item-object]': 'product_cat',
            f'menu-item[{db_id}][menu-item-parent-id]': '0',
            f'menu-item[{db_id}][menu-item-type]': 'taxonomy',
            f'menu-item[{db_id}][menu-item-title]': keyword,
            f'menu-item[{db_id}][menu-item-url]': fenlei_url,
            f'menu-item[{db_id}][menu-item-target]': '',
            f'menu-item[{db_id}][menu-item-attr-title]': '',
            f'menu-item[{db_id}][menu-item-classes]': '',
            f'menu-item[{db_id}][menu-item-xfn]': ''
        }
        add_category_form.append(menu_item_data)

    shop_db_id_int = 0
    while str(shop_db_id_int) in used_db_ids:
        shop_db_id_int += 1
    shop_db_id_str = str(shop_db_id_int)
    used_db_ids.add(shop_db_id_str)

    shop_menu_item_data = {
        f'menu-item[{shop_db_id_str}][menu-item-object-id]': str(shop_id),
        f'menu-item[{shop_db_id_str}][menu-item-db-id]': '0',
        f'menu-item[{shop_db_id_str}][menu-item-object]': 'page',
        f'menu-item[{shop_db_id_str}][menu-item-parent-id]': '0',
        f'menu-item[{shop_db_id_str}][menu-item-type]': 'post_type',
        f'menu-item[{shop_db_id_str}][menu-item-title]': 'Shop',
        f'menu-item[{shop_db_id_str}][menu-item-url]': 'https://www.snowmenart.com/shop/',
        f'menu-item[{shop_db_id_str}][menu-item-target]': '',
        f'menu-item[{shop_db_id_str}][menu-item-attr-title]': '',
        f'menu-item[{shop_db_id_str}][menu-item-classes]': '',
        f'menu-item[{shop_db_id_str}][menu-item-xfn]': ''
    }
    add_category_form.append(shop_menu_item_data)

    data = {
        'action': 'add-menu-item',
        'menu': f'{menu}',
        'menu-settings-column-nonce': f'{menu_settings_column_nonce}'
    }

    addfrom = merge_form(add_category_form)
    data.update(addfrom)
    print(f"✅ 成功构建包含 {len(add_category_form)} 个菜单项的表单数据。")
    return main_data, data

def merge_form(menu_item_list):
    merged_dict = {}
    for item in menu_item_list:
        merged_dict.update(item)
    return merged_dict


def creat_save_from(session, main_data, category, data, home_db, shop_id, domain):
    """Extract new IDs and restructure the data for saving."""
    print(f"--- 步骤 7: 提取新ID并重组保存数据 ---")
    url = f"https://www.{domain}/wp-admin/admin-ajax.php"
    response = request_with_retry(session, 'POST', url, data=data)
    if not response:
        print(f"❌ 提交临时菜单项失败。")
        return False, None
    response_text = html.unescape(response.text)
    li_pattern = r'<li\s+id="menu-item-(\d+)"[^>]*>(.*?)</li>'
    li_matches = re.findall(li_pattern, response_text, re.DOTALL)
    results = []
    for db_id, li_content in li_matches:
        title_pattern = rf'name="menu-item-title\[{re.escape(db_id)}\]"[^>]*value="([^"]*)"'
        title_match = re.search(title_pattern, li_content)
        
        object_id_pattern = rf'name="menu-item-object-id\[{re.escape(db_id)}\]"[^>]*value="([^"]*)"'
        object_id_match = re.search(object_id_pattern, li_content)

        if title_match and object_id_match:
            title = title_match.group(1)
            object_id = object_id_match.group(1)
            results.append((db_id, title, object_id))
        else:
            print(f"警告: 在菜单项 {db_id} 中未找到完整的标题或分类ID信息。")

    main_data_key_to_details = {(item['keyword'], str(item['fenlei_id'])): item for item in main_data.values()}
    title_and_obj_id_to_new_db_id = {(item[1].strip(), item[2]): int(item[0]) for item in results}
    
    all_keywords_found = True
    missing_keywords = []
    for full_path, info in main_data.items():
        keyword = info['keyword']
        fenlei_id = str(info['fenlei_id'])
        lookup_key = (keyword, fenlei_id)
        
        if lookup_key not in title_and_obj_id_to_new_db_id:
            print(f"❌ 错误: 关键词 '{keyword}' (分类ID: {fenlei_id}) 在响应中未找到对应的新db_id。")
            missing_keywords.append((keyword, fenlei_id))
            all_keywords_found = False

    shop_found = False
    for db_id, title, object_id in results:
        if title == 'Shop':
            shop_found = True
            break
    if not shop_found:
        print(f"❌ 错误: 关键词 'Shop' 在响应中未找到对应的新db_id。")
        missing_keywords.append(('Shop', 'N/A'))
        all_keywords_found = False

    if not all_keywords_found:
        print(f"❌ 由于以下关键词未找到新db_id，整个站点 '{domain}' 的处理失败: {missing_keywords}")
        return False, None

    restructured_data = {}
    shop_db_id = None
    for db_id, title, object_id in results:
        if title == 'Shop':
            shop_db_id = int(db_id)
            break
    if shop_db_id is None:
        print("❌ 未能找到Shop菜单项的新db_id。")
        return False, None

    all_menu_items = []
    home_id = str(home_db)
    all_menu_items.append({
        'type': 'home',
        'db_id': home_id,
        'title': 'Home',
        'url': '/',
        'parent_id': '0',
        'relative_pos': 1,
        'absolute_pos': 1,
        'object_id': home_id,
        'object': 'custom'
    })

    top_level_items_count = 0
    all_items_count = 0

    for full_path, info in main_data.items():
        keyword = info['keyword']
        parent_keyword = info['parent_keyword']
        fenlei_id = str(info['fenlei_id'])
        original_relative_pos = info['relative_pos']
        original_absolute_pos = info['absolute_pos']

        lookup_key = (keyword, fenlei_id)
        new_db_id = title_and_obj_id_to_new_db_id.get(lookup_key)

        if new_db_id is None:
            print(f"❌ 警告: 关键词 '{keyword}' (分类ID: {fenlei_id}) 在响应中未找到对应的新db_id。")
            continue

        parent_id = '0'
        if parent_keyword:
            parent_fenlei_id = None
            for item in main_data.values():
                if item['keyword'] == parent_keyword:
                    parent_fenlei_id = str(item['fenlei_id'])
                    break
            if parent_fenlei_id and (parent_keyword, parent_fenlei_id) in title_and_obj_id_to_new_db_id:
                parent_id = str(title_and_obj_id_to_new_db_id[(parent_keyword, parent_fenlei_id)])
            elif parent_fenlei_id:
                print(f"❌ 警告: 父级关键词 '{parent_keyword}' (分类ID: {parent_fenlei_id}) 在响应中未找到对应的新db_id，设为 '0'。")
            else:
                print(f"❌ 警告: 无法在 main_data 中找到父级关键词 '{parent_keyword}' 的 fenlei_id，设为 '0'。")

        adjusted_relative_pos = original_relative_pos
        if parent_id == '0':
            adjusted_relative_pos += 1
            top_level_items_count += 1

        adjusted_absolute_pos = original_absolute_pos + 1
        all_items_count += 1

        all_menu_items.append({
            'type': 'category',
            'db_id': str(new_db_id),
            'title': keyword,
            'parent_id': parent_id,
            'relative_pos': adjusted_relative_pos,
            'absolute_pos': adjusted_absolute_pos,
            'object_id': fenlei_id,
            'object': 'product_cat'
        })

    shop_order = top_level_items_count + 2
    shop_position = all_items_count + 1 + 1

    all_menu_items.append({
        'type': 'shop',
        'db_id': str(shop_db_id),
        'title': 'Shop',
        'parent_id': '0',
        'relative_pos': shop_order,
        'absolute_pos': shop_position,
        'object_id': str(shop_id),
        'object': 'page'
    })

    sorted_menu_items = sorted(all_menu_items, key=lambda x: x['absolute_pos'])

    for i, item in enumerate(sorted_menu_items):
        item['absolute_pos'] = i + 1

    top_level_items = [item for item in sorted_menu_items if item['parent_id'] == '0']
    top_level_items.sort(key=lambda x: x['relative_pos'])
    for i, item in enumerate(top_level_items):
        item['relative_pos'] = i + 1

    for item in sorted_menu_items:
        db_id = item['db_id']
        restructured_data[f'menu-item-title[{db_id}]'] = item['title']
        restructured_data[f'menu-item-attr-title[{db_id}]'] = ''
        restructured_data[f'menu-item-classes[{db_id}]'] = ''
        restructured_data[f'menu-item-xfn[{db_id}]'] = ''
        restructured_data[f'menu-item-description[{db_id}]'] = ''
        restructured_data[f'menu-item-parent[{db_id}]'] = item['parent_id']
        restructured_data[f'menu-item-order[{db_id}]'] = str(item['relative_pos'])
        restructured_data[f'menu-item-db-id[{db_id}]'] = str(db_id)
        restructured_data[f'menu-item-object-id[{db_id}]'] = item['object_id']
        restructured_data[f'menu-item-object[{db_id}]'] = item['object']
        restructured_data[f'menu-item-parent-id[{db_id}]'] = item['parent_id']
        restructured_data[f'menu-item-position[{db_id}]'] = str(item['absolute_pos'])
        restructured_data[f'menu-item-type[{db_id}]'] = 'post_type' if item['object'] == 'page' else 'custom' if item['object'] == 'custom' else 'taxonomy'

        if item['type'] == 'home':
            restructured_data[f'menu-item-url[{db_id}]'] = item['url']

    print(f"✅ 成功重组包含 {len(restructured_data)} 个字段的保存数据。")
    return True, restructured_data


def save_menu(session, data1, domain, closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu):
    """Save the menu to the site."""
    print(f"--- 步骤 8: 保存菜单到站点 '{domain}' ---")
    required_db_ids = set()
    for key in data1.keys():
        if key.startswith('menu-item-db-id['):
            match = re.search(r'\[(\d+)\]', key)
            if match:
                required_db_ids.add(match.group(1))

    if not required_db_ids:
        print("❌ 错误: data1中没有找到任何db_id")
        return False

    print(f"  需要验证的db_ids数量: {len(required_db_ids)}")

    data = {
        "closedpostboxesnonce": f"{closedpostboxesnonce}",
        "meta-box-order-nonce": f"{meta_box_order_nonce}",
        "update-nav-menu-nonce": f"{update_nav_menu_nonce}",
        "_wp_http_referer": "/wp-admin/nav-menus.php",
        "action": "update",
        "menu": f"{menu}",
        "menu-name": "primary-menu",
        "save_menu": "Save Menu"
    }
    data.update(data1)

    url = f"https://www.{domain}/wp-admin/nav-menus.php"
    response = request_with_retry(session, 'POST', url, data=data)
    if not response:
        print(f"❌ 保存菜单请求失败。")
        return False

    response_text = response.text

    missing_db_ids = []

    for db_id in required_db_ids:
        pattern = f'<label\\s+class="item-title"\\s+for=[\'"]menu-item-checkbox-{db_id}[\'"]'

        if not re.search(pattern, response_text):
            missing_db_ids.append(db_id)

    if missing_db_ids:
        print(f"❌ 保存失败。缺失的db_ids: {missing_db_ids[:5]}...")
        return False
    else:
        print(f"✅ 菜单在站点 '{domain}' 上保存成功！")
        return True


def log_failure(domain):
    """Log the failed domain."""
    logger.info(domain)


def get_failed_domains():
    """Read the list of failed domains from the log file."""
    if os.path.exists(FAILED_LOG_FILE):
        try:
            with open(FAILED_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            failed_domains = [line.strip() for line in lines if line.strip()]
            return failed_domains
        except Exception as e:
            print(f"❌ 读取失败日志文件 {FAILED_LOG_FILE} 时出错: {e}")
            return []
    return []


def clear_failed_log():
    """Clear the failure log file."""
    try:
        open(FAILED_LOG_FILE, 'w').close()
        print(f"--- 已清空失败日志文件 {FAILED_LOG_FILE} ---")
    except Exception as e:
        print(f"❌ 清空失败日志文件 {FAILED_LOG_FILE} 时出错: {e}")


def process_domain(file_path_str, domain):
    """Process a single domain."""
    file_path = Path(file_path_str)
    print(f"\n--- 处理站点: {domain} (数据文件: {file_path.name}) ---")

    session = login_site(domain)
    if not session:
        print(f"❌ 登录站点 '{domain}' 失败，将记录到失败日志。")
        log_failure(domain)
        return False

    params = get_canshu(session, domain)
    if not params:
        print(f"❌ 获取站点 '{domain}' 参数失败，将记录到失败日志。")
        log_failure(domain)
        return False
    closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu_settings_column_nonce, menu, home_db, shop_id = params

    activation_success = at_type(session, domain, closedpostboxesnonce)
    if not activation_success:
        print(f"❌ 激活站点 '{domain}' 分类列表失败，将记录到失败日志。")
        log_failure(domain)
        return False

    main_data_dict = main_data(str(file_path))
    if not main_data_dict:
        print(f"❌ 解析站点 '{domain}' 数据文件失败，将记录到失败日志。")
        log_failure(domain)
        return False

    category_list = category_dic(session, domain)
    if not category_list:
        print(f"❌ 获取站点 '{domain}' 现有分类失败，将记录到失败日志。")
        log_failure(domain)
        return False

    maindata, form_data = create_add_category_form(main_data_dict, category_list, menu, menu_settings_column_nonce, shop_id)
    if not form_data:
        print(f"❌ 构建站点 '{domain}' 添加表单数据失败，将记录到失败日志。")
        log_failure(domain)
        return False

    success, final_save_data = creat_save_from(session, maindata, category_list, form_data, home_db, shop_id, domain)
    if not success:
        print(f"❌ 重组站点 '{domain}' 保存数据失败或主数据关键词未完全匹配，将记录到失败日志。")
        log_failure(domain)
        return False

    save_success = save_menu(session, final_save_data, domain, closedpostboxesnonce, meta_box_order_nonce, update_nav_menu_nonce, menu)
    if save_success:
        print(f"🎉 站点 '{domain}' 的菜单设置已成功完成！")
        return True
    else:
        print(f"❌ 站点 '{domain}' 的菜单保存失败，将记录到失败日志。")
        log_failure(domain)
        return False


if __name__ == "__main__":
    print("="*50)
    print("开始批量设置 WordPress 菜单")
    print("="*50)

    file_paths = find_domain_xlsx_paths()
    if not file_paths:
        print("❌ 没有找到需要处理的文件，程序退出。")
        sys.exit(1)

    pending_domains = [Path(fp).parent.name for fp in file_paths]
    print(f"\n--- 找到 {len(pending_domains)} 个待处理的域名: {pending_domains} ---")

    user_input = input("是否开始处理这些域名？(y/N): ").strip().lower()
    if user_input not in ['y', 'yes']:
        print("用户取消操作，程序退出。")
        sys.exit(0)

    print("\n--- 开始第一轮处理 ---")
    for file_path_str in file_paths:
        domain = Path(file_path_str).parent.name
        process_domain(file_path_str, domain)

    failed_domains = get_failed_domains()
    if failed_domains:
        print(f"\n--- 第一轮处理结束，发现 {len(failed_domains)} 个失败的域名: {failed_domains} ---")
        retry_input = input("是否对失败的域名进行重试？(y/N): ").strip().lower()
        if retry_input in ['y', 'yes']:
            print("\n--- 开始重试失败的域名 ---")
            clear_failed_log()
            for file_path_str in file_paths:
                domain = Path(file_path_str).parent.name
                if domain in failed_domains:
                    print(f"\n--- 重试处理站点: {domain} ---")
                    process_domain(file_path_str, domain)

            final_failed_domains = get_failed_domains()
            if final_failed_domains:
                print(f"\n--- 重试后仍有 {len(final_failed_domains)} 个域名失败: {final_failed_domains} ---")
                print(f"这些域名已记录在 {FAILED_LOG_FILE} 中。")
            else:
                print("\n--- 重试完成，所有失败域名均已成功处理！ ---")
                if os.path.exists(FAILED_LOG_FILE):
                     os.remove(FAILED_LOG_FILE)
                     print(f"--- 未发现失败域名，已删除日志文件 {FAILED_LOG_FILE} ---")
        else:
            print("用户选择不重试失败的域名。")
            print(f"失败的域名已记录在 {FAILED_LOG_FILE} 中。")
    else:
        print("\n--- 第一轮处理完成，没有发现失败的域名。 ---")
        if os.path.exists(FAILED_LOG_FILE):
             os.remove(FAILED_LOG_FILE)
             print(f"--- 未发现失败域名，已删除旧的日志文件 {FAILED_LOG_FILE} ---")

    print("\n" + "="*50)
    print("批量设置任务完成。")
    print("="*50)