import random
import re
from collections import defaultdict

import requests
import urllib3
from bs4 import BeautifulSoup


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ORDER_STATUS_LABELS = {
    "pending": "待付款",
    "processing": "处理中",
    "on-hold": "搁置",
    "completed": "已完成",
    "cancelled": "已取消",
    "refunded": "已退款",
    "failed": "失败",
    "checkout-draft": "草稿",
    "trash": "回收站",
    "all": "全部",
}
ORDER_STATUS_PATTERNS = [
    "pending",
    "processing",
    "on-hold",
    "completed",
    "cancelled",
    "refunded",
    "failed",
    "checkout-draft",
]
TARGET_ORDER_STATUSES = ("pending", "processing", "completed")


def _normalize_status_key(value):
    key = str(value or "").strip().lower()
    if not key:
        return ""
    if key.startswith("wc-"):
        key = key[3:]
    if key.startswith("status-"):
        key = key[7:]
    return key


def _normalize_site(domain):
    domain = str(domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _site_username(domain):
    domain = _normalize_site(domain)
    return f"Ad{domain.split('.')[0].strip()}min"


def _extract_count(text):
    match = re.search(r"\((\d+)\)", str(text or ""))
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\b", str(text or ""))
    if match:
        return int(match.group(1))
    return None


class WooOrderChecker:
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
        site = _normalize_site(site)
        base_url = f"https://www.{site}"
        login_url = f"{base_url}/bbwllogin/"
        data = {
            "log": _site_username(site),
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"{base_url}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data)
        if resp is None:
            raise RuntimeError("WordPress 登录请求失败")
        if any("wordpress_logged_in" in cookie.name for cookie in self._session.cookies):
            return base_url
        admin_check = self._request("GET", f"{base_url}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            soup = BeautifulSoup(admin_check.text, "html.parser")
            body = soup.find("body")
            body_classes = " ".join(body.get("class", []) or []) if body else ""
            if "wp-admin" in body_classes or "wpbody-content" in body_classes:
                return base_url
        raise RuntimeError("WordPress 登录失败")

    def _extract_status_counts(self, soup):
        counts = {}
        for item in soup.select("ul.subsubsub li"):
            anchor = item.find("a", href=True)
            if anchor is None:
                continue

            key = ""
            for cls in item.get("class", []) or []:
                normalized = _normalize_status_key(cls)
                if normalized in ORDER_STATUS_LABELS:
                    key = normalized
                    break

            if not key:
                href = anchor.get("href", "")
                match = re.search(r"status=([a-z0-9-]+)", href)
                if match:
                    key = _normalize_status_key(match.group(1))
                elif "page=wc-orders" in href or "post_type=shop_order" in href:
                    key = "all"

            if key not in ORDER_STATUS_LABELS:
                continue

            count = None
            count_el = anchor.find(class_="count")
            if count_el:
                count = _extract_count(count_el.get_text(" ", strip=True))
            if count is None:
                count = _extract_count(anchor.get_text(" ", strip=True))
            if count is not None:
                counts[key] = int(count)
        return counts

    def _extract_visible_count(self, soup):
        counter = soup.select_one(".displaying-num")
        if counter is not None:
            count = _extract_count(counter.get_text(" ", strip=True))
            if count is not None:
                return int(count)
        return None

    def _detect_row_status(self, row):
        classes = row.get("class", []) or []
        for cls in classes:
            cls = str(cls)
            if cls.startswith("status-"):
                return cls.replace("status-", "").strip()
        for element in row.find_all(True):
            for cls in element.get("class", []) or []:
                cls = str(cls)
                if cls.startswith("status-"):
                    return cls.replace("status-", "").strip()
        text = row.get_text(" ", strip=True).lower()
        for status in ORDER_STATUS_PATTERNS:
            if status in text:
                return status
        return ""

    def _extract_order_rows(self, soup):
        rows = []
        for row in soup.find_all("tr"):
            classes = " ".join(str(item) for item in (row.get("class", []) or []))
            row_id = str(row.get("id", "") or "")
            has_order_row_marker = (
                row_id.startswith("order-")
                or "type-shop_order" in classes
                or "order_number" in row.get_text(" ", strip=True).lower()
            )
            if row.find("th") and not has_order_row_marker:
                continue

            row_text = row.get_text(" ", strip=True)
            if not row_text:
                continue

            order_id = ""
            order_date = ""
            order_total = ""
            for td in row.find_all("td"):
                td_classes = " ".join(td.get("class", []) or [])
                if not order_id:
                    for href in [a.get("href", "") for a in td.find_all("a", href=True)]:
                        match = re.search(r"(?:action=edit&id=|post=)(\d+)", href)
                        if match:
                            order_id = match.group(1)
                            break
                if not order_date and any(c in td_classes for c in ("order_date", "order-date", "column-order_date")):
                    time_tag = td.find("time")
                    if time_tag:
                        order_date = time_tag.get("datetime", "") or time_tag.get_text(" ", strip=True)
                    if not order_date:
                        order_date = td.get_text(" ", strip=True)[:60]
                if not order_total and any(c in td_classes for c in ("order_total", "order-total", "column-order_total", "total")):
                    order_total = td.get_text(" ", strip=True)[:60]

            if not order_id:
                match = re.search(r"#?(\d{3,})", row_text)
                if match:
                    order_id = match.group(1)

            status = self._detect_row_status(row)
            if not order_id and not status:
                continue

            rows.append(
                {
                    "order_id": order_id,
                    "status": status,
                    "status_label": ORDER_STATUS_LABELS.get(status, status or "未知"),
                    "date": order_date,
                    "total": order_total,
                    "text": row_text[:200],
                }
            )
        return rows

    def _request_orders_page(self, base_url, status_key=None, month=""):
        normalized_status = _normalize_status_key(status_key)
        month_param = ""
        if month:
            m = str(month).strip().replace("-", "")
            if m.isdigit() and len(m) >= 6:
                month_param = f"&m={m[:6]}"
        if normalized_status:
            order_urls = [
                f"{base_url}/wp-admin/admin.php?page=wc-orders&status=wc-{normalized_status}{month_param}",
                f"{base_url}/wp-admin/edit.php?post_type=shop_order&post_status=wc-{normalized_status}{month_param}",
            ]
        else:
            order_urls = [
                f"{base_url}/wp-admin/admin.php?page=wc-orders&status=all{month_param}",
                f"{base_url}/wp-admin/edit.php?post_type=shop_order{month_param}",
            ]

        last_resp = None
        for url in order_urls:
            resp = self._request("GET", url)
            if resp is None:
                continue
            last_resp = resp
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            body_classes = (soup.find("body") or soup).get("class", []) or []
            if any("woocommerce" in str(c).lower() for c in body_classes):
                return url, resp
            if "wc-orders" in resp.url or "shop_order" in resp.url:
                return url, resp
            if soup.find("ul", class_="subsubsub"):
                return url, resp

        if last_resp is None:
            raise RuntimeError("订单页面请求失败")
        return order_urls[0], last_resp

    def _extract_filtered_status_total(self, soup, status_key):
        visible_count = self._extract_visible_count(soup)
        if visible_count is not None:
            return int(visible_count)

        normalized_status = _normalize_status_key(status_key)
        status_counts = self._extract_status_counts(soup)
        if normalized_status in status_counts:
            return int(status_counts.get(normalized_status, 0) or 0)

        rows = self._extract_order_rows(soup)
        return sum(1 for row in rows if _normalize_status_key(row.get("status")) == normalized_status)

    def _get_total_pages(self, soup):
        el = soup.select_one(".paging-input")
        if el:
            m = re.search(r"of\s+(\d+)", el.get_text())
            if m:
                return int(m.group(1))
        el = soup.select_one(".total-pages")
        if el:
            try:
                return int(el.get_text().strip())
            except ValueError:
                pass
        return 1

    def _fetch_all_orders(self, base_url, month=""):
        all_rows = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            url = f"{base_url}/wp-admin/admin.php?page=wc-orders&paged={page}"
            if month:
                m = str(month).strip().replace("-", "")
                if m.isdigit() and len(m) >= 6:
                    url += f"&m={m[:6]}"
            resp = self._request("GET", url)
            if resp is None or resp.status_code != 200:
                if page > 1:
                    break
                raise RuntimeError("订单页访问失败")
            soup = BeautifulSoup(resp.text, "html.parser")
            if page == 1:
                total_pages = self._get_total_pages(soup)
            rows = self._extract_order_rows(soup)
            all_rows.extend(rows)
            page += 1
        return all_rows, soup if all_rows else None

    def check_orders(self, site, month=""):
        base_url = self._login(site)

        all_orders, final_soup = self._fetch_all_orders(base_url, month)
        status_counts = self._extract_status_counts(final_soup) if final_soup else {}

        total_orders = len(all_orders)
        real_order_statuses = ("pending", "processing", "completed")
        real_orders = sum(1 for o in all_orders if o.get("status") in real_order_statuses)

        monthly = defaultdict(lambda: {"count": 0, "valid_count": 0, "valid_total": 0.0, "cancelled": 0})
        for o in all_orders:
            if o.get("status") == "trash":
                continue
            d = o.get("date", "")
            month_key = d[:7] if len(d) >= 7 else ""
            if not month_key:
                continue
            monthly[month_key]["count"] += 1
            if o.get("status") == "cancelled":
                monthly[month_key]["cancelled"] += 1
            if o.get("status") not in ("failed", "on-hold", "cancelled", "refunded"):
                monthly[month_key]["valid_count"] += 1
                try:
                    total_str = o.get("total", "0").replace("$", "").replace(",", "").strip()
                    monthly[month_key]["valid_total"] += float(total_str) if total_str else 0
                except ValueError:
                    pass

        recent_orders = [o for o in all_orders if o.get("status") != "trash"][:5]

        return {
            "site": _normalize_site(site),
            "has_orders": total_orders > 0,
            "has_real_orders": real_orders > 0,
            "total_orders": total_orders,
            "real_orders": real_orders,
            "status_counts": {k: v for k, v in status_counts.items()},
            "recent_orders": recent_orders,
            "monthly_breakdown": {
                k: {
                    "count": v["count"],
                    "valid_count": v["valid_count"],
                    "valid_total": round(v["valid_total"], 2),
                    "cancelled": v["cancelled"],
                }
                for k, v in sorted(monthly.items())
            },
        }
