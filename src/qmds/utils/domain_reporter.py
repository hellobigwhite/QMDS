# -*- coding: utf-8 -*-
"""域名上报平台客户端（新版 YzhAdmin 平台）

平台协议（Laravel + layui，Session Cookie 鉴权）：
- 登录: GET /index/index/login 取 _token -> GET /index/index/captcha 取动图验证码
        -> OCR 识别 -> POST /index/index/loginPost (account/password/captcha/_token)
- 单条上报: POST /directory/domain/add
- 列表: POST /directory/domain/list (page/limit, layui 格式 {code,count,data})
- 详情: GET /directory/domain/get?domain_id=N
- 删除: POST /directory/domain/del (domain_id)
- 表单选项页: GET /directory/domain/index (含 host/theme/user/category 下拉选项)

验证码识别依赖 ddddocr（安装于项目 .libs 目录，见 scripts/fetch_wheels 逻辑）。
"""

import sys
import time
from pathlib import Path

import requests

from qmds.utils.logger import get_logger

log = get_logger("domain_reporter")


class DomainNotFoundError(Exception):
    """域名在上报平台中确实不存在（区别于网络/登录等临时故障）

    审查类功能据此判断是否将站点标记为"未报"：
    - DomainNotFoundError -> 平台确认无此记录，可安全标记
    - 其他异常（requests.*、登录失败 RuntimeError 等）-> 临时故障，不应改变状态
    """


# ddddocr 本地依赖目录（项目根/.libs）
_LIBS_DIR = Path(__file__).resolve().parents[3] / ".libs"
if _LIBS_DIR.is_dir() and str(_LIBS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIBS_DIR))

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

REPORT_API_BASE_URL = "http://123.60.135.93"

# QMDS 英文分类名 -> 远程上报平台中文分类名
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

# QMDS 分类名 -> 上报平台分类 ID（新版平台，注意 10/11/12 与旧平台不同）
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
    "religious_ceremonial": "11",
    "furniture": "12",
    "home_garden": "10",
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
    "宗教": "11",
    "家具": "12",
    "家居与园艺": "10",
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

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"


def _ocr_captcha_gif(content: bytes) -> str:
    """识别验证码动图：取最后一帧（字符最完整）"""
    import io

    from PIL import Image

    try:
        import ddddocr
    except ImportError as exc:
        raise RuntimeError(
            "验证码识别依赖 ddddocr 未安装：请将 ddddocr/onnxruntime/opencv "
            "wheel 解压到项目 .libs 目录（参考 .tmp/fetch_wheels.py）"
        ) from exc

    ocr = ddddocr.DdddOcr(show_ad=False)
    img = Image.open(io.BytesIO(content))
    frames = []
    try:
        while True:
            frames.append(img.convert("RGB"))
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    last = frames[-1] if frames else img.convert("RGB")
    buf = io.BytesIO()
    last.save(buf, format="PNG")
    return ocr.classification(buf.getvalue())


