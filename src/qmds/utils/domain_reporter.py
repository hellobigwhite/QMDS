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

# QMDS 英文分类名 → 远程上报平台中文分类名
REPORT_CATEGORY_NAME_MAP = {
    "hardware": "五金",
    "vehicles_parts": "交通工具",
    "sporting_goods": "体育用品",
    "health_beauty": "保健",
    "office_supplies": "办公用品",
    "animals_pet_supplies": "动物",
    "business_industrial": "商业",
    "baby_toddler": "婴幼儿用品",
    "media": "媒体",
    "religious_ceremonial": "宗教",
    "furniture": "家具",
    "home_garden": "家居与园艺",
    "mature": "成人",
    "apparel_accessories": "服饰与配饰",
    "toys_games": "玩具",
    "electronics": "电子产品",
    "cameras_optics": "相机与光学器件",
    "luggage_bags": "箱包",
    "arts_entertainment": "艺术与娱乐",
    "software": "软件",
    "food_beverages_tobacco": "饮食",
}

# QMDS 英文分类名 → 远程上报平台分类 ID
REPORT_CATEGORY_ID_MAP = {
    # 英文键
    "hardware": "1",
    "vehicles_parts": "2",
    "sporting_goods": "3",
    "health_beauty": "4",
    "office_supplies": "5",
    "animals_pet_supplies": "6",
    "business_industrial": "7",
    "baby_toddler": "8",
    "media": "9",
    "religious_ceremonial": "10",
    "furniture": "11",
    "home_garden": "12",
    "mature": "13",
    "apparel_accessories": "14",
    "toys_games": "15",
    "electronics": "16",
    "cameras_optics": "17",
    "luggage_bags": "18",
    "arts_entertainment": "19",
    "software": "20",
    "food_beverages_tobacco": "21",
    # 中文键（兼容数据库中存储中文分类名的情况）
    "五金": "1",
    "交通工具": "2",
    "体育用品": "3",
    "保健": "4",
    "办公用品": "5",
    "动物": "6",
    "商业": "7",
    "婴幼儿用品": "8",
    "媒体": "9",
    "宗教": "10",
    "家具": "11",
    "家居与园艺": "12",
    "成人": "13",
    "服饰与配饰": "14",
    "玩具": "15",
    "电子产品": "16",
    "相机与光学器件": "17",
    "箱包": "18",
    "艺术与娱乐": "19",
    "软件": "20",
    "饮食": "21",
}


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
