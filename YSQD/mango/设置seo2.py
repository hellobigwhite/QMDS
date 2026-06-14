import requests
import time
import re
import pandas as pd
import json
import os
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
DEFAULT_PASSWORD = "f!XsS$J2WneOkMyUgQ"
EXCEL_FILE_PATH = "C:\\Users\\Administrator\\Desktop\\建站域名管理.xlsx"
FAILED_LOG_FILE = "failed_domains_log.json"  # 存储失败域名的日志文件


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


def normalize_url(url):
    normalized_url = url.replace('\\/', '/')
    return normalized_url


def denglu_wpseo(session, domain):
    url = f"https://www.{domain}/wp-admin/admin.php?page=wpseo_page_settings#/homepage"
    try:
        response = session.get(url, timeout=25, verify=False)
    except Exception as e:
        print(f"❌ 请求 {domain} 时发生异常: {e}")
        return None

    if response and response.status_code == 200:
        wpseo_text = str(response.text)

        # 定义正则表达式模式和对应的键名
        patterns = {
            'nonce': r'"endpoint".*?"nonce":"([^"]+)"',
            'index_now_key': r'"index_now_key":"([^"]+)"',
            'version': r'"version":"([^"]+)"',
            'first_activated_on': r'"first_activated_on":([^"]+),',
            'activation_redirect_timestamp_free': r'"activation_redirect_timestamp_free":([^"]+),',
            'website_name': r'"website_name":"([^"]+)"',
            'company_logo': r'"company_logo":"([^"]+)"',
            'company_logo_id': r'"company_logo_id":([^"]+),',  # 注意这里可能需要匹配数字或字符串
            'company_name': r'"company_name":"([^"]+)"',
            'blogdescription': r'"blogdescription":"([^"]+)"}',
        }

        result_dict = {}

        # 遍历模式，进行搜索并存入字典
        for key, pattern in patterns.items():
            match = re.search(pattern, wpseo_text)
            if match:
                result_dict[key] = match.group(1)
            else:
                print(f"⚠️  在响应中未找到 '{key}'")
                return None
        # 检查是否有关键信息缺失
        # 假设 'nonce' 是一个关键字段，没有它就认为失败
        if 'nonce' in result_dict and result_dict['nonce'] is not None:
            result_dict['company_logo'] = normalize_url(result_dict['company_logo'])
            return result_dict
        else:
            print('获取关键参数失败')
            return None

    else:
        print(f"❌ {domain} 登录失败，状态码: {response.status_code if response else 'No Response'}")
        return None


def set_wpseo(session, canshu, domain):
    data = {'option_page': 'wpseo_page_settings',
            '_wp_http_referer': 'admin.php?page=wpseo_page_settings_saved',
            'action': 'update',
            '_wpnonce': canshu['nonce'],
            'wpseo[tracking]': 'false',
            'wpseo[toggled_tracking]': 'true',
            'wpseo[license_server_version]': 'false',
            'wpseo[ms_defaults_set]': 'false',
            'wpseo[ignore_search_engines_discouraged_notice]': 'false',
            'wpseo[indexing_first_time]': 'true',
            'wpseo[indexing_started]': 'false',
            'wpseo[indexing_reason]': 'first_install',
            'wpseo[indexables_indexing_completed]': 'false',
            'wpseo[index_now_key]': canshu['index_now_key'],
            'wpseo[version]': canshu['version'],
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
            'wpseo[first_activated_on]': canshu['first_activated_on'],
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
            'wpseo[activation_redirect_timestamp_free]': canshu['activation_redirect_timestamp_free'],
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
            'wpseo_titles[website_name]': canshu['website_name'],
            'wpseo_titles[person_name]': '',
            'wpseo_titles[person_logo]': '',
            'wpseo_titles[person_logo_id]': '0',
            'wpseo_titles[alternate_website_name]': '',
            'wpseo_titles[company_logo]': canshu['company_logo'],
            'wpseo_titles[company_logo_id]': canshu['company_logo_id'],
            'wpseo_titles[company_name]': canshu['company_name'],
            'wpseo_titles[company_alternate_name]': '',
            'wpseo_titles[company_or_person]': 'company',
            'wpseo_titles[company_or_person_user_id]': 'false',
            'wpseo_titles[stripcategorybase]': 'false',
            'wpseo_titles[open_graph_frontpage_title]': '%%sitename%%',
            'wpseo_titles[open_graph_frontpage_desc]': '',
            'wpseo_titles[open_graph_frontpage_image]': canshu['company_logo'],
            'wpseo_titles[open_graph_frontpage_image_id]': canshu['company_logo_id'],
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
            'blogdescription': canshu['blogdescription']}

    url = f'https://www.{domain}/wp-admin/options.php'
    response = session.post(url, data=data)
    if response.status_code == 200:
        print(f"✅ {domain} seo标题描述配置成功")
        return True
    else:
        print(f"❌ {domain} 配置失败，状态码: {response.status_code}")
        return False


