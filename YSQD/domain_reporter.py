import requests


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
