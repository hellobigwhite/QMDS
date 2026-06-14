import os
import re

import requests
from bs4 import BeautifulSoup


LOGIN_URL = "https://erp.yswl.site/index.php?main_page=login&dongzuo=denglu"
ADD_SITE_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=addsite"
UPLOAD_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=uplogo"
ADD_PAGE_URL = "https://erp.yswl.site/index.php?main_page=site&p=addsite_d"


class ERPBuilder:
    def __init__(self, username, password, image_root="media", admin_id=None):
        self._username = username
        self._password = password
        self._image_root = image_root
        self._admin_id = admin_id
        self._session = requests.Session()
        self._session.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "Referer": "https://erp.yswl.site/index.php/",
        }

    def login(self):
        data = {"username": self._username, "password": self._password}
        resp = self._session.post(LOGIN_URL, data=data, timeout=60)
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code == 200 and body.get("code") == 0:
            return True
        raise RuntimeError(f"ERP 登录失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    def _get_jx(self, domain_name):
        url = f"https://erp.yswl.site/index.php?main_page=site&p=wh&sitename={domain_name}&ip="
        resp = self._session.get(url, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        domain_td = soup.find("td", string=domain_name)
        if not domain_td:
            raise RuntimeError("未找到解析信息")
        row = domain_td.find_parent("tr")
        cells = row.find_all("td")
        if len(cells) < 6:
            raise RuntimeError("解析信息不完整")
        cf_data = {
            "cfacc": cells[3].get_text(strip=True),
            "cfkey": cells[4].get_text(strip=True),
            "ip": cells[5].get_text(strip=True),
        }
        if not all(cf_data.values()):
            raise RuntimeError("解析信息不完整")
        return cf_data

    def _get_select_value(self, soup, select_name, match_text):
        select_tag = soup.find("select", {"name": select_name})
        if not select_tag:
            return None
        for option in select_tag.find_all("option"):
            if match_text in option.get_text(strip=True):
                return option.get("value", "").strip()
        return None

    def _get_form_ids(self, server_input, template_input, store_pf_input=""):
        resp = self._session.get(ADD_PAGE_URL, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        server_value = self._get_select_value(soup, "site_fwq_id", server_input)
        template_value = self._get_select_value(soup, "site_db_id", template_input)
        store_pf_value = self._get_select_value(soup, "store_pf", store_pf_input)
        admin = soup.find("input", {"name": "site_admin_id"})
        admin_value = admin.get("value", "").strip() if admin else None
        if not admin_value and self._admin_id:
            admin_value = str(self._admin_id).strip()
        if not server_value or not template_value or not admin_value:
            raise RuntimeError("建站参数获取失败")
        return {
            "site_fwq_id": server_value,
            "site_db_id": template_value,
            "site_admin_id": admin_value,
            "store_pf": store_pf_value or "/www/wwwroot/",
        }

    def _upload_logo(self, domain_name):
        logo_path = os.path.join(self._image_root, domain_name, "logo.png")
        if not os.path.exists(logo_path):
            raise RuntimeError("logo.png 未找到")
        with open(logo_path, "rb") as f:
            files = {"file": f}
            data = {"model": domain_name}
            resp = self._session.post(UPLOAD_URL, files=files, data=data, timeout=60)
        body = resp.json()
        if body.get("code") == 0 and body.get("msg") == "ok":
            return body.get("file")
        raise RuntimeError("logo 上传失败")

    def _parse_us_address(self, address):
        raw = address
        address = re.sub(r"[^a-zA-Z0-9,\s-]", "", address).strip()
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if len(parts) >= 3:
            street = ", ".join(parts[:-2])
            city = parts[-2]
            state_zip = parts[-1]
        elif len(parts) == 2:
            street, state_zip = parts
            city = ""
        else:
            raise RuntimeError(f"地址格式应为：Street, City, ST 12345（当前：{raw}）")

        match = re.match(r"^([A-Za-z]{2})\s*(\d{5})(?:-\d{4})?$", state_zip.strip())
        if match:
            state = match.group(1).upper()
            zipcode = match.group(2)
        else:
            match = re.match(r"^([A-Za-z]{2})\s*$", state_zip.strip())
            if not match:
                raise RuntimeError(f"地址格式应为：Street, City, ST 12345（当前：{raw}）")
            state = match.group(1).upper()
            zipcode = "00000"
        return {
            "store_code": zipcode,
            "store_state": f"US:{state}",
            "store_city": city.strip(),
            "store_address": street.strip(),
        }

    def build_site(self, domain, server, template, title, description, address, category, store_pf=""):
        cf_data = self._get_jx(domain)
        ids = self._get_form_ids(server, template, store_pf)
        logo_file = self._upload_logo(domain)
        addr_info = self._parse_us_address(address)

        form_data = {
            "site_name": domain.strip(),
            "cfacc": cf_data["cfacc"].strip(),
            "cfkey": cf_data["cfkey"].strip(),
            "site_fwq_id": str(ids["site_fwq_id"]).strip(),
            "site_db_id": str(ids["site_db_id"]).strip(),
            "site_title": title.strip(),
            "site_dec": description.strip(),
            "store_adress": addr_info["store_address"].strip(),
            "store_city": addr_info["store_city"].strip(),
            "store_code": addr_info["store_code"].strip(),
            "store_state": addr_info["store_state"].strip(),
            "file": "",
            "imgs[0]": logo_file.strip(),
            "site_beizhu": category.strip(),
            "site_admin_id": str(ids["site_admin_id"]).strip(),
            "store_pf": str(ids["store_pf"]).strip(),
        }
        resp = self._session.post(ADD_SITE_URL, data=form_data, timeout=60)
        try:
            body = resp.json()
        except Exception:
            raw = resp.text
            if raw.strip() == "":
                body = {"raw": raw, "status_code": resp.status_code, "error": "ERP 返回了空响应，可能域名已建站或会话已过期"}
            else:
                body = {"raw": raw, "status_code": resp.status_code, "error": f"ERP 返回非 JSON (HTTP {resp.status_code})"}
        if body.get("code") == 0:
            return True, body
        if body.get("status_code") == 200 and not body.get("raw", "").strip():
            self.login()
            resp = self._session.post(ADD_SITE_URL, data=form_data, timeout=60)
            try:
                body = resp.json()
            except Exception:
                raw = resp.text
                body = {"raw": raw, "status_code": resp.status_code, "error": "重试后 ERP 仍返回空响应，请检查 ERP 状态"}
            if body.get("code") == 0:
                return True, body
        return False, body
