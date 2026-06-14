import requests
import pandas as pd
import json  # 新增，用于安全解析


class DomainReporter:
    def __init__(self, base_url, username, password):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = None

    def _login(self):
        url = f"{self._base_url}/login"
        params = {
            "username": self._username,
            "password": self._password
        }
        resp = requests.get(url, params=params, timeout=15)

        # ---------- 关键修复：打印原始响应，便于调试 ----------
        print(f"[Login] Status: {resp.status_code} | Content-Type: {resp.headers.get('Content-Type')}")
        print(f"[Login] Body: {resp.text[:500]}...")   # 只打印前500字符

        resp.raise_for_status()

        try:
            body = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"登录接口返回非JSON: {resp.text[:300]}")

        token = body.get("token") or body.get("access_token")
        if not token:
            raise RuntimeError(f"登录失败，未获取到token。返回内容: {body}")
        self._token = token
        print(f"✅ 登录成功，Token 已获取")

    def _make_request(self, method, url, **kwargs):
        """统一请求封装，自动处理 401 重登录 + 非JSON错误"""
        if not self._token:
            self._login()

        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self._token}")
        headers.setdefault("Content-Type", "application/json")

        resp = method(url, headers=headers, timeout=15, **kwargs)

        # ---------- 关键修复：详细错误信息 ----------
        if resp.status_code != 200:
            print(f"[DEBUG] {method.__name__.upper()} {url} → {resp.status_code}")
            print(f"[DEBUG] Response: {resp.text[:800]}")

        if resp.status_code == 401:
            print("⚠️ Token 过期，重新登录...")
            self._login()
            # 重新发起请求（headers 会重新带上新 token）
            resp = method(url, headers=headers, timeout=15, **kwargs)

        resp.raise_for_status()

        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"接口返回非JSON格式 (status={resp.status_code})\n"
                f"URL: {url}\n"
                f"Content-Type: {resp.headers.get('Content-Type')}\n"
                f"Body: {resp.text[:600]}"
            ) from e

    def submit_domain(self, payload):
        url = f"{self._base_url}/system/domainmanage"
        return self._make_request(requests.post, url, json=payload)

    def fetch_domain_info(self, name):
        url = f"{self._base_url}/system/domainmanage/list"
        params = {"pageNum": 1, "pageSize": 10, "name": name.strip()}

        body = self._make_request(requests.get, url, params=params)

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
            return {"id": None, "status": "未找到"}

        return {
            "id": record.get("id"),
            "status": record.get("status", "未知")
        }

    def fetch_domains_by_date(self, date_text):
        url = f"{self._base_url}/system/domainmanage/list"
        params = {
            "pageNum": 1,
            "pageSize": 10,
            "beginCreateTime": date_text,
            "endCreateTime": date_text,
        }
        body = self._make_request(requests.get, url, params=params)

        if isinstance(body, dict):
            if isinstance(body.get("rows"), list):
                return body["rows"]
            elif isinstance(body.get("data"), dict) and isinstance(body["data"].get("records"), list):
                return body["data"]["records"]
            elif isinstance(body.get("data"), list):
                return body["data"]
        return []

    def delete_domain(self, domain_id):
        url = f"{self._base_url}/system/domainmanage/{domain_id}"
        return self._make_request(requests.delete, url)

    def process_excel(self, excel_path, output_path=None):
        if not output_path:
            output_path = excel_path.replace(".xlsx", "_结果.xlsx")

        df = pd.read_excel(excel_path, sheet_name="Sheet1")
        required_cols = ["大类", "模板", "地址", "服务器", "域名", "标题", "描述"]

        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Excel中缺少列：{col}")

        if "状态" not in df.columns:
            df["状态"] = ""

        for idx, row in df.iterrows():
            domain = str(row["域名"]).strip() if pd.notna(row["域名"]) else ""
            if not domain:
                df.at[idx, "状态"] = "无域名"
                continue

            try:
                info = self.fetch_domain_info(domain)
                df.at[idx, "状态"] = info["status"]
                print(f"✅ {domain} → {info['status']}")
            except Exception as e:
                error_msg = str(e)
                df.at[idx, "状态"] = f"查询失败: {error_msg[:100]}"
                print(f"❌ {domain} → 查询失败: {error_msg}")

        df.to_excel(output_path, sheet_name="Sheet1", index=False)
        print(f"\n📊 处理完成！结果已保存到：{output_path}")


if __name__ == "__main__":
    BASE_URL = "http://123.60.135.93:8066"
    USERNAME = "liwei"
    PASSWORD = "123456"

    EXCEL_PATH = r"C:\Users\Administrator\Desktop\建站域名管理.xlsx"

    reporter = DomainReporter(BASE_URL, USERNAME, PASSWORD)
    reporter.process_excel(EXCEL_PATH)