class DomainReporter:
    """新版上报平台客户端（Session + CSRF + 验证码 OCR）"""

    LOGIN_MAX_ATTEMPTS = 8

    def __init__(self, base_url, username, password):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = None            # 当前 CSRF _token
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _UA,
            "X-Requested-With": "XMLHttpRequest",
        })
        # 表单选项缓存: IP->host_id / 底板名->theme_id / 专员名->user_id / 分类ID->名
        self._host_map = None
        self._theme_map = None
        self._user_map = None
        self._category_map = None
        # 域名记录索引缓存 {domain_name: record} 及建立时间
        self._domain_index = None
        self._index_built_at = 0.0

    # ── 内部工具 ────────────────────────────────────────────

    def _looks_logged_out(self, resp) -> bool:
        """响应是否为登录页 HTML（会话失效）"""
        if resp.status_code in (401, 419):
            return True
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype:
            return False
        return "/index/index/login" in resp.text[:3000]

    def _login(self):
        """OCR 识别验证码登录，成功后初始化会话与表单选项"""
        import re

        last_err = ""
        for _ in range(self.LOGIN_MAX_ATTEMPTS):
            page = self._session.get(
                f"{self._base_url}/index/index/login", timeout=15, allow_redirects=False)
            m = re.search(r'name="_token" value="([^"]+)"', page.text)
            if not m:
                raise RuntimeError("登录页未找到 CSRF _token")
            cap = self._session.get(f"{self._base_url}/index/index/captcha", timeout=15)
            try:
                code = _ocr_captcha_gif(cap.content)
            except RuntimeError:
                raise
            except Exception as exc:  # OCR 单次失败重试
                last_err = str(exc)
                continue
            resp = self._session.post(
                f"{self._base_url}/index/index/loginPost",
                data={
                    "_token": m.group(1),
                    "account": self._username,
                    "password": self._password,
                    "captcha": code,
                },
                timeout=15,
                allow_redirects=False,
            )
            if "json" in resp.headers.get("Content-Type", ""):
                try:
                    body = resp.json()
                except Exception:
                    body = {"error": True, "msg": ["响应解析失败"]}
                if body.get("error"):
                    msgs = body.get("msg") or []
                    last_err = "；".join(str(x) for x in msgs) if isinstance(msgs, list) else str(msgs)
                    continue
                self._load_form_options()
                return
            if resp.status_code in (301, 302):
                self._load_form_options()
                return
            last_err = f"HTTP {resp.status_code}"
        raise RuntimeError(f"上报平台登录失败（{self.LOGIN_MAX_ATTEMPTS} 次尝试）: {last_err}")

    def _ensure_login(self):
        if self._host_map is None:
            self._login()

    def _load_form_options(self):
        """拉取域名表单页，解析 CSRF token 与下拉选项映射"""
        import re

        resp = self._session.get(f"{self._base_url}/directory/domain/index", timeout=20)
        resp.raise_for_status()
        html = resp.text

        m = re.search(r'name="_token" value="([^"]+)"', html)
        if not m:
            raise RuntimeError("表单页未找到 CSRF _token（会话可能已失效）")
        self._token = m.group(1)

        def select_options(field):
            block = re.search(
                r'name="' + field + r'".*?</select>', html, re.S)
            if not block:
                return {}
            pairs = re.findall(
                r'<option value="([0-9]+)">([^<]*)</option>', block.group(0))
            return {text.strip(): oid for oid, text in pairs}

        host_opts = select_options("host_id")
        theme_opts = select_options("theme_id")
        user_opts = select_options("user_id")
        cat_opts = select_options("category_id")

        self._host_map = host_opts          # IP -> host_id
        self._theme_map = theme_opts        # zh01 -> theme_id
        self._user_map = user_opts          # 专员名 -> user_id
        self._category_map = cat_opts       # 分类中文名 -> category_id
        self._domain_index = None           # 选项刷新后重置域名索引

    def _refresh_token(self):
        """重新拉表单页刷新 CSRF token 与选项"""
        self._load_form_options()

    def _post_json(self, path, data, retry_on_auth=True):
        """POST 并解析 JSON；会话失效时自动重登录重试一次"""
        data = {"_token": self._token, **data}
        resp = self._session.post(
            f"{self._base_url}{path}", data=data, timeout=20, allow_redirects=False)
        if self._looks_logged_out(resp):
            if retry_on_auth:
                self._login()
                return self._post_json(path, data, retry_on_auth=False)
            raise RuntimeError("上报平台会话失效，重登录后仍失败")
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype:
            resp.raise_for_status()
            raise RuntimeError(f"上报平台返回非 JSON 响应: {resp.text[:120]}")
        return resp.json()

    def _resolve_category_id(self, category):
        """解析分类为平台 category_id。

        兼容三种输入形态:
        1. 平台分类 ID（"3"/"14" 等，部分 QMDS 站点记录直接存数字）
        2. 中文分类名（"五金"/"体育用品"）
        3. 英文分类键（"hardware"/"sporting_goods"）
        """
        if not category:
            return None
        category = str(category).strip()

        # 1) 平台分类 ID 直接透传（需存在于平台类目 ID 集合中）
        #    site_management 会预先把分类名转成 ID 传入
        if category.isdigit():
            platform_ids = {str(oid) for oid in (self._category_map or {}).values()}
            if category in platform_ids:
                return category

        # 2) 中文分类名 -> ID
        cid = (self._category_map or {}).get(category)
        if cid:
            return cid

        # 3) 英文键 -> ID（REPORT_CATEGORY_ID_MAP 值即新版平台 ID）
        cid = REPORT_CATEGORY_ID_MAP.get(category)
        if cid:
            return cid

        # 4) 兜底: 该值不在任何映射中
        return None

    # ── 对外接口（保持旧版签名兼容） ──────────────────────────

    def submit_domain(self, payload):
        """上报单个域名。

        payload 兼容旧版字段: name/serverip/template/category(+categoryTag/language)
        映射到新版: domain_name/host_id/theme_id/category_id/user_id/create_date
        """
        import re

        self._ensure_login()

        domain = (payload.get("name") or "").strip()
        server = (payload.get("serverip") or "").strip()
        template = (payload.get("template") or "").strip()
        category = str(payload.get("category") or "").strip()

        missing = []
        if not domain:
            missing.append("域名(name)")
        if not server:
            missing.append("服务器(serverip)")
        if not template:
            missing.append("模板(template)")
        if not category:
            missing.append("分类(category)")
        if missing:
            raise RuntimeError("缺少必填字段: " + ", ".join(missing))

        host_id = self._host_map.get(server)
        if not host_id:
            raise RuntimeError(
                f"服务器 {server} 不在上报平台服务器列表中，请先在平台添加该服务器")

        theme_id = self._theme_map.get(template)
        if not theme_id:
            # 容错: 模板名大小写/前后空白
            for name, oid in self._theme_map.items():
                if name.lower() == template.lower():
                    theme_id = oid
                    break
        if not theme_id:
            raise RuntimeError(
                f"模板 {template} 不在上报平台底板列表中（可用: "
                + ", ".join(sorted(self._theme_map)) + "）")

        category_id = self._resolve_category_id(category)
        if not category_id:
            raise RuntimeError(f"无效分类: {category}")

        user_id = self._user_map.get(self._username)
        if not user_id:
            raise RuntimeError(
                f"上报账号 {self._username} 不在平台采集专员列表中（可用: "
                + ", ".join(sorted(self._user_map)) + "）")

        data = {
            "domain_name": domain,
            "host_id": host_id,
            "theme_id": theme_id,
            "category_id": category_id,
            "user_id": user_id,
            "create_date": time.strftime("%Y-%m-%d"),
            "is_special": "0",
            "is_submit": "0",
            "remarks": payload.get("remarks") or "",
        }
        body = self._post_json("/directory/domain/add", data)
        if body.get("error"):
            msgs = body.get("msg") or []
            msg = "；".join(
                "、".join(str(y) for y in x) if isinstance(x, list) else str(x)
                for x in msgs) if isinstance(msgs, list) else str(msgs)
            raise RuntimeError(f"上报失败: {msg}")
        # 不清空索引：刚上报的域名由 fetch_domain_info 的
        # 第一页轻量兜底查询覆盖（列表按新增倒序，最新在最前）
        return body

    def _fetch_list_page(self, page, limit):
        body = self._post_json(
            "/directory/domain/list", {"page": page, "limit": limit})
        data = body.get("data")
        if not isinstance(data, list):
            data = []
        total = body.get("count") or 0
        return data, int(total)

    # 索引最大新鲜期（秒）：期内未命中的域名不再触发全量刷新
    INDEX_MAX_AGE = 300

    def _build_domain_index(self, force=True):
        """拉全量域名列表建立索引（列表接口不支持按名过滤）

        force=False 时索引仍在新鲜期内则跳过（供低频调用方复用缓存）。
        """
        if not force and self._domain_index is not None and (
                time.time() - self._index_built_at < self.INDEX_MAX_AGE):
            return
        self._ensure_login()
        all_records = self.fetch_all_domains()
        self._domain_index = {r.get("domain_name", ""): r for r in all_records}
        self._index_built_at = time.time()
        log.info(f"上报平台域名索引已建立: {len(self._domain_index)} 条")

    def _refresh_first_page_into_index(self):
        """轻量兜底：拉列表第一页并入索引（1 个请求）。

        平台列表按新增倒序，刚上报的域名必然出现在第一页，
        避免为单个新域名触发 20+ 页的全量重建。
        """
        records, _total = self._fetch_list_page(1, 25)
        changed = 0
        for r in records:
            key = r.get("domain_name", "")
            if not key:
                continue
            old = self._domain_index.get(key)
            if old is None or old.get("domain_id") != r.get("domain_id") \
                    or old.get("status") != r.get("status"):
                self._domain_index[key] = r
                changed += 1
        return changed

    def fetch_domain_info(self, name):
        """按域名查询记录，返回 {id, status}。

        查找顺序（由轻到重）：
        1. 内存索引
        2. 列表第一页轻量兜底（覆盖刚上报的新域名）
        3. 索引已过期（>INDEX_MAX_AGE）时全量刷新一次
        均未命中抛 DomainNotFoundError（平台确认无此记录）。
        """
        self._ensure_login()
        if self._domain_index is None:
            self._build_domain_index(force=True)

        record = self._domain_index.get(name)
        if record is None:
            self._refresh_first_page_into_index()
            record = self._domain_index.get(name)
        if record is None and time.time() - self._index_built_at >= self.INDEX_MAX_AGE:
            self._build_domain_index(force=True)
            record = self._domain_index.get(name)
        if record is None:
            raise DomainNotFoundError(f"未找到域名记录: {name}")
        return {"id": record.get("domain_id"), "status": record.get("status")}

    def fetch_domains_by_date(self, date_text):
        """获取指定创建日期的域名记录列表"""
        self._ensure_login()
        if self._domain_index is None:
            self._build_domain_index()
        return [r for r in self._domain_index.values()
                if r.get("create_date") == date_text]

    def fetch_all_domains(self, page_size=25):
        """获取所有域名记录（分页拉全量）。

        平台固定每页最多返回 25 条（limit 参数仅装饰），按页码翻页。
        返回记录在平台原始字段基础上补充旧版兼容字段：
        name/serverip，便于既有调用方直接使用。
        """
        self._ensure_login()
        all_records = []
        seen_ids = set()
        page = 1
        while True:
            records, total = self._fetch_list_page(page, page_size)
            new = [r for r in records
                   if str(r.get("domain_id")) not in seen_ids]
            for r in new:
                seen_ids.add(str(r.get("domain_id")))
            all_records.extend(new)
            if not records or len(all_records) >= total:
                break
            page += 1
            if page > 1000:  # 安全护栏
                break
        normalized = []
        for r in all_records:
            r = dict(r)
            r.setdefault("name", r.get("domain_name", ""))
            r.setdefault("serverip", r.get("host_ip", "") or "")
            # 旧版调用方（order_db.sync_from_reporter）读取 category 字段，
            # 新平台记录只有 category_id，补充以保持兼容
            r.setdefault("category", r.get("category_id", "") or "")
            normalized.append(r)
        return normalized

    def delete_domain(self, domain_id):
        """按平台 domain_id 删除域名"""
        self._ensure_login()
        body = self._post_json("/directory/domain/del", {"domain_id": domain_id})
        if body.get("error"):
            msgs = body.get("msg") or []
            msg = "；".join(str(x) for x in msgs) if isinstance(msgs, list) else str(msgs)
            raise RuntimeError(f"删除失败: {msg}")
        if self._domain_index is not None:
            for k, v in list(self._domain_index.items()):
                if str(v.get("domain_id")) == str(domain_id):
                    del self._domain_index[k]
                    break
        return body

    def fetch_categories(self):
        """获取类目列表，返回 {id: name} 的映射"""
        self._ensure_login()
        return {oid: name for name, oid in (self._category_map or {}).items()}
