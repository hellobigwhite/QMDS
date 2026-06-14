import random
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class WpCachePurger:
    def __init__(self, password, max_retries=3):
        self._password = password
        self._max_retries = max(1, int(max_retries or 1))
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def _request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault(
            "X-Forwarded-For",
            ".".join(str(random.randint(1, 255)) for _ in range(4)),
        )
        last_error = None
        for _ in range(self._max_retries):
            try:
                resp = self._session.request(
                    method,
                    url,
                    timeout=30,
                    verify=False,
                    allow_redirects=True,
                    headers=headers,
                    **kwargs,
                )
                if resp is not None and resp.status_code in (200, 201, 302):
                    return resp
            except requests.exceptions.RequestException as exc:
                last_error = exc
        if last_error:
            raise RuntimeError(str(last_error)) from last_error
        return None

    def _login(self, site):
        base_url = f"https://www.{site}"
        login_url = f"{base_url}/bbwllogin/"
        username = f"Ad{site.replace('.com', '').replace('.shop', '').strip()}min"
        data = {
            "log": username,
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"{base_url}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data)
        if resp is None:
            raise RuntimeError("WP 登录请求失败")
        if any("wordpress_logged_in" in cookie.name for cookie in self._session.cookies):
            return base_url
        admin_check = self._request("GET", f"{base_url}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            return base_url
        raise RuntimeError("WP 登录失败")

    def _extract_purge_links(self, admin_html, base_url):
        soup = BeautifulSoup(admin_html, "html.parser")
        links = []

        target = soup.find(id="wp-admin-bar-purge-all")
        if target:
            if target.name == "a" and target.get("href"):
                links.append(target["href"])
            for anchor in target.find_all("a", href=True):
                links.append(anchor["href"])

        if not links:
            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href", "")
                text = anchor.get_text(" ", strip=True).lower()
                href_lower = href.lower()
                if "purge" in href_lower or "rocket_clean_domain" in href_lower:
                    links.append(href)
                    continue
                if "cache" in text and ("purge" in text or "clear" in text):
                    links.append(href)

        normalized = []
        seen = set()
        for href in links:
            full_url = urljoin(f"{base_url}/wp-admin/", href)
            if full_url in seen:
                continue
            seen.add(full_url)
            normalized.append(full_url)
        return normalized

    def purge(self, site):
        base_url = self._login(site)
        admin_resp = self._request("GET", f"{base_url}/wp-admin/")
        if admin_resp is None or admin_resp.status_code != 200:
            raise RuntimeError("登录后无法访问 wp-admin")

        purge_links = self._extract_purge_links(admin_resp.text, base_url)
        if not purge_links:
            raise RuntimeError("未找到清理缓存按钮")

        for link in purge_links:
            resp = self._request("GET", link)
            if resp is None or resp.status_code not in (200, 302):
                raise RuntimeError(f"清理缓存失败: {link}")
        return True

