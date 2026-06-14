import json
import re

import requests
from bs4 import BeautifulSoup


class WpPluginConfigurator:
    def __init__(self, password):
        self._password = password
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            }
        )

    def _request(self, method, url, **kwargs):
        for _ in range(3):
            try:
                resp = self._session.request(method, url, timeout=60, verify=False, **kwargs)
                if resp is not None and resp.status_code in (200, 201, 302):
                    return resp
            except requests.exceptions.RequestException:
                pass
        return None

    def _login(self, site):
        login_url = f"https://www.{site}/bbwllogin/"
        user = site.replace(".com", "").strip()
        data = {
            "log": f"Ad{user}min",
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"https://www.{site}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data, allow_redirects=True)
        if resp is None:
            raise RuntimeError("WP 登录请求失败")
        if any("wordpress_logged_in" in c.name for c in self._session.cookies):
            return True
        admin_check = self._request("GET", f"https://www.{site}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            return True
        raise RuntimeError("WP 登录失败")

    def _find_activate_link(self, soup, link_id, plugin_file):
        a = soup.find("a", id=link_id)
        if a and a.get("href"):
            return a.get("href")
        if plugin_file:
            encoded = requests.utils.quote(plugin_file)
            a = soup.select_one(
                f'a[href*="action=activate"][href*="plugin="][href*="{encoded}"]'
            )
            if a and a.get("href"):
                return a.get("href")
            plugin_dir = plugin_file.split("/")[0]
            a = soup.select_one(f'a[href*="action=activate"][href*="{plugin_dir}"]')
            if a and a.get("href"):
                return a.get("href")
        return None

    def _activate_plugin(self, site, link_id, plugin_file):
        url = f"https://www.{site}/wp-admin/plugins.php"
        resp = self._request("GET", url)
        if not resp:
            raise RuntimeError("无法访问 plugins.php")
        soup = BeautifulSoup(resp.text, "html.parser")
        href = self._find_activate_link(soup, link_id, plugin_file)
        if not href:
            return True
        activate_url = (
            href if href.startswith("http") else f"https://www.{site}/wp-admin/{href.lstrip('/')}"
        )
        res = self._request("GET", activate_url)
        return res is not None

    def _get_input_value(self, doc, input_id, required=False):
        node = doc.find("input", {"id": input_id})
        value = node.get("value", "") if node else ""
        if required and not value:
            raise RuntimeError(f"未获取到字段: {input_id}")
        return value

    def _get_yoast_nonce(self, site):
        url = f"https://www.{site}/wp-admin/admin.php?page=wpseo_dashboard#/first-time-configuration"
        resp = self._request("GET", url)
        if not resp:
            raise RuntimeError("无法访问 Yoast 配置页")
        html = resp.text
        doc = BeautifulSoup(html, "html.parser")
        for script in doc.find_all("script"):
            text = script.string or ""
            if "wpApiSettings" not in text:
                continue
            match = re.search(r'nonce"\s*:\s*"([a-zA-Z0-9\-_]+)"', text)
            if match:
                return match.group(1)
        match = re.search(r'nonce"\s*:\s*"([a-zA-Z0-9\-_]+)"', html)
        if match:
            return match.group(1)
        raise RuntimeError("未找到 Yoast nonce")

    def _get_logo_id(self, site, nonce):
        url = f"https://www.{site}/wp-json/wp/v2/media?search=logo.png"
        headers = {"X-WP-Nonce": nonce}
        resp = self._request("GET", url, headers=headers)
        if not resp:
            return 0, ""
        try:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    if "logo.png" in str(item.get("source_url", "")).lower():
                        return item.get("id", 0), item.get("source_url", "")
        except Exception:
            pass
        return 0, ""

    def _post_yoast(self, site, path, nonce, payload):
        url = f"https://www.{site}{path}"
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}_wpnonce={requests.utils.quote(nonce)}"
        headers = {
            "Content-Type": "application/json",
            "X-WP-Nonce": nonce,
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = self._request("POST", url, headers=headers, data=json.dumps(payload))
        if not resp or resp.status_code >= 500:
            print(
                f"Yoast API 服务器错误: {path} - HTTP {resp.status_code if resp else '无响应'}"
            )
            return
        if resp.status_code >= 400:
            raise RuntimeError(f"Yoast API 失败: {path}")

    def _update_yoast_settings(self, site, nonce, logo_id, logo_url):
        settings_url = f"https://www.{site}/wp-admin/admin.php?page=wpseo_page_settings"
        resp = self._request("GET", settings_url)
        if not resp:
            raise RuntimeError("无法访问 Yoast 设置页")
        html = resp.text
        script_match = re.search(
            r'<script id="yoast-seo-new-settings-js-extra">([\s\S]*?)</script>',
            html,
        )
        if not script_match:
            raise RuntimeError("未找到 Yoast settings 脚本")
        script_content = script_match.group(1)
        data_match = re.search(r"var\s+wpseoScriptData\s*=\s*({[\s\S]*?});", script_content)
        if not data_match:
            raise RuntimeError("未找到 wpseoScriptData")
        json_str = data_match.group(1).strip()
        json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
        json_str = re.sub(r"^\s*,", "", json_str)
        json_str = json_str.replace("'", '"')
        settings_data = json.loads(json_str)
        settings_obj = settings_data.get("settings", {})
        if not settings_obj:
            raise RuntimeError("settings 为空")

        settings_nonce = settings_obj.get("nonce") or settings_obj.get("wpnonce")
        if not settings_nonce:
            match = re.search(r'"nonce"\s*:\s*"([a-zA-Z0-9\-_]+)"', script_content)
            if match:
                settings_nonce = match.group(1)
        if not settings_nonce:
            raise RuntimeError("未找到 Yoast settings nonce")

        settings_obj.setdefault("wpseo_titles", {})
        settings_obj["wpseo_titles"]["title-home-wpseo"] = "%%sitename%%"
        settings_obj["wpseo_titles"]["metadesc-home-wpseo"] = "%%sitedesc%%"
        settings_obj["wpseo_titles"]["open_graph_frontpage_image"] = (
            logo_url or f"https://www.{site}/wp-content/uploads/logo.png"
        )
        settings_obj["wpseo_titles"]["open_graph_frontpage_image_id"] = logo_id or 0

        settings_obj.setdefault("wpseo_social", {})
        settings_obj["wpseo_social"]["og_default_image"] = (
            logo_url or f"https://www.{site}/wp-content/uploads/logo.png"
        )
        settings_obj["wpseo_social"]["og_default_image_id"] = logo_id or 0

        form_parts = []

        def flatten(prefix, obj):
            if isinstance(obj, dict):
                for key, val in obj.items():
                    flatten(f"{prefix}[{key}]", val)
            else:
                form_parts.append(
                    f"{requests.utils.quote(prefix)}={requests.utils.quote(str(obj))}"
                )

        for key, val in settings_obj.items():
            flatten(key, val)

        body = (
            "option_page=wpseo_page_settings&action=update"
            f"&_wpnonce={requests.utils.quote(settings_nonce)}"
            f"&_wp_http_referer={requests.utils.quote('/wp-admin/admin.php?page=wpseo_page_settings')}"
            f"&{'&'.join(form_parts)}"
        )
        save_url = f"https://www.{site}/wp-admin/options.php"
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        try:
            resp2 = self._session.post(
                save_url,
                headers=headers,
                data=body,
                timeout=90,
                verify=False,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            print(f"保存 Yoast settings 网络异常: {exc}")
            return

        if resp2.status_code >= 500:
            print(f"保存 Yoast settings 服务器错误: HTTP {resp2.status_code} {resp2.text[:200]}")
            return
        if resp2.status_code >= 400:
            raise RuntimeError(
                f"保存 Yoast settings 失败: HTTP {resp2.status_code} {resp2.text[:200]}"
            )

    def _configure_yoast(self, site):
        self._activate_plugin(site, "activate-wordpress-seo", "wordpress-seo/wp-seo.php")
        self._activate_plugin(
            site,
            "activate-yoast-seo-premium",
            "yoast-seo-premium/yoast-seo-premium.php",
        )

        nonce = self._get_yoast_nonce(site)
        logo_id, logo_url = self._get_logo_id(site, nonce)

        self._post_yoast(
            site,
            "/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user",
            nonce,
            {"finishedSteps": ["optimizeSeoData"]},
        )
        self._post_yoast(
            site,
            "/wp-json/yoast/v1/configuration/site_representation?_locale=user",
            nonce,
            {
                "company_or_person": "company",
                "company_name": site,
                "company_logo": logo_url or f"https://www.{site}/wp-content/uploads/logo.png",
                "company_logo_id": logo_id or 0,
                "person_logo": "",
                "person_logo_id": 0,
                "website_name": site,
            },
        )
        self._post_yoast(
            site,
            "/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user",
            nonce,
            {
                "finishedSteps": [
                    "optimizeSeoData",
                    "siteRepresentation",
                    "socialProfiles",
                    "personalPreferences",
                ]
            },
        )
        self._post_yoast(
            site,
            "/wp-json/yoast/v1/configuration/social_profiles?_locale=user",
            nonce,
            {"facebook_site": "", "twitter_site": "", "other_social_urls": []},
        )
        self._post_yoast(
            site,
            "/wp-json/yoast/v1/configuration/enable_tracking?_locale=user",
            nonce,
            {"tracking": 0},
        )
        self._update_yoast_settings(site, nonce, logo_id, logo_url)

    def _configure_rocket(self, site):
        self._request("GET", f"https://www.{site}/wp-admin/")

        plugins_url = f"https://www.{site}/wp-admin/plugins.php"
        wp_response = self._request("GET", plugins_url)
        if not wp_response:
            raise RuntimeError("无法访问插件页面")

        plugin_doc = BeautifulSoup(wp_response.text, "html.parser")
        activate_href = self._find_activate_link(
            plugin_doc,
            "activate-wp-rocket",
            "wp-rocket/wp-rocket.php",
        )

        if activate_href:
            activate_url = (
                activate_href
                if activate_href.startswith("http")
                else f"https://www.{site}/wp-admin/{activate_href.lstrip('/')}"
            )
            activate_resp = self._request("POST", activate_url, allow_redirects=True)
            if not activate_resp:
                activate_resp = self._request("GET", activate_url, allow_redirects=True)
            if not activate_resp:
                raise RuntimeError("WP Rocket 激活请求失败")

        setting_url = f"https://www.{site}/wp-admin/options-general.php?page=wprocket"
        st_response = self._request("GET", setting_url)
        if not st_response:
            raise RuntimeError("无法访问 WP Rocket 设置页")

        doc = BeautifulSoup(st_response.text, "html.parser")
        wpnonce = self._get_input_value(doc, "_wpnonce", required=True)
        secret_key = self._get_input_value(doc, "secret_key", required=True)
        minify_js_key = self._get_input_value(doc, "minify_js_key", required=True)
        consumer_email = self._get_input_value(doc, "consumer_email", required=True)
        consumer_key = self._get_input_value(doc, "consumer_key", required=True)
        version = self._get_input_value(doc, "version", required=True)
        minify_css_key = self._get_input_value(doc, "minify_css_key", required=True)
        wplicense = self._get_input_value(doc, "license", required=True)

        data = {
            "option_page": "wprocket",
            "action": "update",
            "_wpnonce": wpnonce,
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
            "wp_rocket_settings[consumer_key]": consumer_key,
            "wp_rocket_settings[consumer_email]": consumer_email,
            "wp_rocket_settings[secret_key]": secret_key,
            "wp_rocket_settings[license]": "",
            "wp_rocket_settings[secret_cache_key]": "",
            "wp_rocket_settings[minify_css_key]": minify_css_key,
            "wp_rocket_settings[minify_js_key]": minify_js_key,
            "wp_rocket_settings[version]": version,
            "wp_rocket_settings[cloudflare_old_settings]": "",
            "wp_rocket_settings[cache_ssl]": "1",
            "wp_rocket_settings[minify_google_fonts]": "0",
            "wp_rocket_settings[emoji]": "0",
            "wp_rocket_settings[remove_unused_css]": "1",
            "wp_rocket_settings[async_css]": "0",
            "wp_rocket_settings[async_css_mobile]": "",
        }

        option_url = f"https://www.{site}/wp-admin/options.php"
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        save_resp = self._request("POST", option_url, headers=headers, data=data)
        if not save_resp or save_resp.status_code >= 400:
            raise RuntimeError("WP Rocket 配置提交失败")

    def configure_all(self, site):
        self._login(site)
        self._configure_yoast(site)
        self._configure_rocket(site)
        return True
