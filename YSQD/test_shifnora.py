import sys
import os
import traceback

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_category import AutoCategoryConfigurator

def test_shifnora():
    """测试 shifnora.com 的主分类设置"""
    try:
        domain = "shifnora.com"
        # 从数据库获取主分类和密码
        from datastore import DataStore
        from constants import DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS
        
        store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)
        rows = store.query_rows('')
        
        target_row = None
        for row in rows:
            if row['domain'] == domain:
                target_row = row
                break
        
        if not target_row:
            print(f"未找到域名: {domain}")
            return
        
        main_category = (target_row['main_category'] or '').strip()
        wp_password = store.get_setting('wp_password', '').strip()
        
        print(f"测试域名: {domain}")
        print(f"主分类: {main_category}")
        print(f"WP密码: {'***' if wp_password else '空'}")
        
        if not main_category:
            print("错误: 主分类为空")
            return
        
        if not wp_password:
            print("错误: WP密码为空")
            return
        
        configurator = AutoCategoryConfigurator(wp_password)
        
        # 步骤1: 登录
        print("步骤1: 登录WordPress站点...")
        configurator._login(domain)
        print("✅ 登录成功")
        
        # 步骤2: 获取菜单参数
        print("步骤2: 获取菜单配置参数...")
        params = configurator._get_menu_params(domain)
        print(f"✅ 获取参数成功: menu={params['menu']}")
        
        # 步骤3: 激活分类列表
        print("步骤3: 激活分类列表...")
        activation_success = configurator._activate_category_list(domain, params["closedpostboxesnonce"])
        print(f"✅ 激活成功: {activation_success}")
        
        # 步骤4: 获取远程分类
        print("步骤4: 获取远程分类数据...")
        rows = configurator._fetch_all_remote_categories()
        print(f"✅ 获取远程分类: {len(rows)} 个")
        
        # 步骤5: 构建分类层级
        print("步骤5: 构建分类层级...")
        hierarchy = configurator._build_category_hierarchy(rows)
        print(f"✅ 构建层级: {len(hierarchy)} 个节点")
        
        # 步骤6: 获取站点分类
        print("步骤6: 获取站点现有分类...")
        categories = configurator._fetch_site_categories(domain)
        print(f"✅ 获取站点分类: {len(categories)} 个")
        
        # 步骤7: 获取站点页面
        print("步骤7: 获取站点页面...")
        pages = configurator._fetch_site_pages(domain)
        print(f"✅ 获取站点页面: {len(pages)} 个")
        
        # 步骤8: 构建添加表单
        print("步骤8: 构建添加分类的表单数据...")
        add_form = configurator._build_add_menu_items_form(hierarchy, categories, pages, set())
        print(f"✅ 构建表单: {len(add_form)} 个字段")
        
        if not add_form:
            print("⚠️ 没有需要添加的菜单项")
            return
        
        # 步骤9: 添加菜单项
        print("步骤9: 添加菜单项...")
        add_data = {
            "action": "add-menu-item",
            "menu": params["menu"],
            "menu-settings-column-nonce": params["menu_settings_column_nonce"],
        }
        add_data.update(add_form)
        
        ajax_url = f"https://www.{domain}/wp-admin/admin-ajax.php"
        add_response = configurator._request("POST", ajax_url, data=add_data)
        if not add_response:
            raise Exception("添加菜单项失败")
        print(f"✅ 添加成功: status={add_response.status_code}")
        
        # 步骤10: 解析新菜单项
        print("步骤10: 解析新菜单项...")
        new_inputs = configurator._parse_menu_item_inputs_from_html(add_response.text)
        print(f"✅ 解析成功: {len(new_inputs)} 个输入项")
        
        # 步骤11: 构建父级更新
        print("步骤11: 构建父级更新...")
        title_to_db_id = {}
        title_to_db_id.update(configurator._extract_title_map_from_inputs({}))
        title_to_db_id.update(configurator._extract_title_map_from_inputs(new_inputs))
        parent_updates = configurator._build_parent_updates(hierarchy, title_to_db_id)
        print(f"✅ 构建父级更新: {len(parent_updates)} 个更新")
        
        # 步骤12: 保存菜单
        print("步骤12: 保存菜单...")
        result = configurator._save_menu(domain, params, {}, new_inputs, parent_updates)
        print(f"✅ 保存成功: {result}")
        
        print("\n🎉 主分类设置成功！")
        
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        print("详细错误:")
        traceback.print_exc()

if __name__ == "__main__":
    test_shifnora()