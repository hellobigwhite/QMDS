import requests


class DomainReporter:
    def __init__(self, base_url, username, password):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = None
        self._session = requests.Session()

    def _auth_headers(self):
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _login(self):
        resp = self._session.post(
            f"{self._base_url}/login",
            data={
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError("登录响应中未找到 token")
        self._token = token

    def _request(self, method, path, retry_on_401=True, **kwargs):
        if not self._token:
            self._login()
        extra_headers = kwargs.pop("headers", {})
        headers = self._auth_headers()
        headers.update(extra_headers)
        url = f"{self._base_url}{path}"
        resp = self._session.request(method, url, headers=headers, timeout=15, **kwargs)
        if retry_on_401 and resp.status_code == 401:
            self._login()
            headers = self._auth_headers()
            headers.update(extra_headers)
            resp = self._session.request(method, url, headers=headers, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp

    def submit_domain(self, payload):
        return self._request("POST", "/system/domainmanage", json=payload).json()

    def fetch_domain_info(self, name):
        resp = self._request(
            "GET",
            "/system/domainmanage/list",
            params={"pageNum": 1, "pageSize": 10, "name": name},
        )
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
        resp = self._request(
            "GET",
            "/system/domainmanage/list",
            params={
                "pageNum": 1,
                "pageSize": 10,
                "beginCreateTime": date_text,
                "endCreateTime": date_text,
            },
        )
        body = resp.json()
        if isinstance(body, dict):
            if isinstance(body.get("rows"), list):
                return body["rows"]
            if isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                return body["data"]["records"]
            if isinstance(body.get("data"), list):
                return body["data"]
        return []

    def delete_domain(self, domain_id):
        resp = self._request("DELETE", f"/system/domainmanage/{domain_id}")
        return resp.json() if resp.content else {}
