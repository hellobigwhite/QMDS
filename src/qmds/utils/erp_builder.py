"""ERP建站工具 - 通过ERP系统创建站点"""

import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from qmds.utils.logger import get_logger

log = get_logger("erp_builder")

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
            log.info(f"ERP登录成功 (cookies: {len(self._session.cookies)} 个)")
            return {"success": True, "message": "登录成功"}
        msg = f"登录失败 (HTTP {resp.status_code}): {resp.text[:200]}"
        log.error(f"ERP {msg}")
        return {"success": False, "message": msg}

    def _get_jx(self, domain_name):
        url = f"https://erp.yswl.site/index.php?main_page=site&p=wh&sitename={domain_name}&ip="
        resp = self._session.get(url, timeout=180)
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
        resp = self._session.get(ADD_PAGE_URL, timeout=180)
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

        hidden_fields = {}
        form_action = ADD_SITE_URL
        form = soup.find("form")
        if form:
            action = form.get("action", "").strip()
            if action:
                form_action = action
                if not action.startswith("http"):
                    form_action = urljoin(ADD_PAGE_URL, action)
            for hidden in form.find_all("input", type="hidden"):
                name = hidden.get("name")
                value = hidden.get("value", "")
                if name:
                    hidden_fields[name] = value
            if hidden_fields:
                log.info(f"表单隐藏字段: {list(hidden_fields.keys())}")

        log.info(f"表单action: {form_action}")
        return {
            "site_fwq_id": server_value,
            "site_db_id": template_value,
            "site_admin_id": admin_value,
            "store_pf": store_pf_value or "/www/wwwroot/",
            "hidden_fields": hidden_fields,
            "form_action": form_action,
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

    def _build_form_data(self, domain, server, template, store_pf, title, description,
                         address, category):
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
        form_data.update(ids["hidden_fields"])
        return form_data, ids

    def _post_site(self, form_data, domain, action_url=None):
        target_url = action_url or ADD_SITE_URL
        self._session.headers.update({"Referer": ADD_PAGE_URL})
        log.info(f"[{domain}] 提交URL: {target_url}")
        log.info(f"[{domain}] POST字段: {list(form_data.keys())}")
        resp = self._session.post(target_url, data=form_data, timeout=180)
        try:
            body = resp.json()
        except Exception:
            raw = resp.text
            if raw.strip() == "":
                body = {"raw": raw, "status_code": resp.status_code,
                        "error": "ERP 返回了空响应，可能域名已建站或会话已过期",
                        "headers": dict(resp.headers)}
            else:
                body = {"raw": raw, "status_code": resp.status_code,
                        "error": f"ERP 返回非 JSON (HTTP {resp.status_code})",
                        "headers": dict(resp.headers)}
        return body

    def _is_empty_erp_response(self, body):
        return body.get("status_code") == 200 and not body.get("raw", "").strip()

    def build_site(self, domain, server, template, title, description, address, category, store_pf="",
                   progress_callback=None):
        try:
            if progress_callback:
                progress_callback("获取域名解析信息...")
            log.info(f"[{domain}] 获取解析信息...")
            cf_data = self._get_jx(domain)

            if progress_callback:
                progress_callback("获取建站参数...")
            log.info(f"[{domain}] 获取建站参数...")
            ids = self._get_form_ids(server, template, store_pf)

            if progress_callback:
                progress_callback("上传Logo...")
            log.info(f"[{domain}] 上传Logo...")
            logo_file = self._upload_logo(domain)

            if progress_callback:
                progress_callback("解析地址...")
            log.info(f"[{domain}] 解析地址...")
            addr_info = self._parse_us_address(address)

            if progress_callback:
                progress_callback("提交建站表单...")
            log.info(f"[{domain}] 提交建站表单...")

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
            form_data.update(ids["hidden_fields"])
            body = self._post_site(form_data, domain, ids.get("form_action"))
            log.info(f"[{domain}] ERP响应: {body}")
            if body.get("code") == 0:
                log.info(f"[{domain}] 建站成功")
                return {"success": True, "message": "建站成功", "data": body}
            if self._is_empty_erp_response(body):
                log.warning(f"[{domain}] ERP返回空响应 (该域名可能已在ERP中建站)")
                return {"success": False, "message": "域名已在ERP中建站，无需重复提交", "data": body}
            err_msg = body.get("error") or str(body)
            log.error(f"[{domain}] 建站失败: {err_msg}")
            return {"success": False, "message": err_msg, "data": body}

        except Exception as e:
            log.error(f"[{domain}] 建站异常: {e}")
            return {"success": False, "message": str(e)}

    def close(self):
        self._session.close()


_erp_builder = None


def get_erp_builder(username, password,
                    image_root="media"):
    global _erp_builder
    if _erp_builder is None:
        _erp_builder = ERPBuilder(username, password, image_root)
    elif (_erp_builder._username != username or
          _erp_builder._password != password or
          _erp_builder._image_root != image_root):
        _erp_builder.close()
        _erp_builder = ERPBuilder(username, password, image_root)
    return _erp_builder