def load_failed_domains():
    """从日志文件加载失败的域名"""
    if os.path.exists(FAILED_LOG_FILE):
        try:
            with open(FAILED_LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            print(f"⚠️ 读取失败日志文件失败，将创建新的日志文件: {FAILED_LOG_FILE}")
            return []
    return []


def save_failed_domains(failed_domains_list):
    """保存失败的域名到日志文件"""
    with open(FAILED_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(failed_domains_list, f, ensure_ascii=False, indent=2)


def remove_from_failed_log(domain_to_remove):
    """从失败日志中移除指定域名"""
    failed_domains = load_failed_domains()
    if domain_to_remove in failed_domains:
        failed_domains.remove(domain_to_remove)
        save_failed_domains(failed_domains)
        print(f"✅ {domain_to_remove} 已从失败日志中移除")


def read_domains_from_excel(file_path):
    """从Excel文件中读取需要设置SEO的域名"""
    try:
        df = pd.read_excel(file_path)

        # 检查必要的列是否存在
        required_columns = ['域名', '是否设置seo2']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            print(f"❌ Excel文件缺少以下列: {missing_columns}")
            return []

        # 筛选"是否设置seo2"为"否"的行
        target_rows = df[df['是否设置seo2'] == '否']

        excel_domains = target_rows['域名'].tolist()
        print(f"📊 从Excel中找到 {len(excel_domains)} 个标记为'否'的域名:")
        for i, domain in enumerate(excel_domains, 1):
            print(f"   {i}. {domain}")

        return excel_domains
    except FileNotFoundError:
        print(f"❌ 找不到文件: {file_path}")
        return []
    except Exception as e:
        print(f"❌ 读取Excel文件时发生错误: {e}")
        return []


def main():
    print("🔍 开始从Excel文件读取需要设置SEO的域名...")

    # 读取Excel中的域名
    excel_domains = read_domains_from_excel(EXCEL_FILE_PATH)

    # 加载之前失败的域名
    failed_domains = load_failed_domains()
    print(f"📊 从失败日志中加载到 {len(failed_domains)} 个之前失败的域名")

    # 合并所有需要处理的唯一域名
    all_unique_domains = list(set(excel_domains + failed_domains))

    if not all_unique_domains:
        print("❌ 没有找到需要处理的域名（Excel中标记为'否'或之前失败的）")
        input("按回车键退出...")
        return

    print(f"\n📋 总共找到 {len(all_unique_domains)} 个需要处理的唯一域名")

    # 显示域名列表供用户确认
    print("\n需要处理的域名列表 (来自Excel和失败日志):")
    for i, domain in enumerate(all_unique_domains, 1):
        source = ""
        if domain in excel_domains and domain in failed_domains:
            source = " (Excel中为'否' & 之前失败)"
        elif domain in excel_domains:
            source = " (Excel中为'否')"
        elif domain in failed_domains:
            source = " (之前失败)"
        print(f"  {i}. {domain}{source}")

    if failed_domains:
        print(f"\n⚠️  发现 {len(failed_domains)} 个之前处理失败的域名:")
        for domain in failed_domains:
            print(f"     - {domain}")
        retry_failed = input(f"\n是否重新处理这些失败的域名？(Y/n): ").strip().lower()
        if retry_failed in ['n', 'no', '否']:
            # 如果用户不重新处理失败的域名，则从总列表中排除它们
            all_unique_domains = [d for d in all_unique_domains if d not in failed_domains]
            print(f"已排除 {len(failed_domains)} 个失败的域名，将继续处理 {len(all_unique_domains)} 个域名")

    if not all_unique_domains:
        print("❌ 没有域名需要处理")
        input("按回车键退出...")
        return

    # 用户确认是否继续处理
    user_input = input(f"\n是否开始处理这 {len(all_unique_domains)} 个域名？(y/N): ").strip().lower()
    if user_input not in ['y', 'yes', '是']:
        print("❌ 用户取消操作")
        input("按回车键退出...")
        return

    print(f"\n🚀 开始处理 {len(all_unique_domains)} 个域名...")

    final_failed_domains = load_failed_domains()  # 初始化为当前失败列表
    success_count = 0

    for i, domain in enumerate(all_unique_domains, 1):
        print(f"\n{'=' * 50}")
        print(f"处理第 {i}/{len(all_unique_domains)} 个域名: {domain}")
        print(f"{'=' * 50}")

        try:
            # 登录网站
            session = login_site(domain)
            if not session:
                print(f"❌ {domain} 登录失败，添加到失败日志")
                if domain not in final_failed_domains:
                    final_failed_domains.append(domain)
                continue

            # 获取WPSEO参数
            canshu = denglu_wpseo(session, domain)
            if not canshu:
                print(f"❌ {domain} 获取WPSEO参数失败，添加到失败日志")
                if domain not in final_failed_domains:
                    final_failed_domains.append(domain)
                continue

            # 设置WPSEO
            success = set_wpseo(session, canshu, domain)
            if success:
                print(f"✅ {domain} 处理成功")
                success_count += 1
                # 如果域名在失败日志中，且本次成功，则将其移除
                if domain in final_failed_domains:
                    remove_from_failed_log(domain)  # 这个函数会自动更新final_failed_domains
                    final_failed_domains = load_failed_domains()  # 重新加载以反映最新状态
            else:
                print(f"❌ {domain} 设置失败，添加到失败日志")
                if domain not in final_failed_domains:
                    final_failed_domains.append(domain)

        except Exception as e:
            print(f"❌ 处理 {domain} 时发生未知错误: {e}")
            if domain not in final_failed_domains:
                final_failed_domains.append(domain)

    # 保存最终的失败域名列表
    save_failed_domains(final_failed_domains)

    # 统计结果
    print(f"\n{'=' * 60}")
    print("🎯 处理完成！统计结果:")
    print(f"   总共处理: {len(all_unique_domains)} 个域名")
    print(f"   成功: {success_count} 个域名")
    print(f"   失败: {len(final_failed_domains)} 个域名")
    print(f"{'=' * 60}")

    if success_count > 0:
        print(f"\n✅ 成功处理了 {success_count} 个域名")

    if final_failed_domains:
        print(
            f"\n❌ 仍有 {len(final_failed_domains)} 个域名处理失败，已记录到日志文件 '{FAILED_LOG_FILE}'，下次运行时可重新处理:")
        for domain in final_failed_domains:
            print(f"   - {domain}")
    else:
        print(f"\n✅ 所有域名均已成功处理，失败日志文件 '{FAILED_LOG_FILE}' 已清空或不存在。")

    print(f"\n🎉 程序执行完毕！")
    input("按回车键退出...")


if __name__ == "__main__":
    main()