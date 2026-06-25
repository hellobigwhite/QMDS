import requests

from qmds.utils.logger import get_logger

log = get_logger("domain_reporter")

DOMAIN_STATUS_LABELS = {
    0: "待解析",
    1: "待配置",
    2: "已解析",
    3: "已解析",
    4: "已建站",
    "0": "待解析",
    "1": "待配置",
    "2": "已解析",
    "3": "已解析",
    "4": "已建站",
}

REPORT_API_BASE_URL = "http://123.60.135.93:8099"


class DomainReporter:
    def __init__(self, base_url, username, password):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = None

    def _login(self):
        url = f"{self._base_url}/login"
        data = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError("登录响应中未找到 token")
        self._token = token

    def submit_domain(self, payload):
        if not self._token:
            self._login()
        url = f"{self._base_url}/system/domainmanage"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                self._login()
                resp = requests.post(url, json=payload, headers=headers, timeout=15)
                resp.raise_for_status()
                return resp.json()
            raise

    def fetch_domain_info(self, name):
        if not self._token:
            self._login()
        url = f"{self._base_url}/system/domainmanage/list"
        params = {"pageNum": 1, "pageSize": 10, "name": name}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        record = None
        if isinstance(body, dict):
            if isinstance(body.get("rows"), list) and body["rows"]:
                record = body["rows"][0]
            elif isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                records = body["data"]["records"]
                record = records[0] if records else None
            elif isinstance(body.get("data"), list) and body["data"]:
                record = body["data"][0]
        if not isinstance(record, dict):
            raise RuntimeError("未找到域名记录")
        return {"id": record.get("id"), "status": record.get("status")}

    def fetch_domains_by_date(self, date_text):
        if not self._token:
            self._login()
        url = f"{self._base_url}/system/domainmanage/list"
        params = {
            "pageNum": 1,
            "pageSize": 10,
            "beginCreateTime": date_text,
            "endCreateTime": date_text,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        records = []
        if isinstance(body, dict):
            if isinstance(body.get("rows"), list):
                records = body["rows"]
            elif isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                records = body["data"]["records"]
            elif isinstance(body.get("data"), list):
                records = body["data"]
        if not isinstance(records, list):
            records = []
        return records

    def fetch_all_domains(self, page_size=100):
        """获取所有域名列表（支持分页）"""
        if not self._token:
            self._login()
        all_records = []
        page_num = 1
        while True:
            url = f"{self._base_url}/system/domainmanage/list"
            params = {"pageNum": page_num, "pageSize": page_size}
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            records = []
            total = 0
            if isinstance(body, dict):
                if isinstance(body.get("rows"), list):
                    records = body["rows"]
                    total = body.get("total", 0)
                elif isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                    records = body["data"]["records"]
                    total = body["data"].get("total", 0)
                elif isinstance(body.get("data"), list):
                    records = body["data"]
            if not records:
                break
            # 打印第一条记录的所有字段和值，用于调试
            if page_num == 1 and records:
                first = records[0]
                log.info(f"上报平台返回字段: {list(first.keys())}")
                for k, v in first.items():
                    log.info(f"  {k}: {v}")
            all_records.extend(records)
            if len(all_records) >= total or len(records) < page_size:
                break
            page_num += 1
        return all_records

    def delete_domain(self, domain_id):
        if not self._token:
            self._login()
        url = f"{self._base_url}/system/domainmanage/{domain_id}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        resp = requests.delete(url, headers=headers, timeout=15)
        if resp.status_code == 401:
            self._login()
            resp = requests.delete(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def fetch_categories(self):
        """获取类目列表，返回 {id: name} 的映射"""
        if not self._token:
            self._login()
        url = f"{self._base_url}/system/domainmanage/categories"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            records = []
            if isinstance(body, dict):
                if isinstance(body.get("rows"), list):
                    records = body["rows"]
                elif isinstance(body.get("data"), list):
                    records = body["data"]
                elif isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                    records = body["data"]["records"]
            result = {}
            for r in records:
                if isinstance(r, dict):
                    cid = r.get("id") or r.get("categoryId")
                    name = r.get("name") or r.get("categoryName") or r.get("label")
                    if cid and name:
                        result[str(cid)] = str(name)
            return result
        except Exception as e:
            log.warning(f"获取类目列表失败: {e}")
            return {}
