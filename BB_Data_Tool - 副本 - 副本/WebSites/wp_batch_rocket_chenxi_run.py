#开启设置火箭插件-晨曦版
import requests
from bs4 import BeautifulSoup

# 打印使用说明
def print_usage():
    print("脚本使用说明：")
    print("1. 请将需要处理的站点域名（如 example.com）逐行写入一个 .txt 文件，不要包含 'https://' 或 'www' 前缀。")
    print("2. 脚本将读取您输入的文件并依次执行 WP Rocket 登录与设置操作。\n")
    print("3. 注意检查是否开启并设置成功。\n")
    print("==============================================================\n")

# 定义函数 RocketSetting，传入站点域名
def RocketSetting(site):
    session = requests.Session()  # 创建一个会话对象，用于保持会话状态
    name = site.replace('.com', '').strip()  # 去除 '.com' 后缀并去除前后空白

    # 登录信息构建
    login_url = f'https://www.{site}/bbwllogin/'  # 登录页面 URL
    login_data = {
        'log': f'Ad{name}min',  # 管理员用户名，假设为 'Ad<站点名>min'
        'pwd': 'f!XsS$J2WneOkMyUgQ',  # 管理员密码
        'wp-submit': 'Log In',  # 提交按钮值
        'redirect_to': f'https://www.{site}/wp-admin/',  # 登录后重定向 URL
        'testcookie': '1'  # 测试 cookie
    }

    # 提交登录请求
    response = session.post(login_url, data=login_data)
    if response.status_code != 200:  # 检查登录是否成功
        print(f'❌ 登录失败: {site}，状态码: {response.status_code}')
        return

    print(f'✅ 登录成功: {site}')  # 登录成功提示
    session.get(f'https://www.{site}/wp-admin/')  # 访问后台首页

    # 获取插件页面
    wp_url = f'https://www.{site}/wp-admin/plugins.php'
    wp_response = session.get(wp_url)
    if wp_response.status_code != 200:  # 如果无法访问插件页面，返回
        print(f'⚠️ 无法访问插件页面: {site}，状态码: {wp_response.status_code}')
        return

    try:
        soup = BeautifulSoup(wp_response.text, 'html.parser')  # 解析 HTML 页面
        activate_url = soup.find('a', {'id': 'activate-wp-rocket'}).get('href')  # 查找 WP Rocket 激活链接
        activate_url = f'https://www.{site}/wp-admin/{activate_url}'  # 完整激活链接
        session.post(activate_url)  # 激活 WP Rocket 插件
        print(f'🚀 已激活 WP Rocket: {site}')  # 激活成功提示
    except:
        print(f'🎉 WP Rocket 已激活: {site}')  # 如果插件已激活，跳过

    # 获取 WP Rocket 设置页面
    setting_url = f'https://www.{site}/wp-admin/options-general.php?page=wprocket'
    st_response = session.get(setting_url)
    Nonce = BeautifulSoup(st_response.text, 'html.parser')  # 解析设置页面

    try:
        # 提取设置页面中的隐藏字段
        wpnonce = Nonce.find('input', {"id": "_wpnonce"}).get('value')
        secret_key = Nonce.find('input', {'id': 'secret_key'}).get('value')
        minify_js_key = Nonce.find('input', {'id': 'minify_js_key'}).get('value')
        consumer_email = Nonce.find('input', {'id': 'consumer_email'}).get('value')
        consumer_key = Nonce.find('input', {'id': 'consumer_key'}).get('value')
        version = Nonce.find('input', {'id': 'version'}).get('value')
        minify_css_key = Nonce.find('input', {'id': 'minify_css_key'}).get('value')
        wplicense = Nonce.find('input', {'id': 'license'}).get('value')
    except:
        print(f'🔒 无法获取设置页面信息: {site}')  # 如果无法获取设置，返回
        return

    # 构造提交设置的表单数据
    setting_data = {
        "option_page": "wprocket",
        "action": "update",
        "_wpnonce": wpnonce,
        "_wp_http_referer": "/wp-admin/options-general.php?page=wprocket",
        "wp_rocket_settings[cache_mobile]": 1,
        "wp_rocket_settings[do_caching_mobile_files]": 1,
        "wp_rocket_settings[purge_cron_interval]": 0,
        "wp_rocket_settings[purge_cron_unit]": "HOUR_IN_SECONDS",
        "wp_rocket_settings[minify_css]": 1,
        "wp_rocket_settings[exclude_css]": "",
        "wp_rocket_settings[optimize_css_delivery]": 1,
        "wp_rocket_settings[remove_unused_css_safelist]": "",
        "wp_rocket_settings[critical_css]": "",
        "wp_rocket_settings[minify_js]": 1,
        "wp_rocket_settings[exclude_inline_js]": "",
        "wp_rocket_settings[exclude_js]": "",
        "wp_rocket_settings[defer_all_js]": 1,
        "wp_rocket_settings[exclude_defer_js]": "",
        "wp_rocket_settings[delay_js]": 1,
        "wp_rocket_settings[delay_js_exclusions]": "",
        "wp_rocket_settings[lazyload]": 1,
        "wp_rocket_settings[exclude_lazyload]": "",
        "wp_rocket_settings[image_dimensions]": 1,
        "wp_rocket_settings[manual_preload]": 1,
        "wp_rocket_settings[preload_excluded_uri]": "",
        "wp_rocket_settings[preload_links]": 1,
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
        "wp_rocket_settings[consumer_key]": consumer_key,
        "wp_rocket_settings[consumer_email]": consumer_email,
        "wp_rocket_settings[secret_key]": secret_key,
        "wp_rocket_settings[license]": wplicense,
        "wp_rocket_settings[secret_cache_key]": "",
        "wp_rocket_settings[minify_css_key]": minify_css_key,
        "wp_rocket_settings[minify_js_key]": minify_js_key,
        "wp_rocket_settings[version]": version,
        "wp_rocket_settings[cloudflare_old_settings]": "",
        "wp_rocket_settings[cache_ssl]": 1,
        "wp_rocket_settings[minify_google_fonts]": 0,
        "wp_rocket_settings[emoji]": 0,
        "wp_rocket_settings[remove_unused_css]": 1,
        "wp_rocket_settings[async_css]": 0,
        "wp_rocket_settings[async_css_mobile]": ""
    }

    # 提交表单，更新设置
    option_url = f'https://www.{site}/wp-admin/options.php'
    st_response = session.post(option_url, data=setting_data)
    if st_response.status_code == 200:  # 检查提交是否成功
        print(f'------------------✅ ✅ ✅成功设置 WP Rocket: {site} ✅ ✅ ✅------------------')
    else:
        print(f'❌ 设置失败: {site}，状态码: {st_response.status_code}')  # 如果设置失败，返回错误信息

# 主程序入口
if __name__ == '__main__':
    print_usage()
    file_path = input("请输入站点域名列表文件路径(可以直接拖入TXT文件)：")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            sites = [line.strip() for line in f if line.strip()]
        for site in sites:
            RocketSetting(site)
    except FileNotFoundError:
        print(f'❌ 未找到文件: {file_path}')
    except Exception as e:
        print(f'❌ 读取文件时出错: {e}')

