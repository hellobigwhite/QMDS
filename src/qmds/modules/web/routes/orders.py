import json
import os
import re
import time
from datetime import datetime
from functools import partial
from queue import Queue, Empty

from flask import Blueprint, Response, jsonify, render_template, request

from qmds.modules.web.db_helpers import get_order_db
from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

bp = Blueprint("orders", __name__)
log = get_logger(__name__)

_order_log_queues = {}


def _order_log(task_id, msg, level="info"):
    q = _order_log_queues.get(task_id)
    if q is not None:
        q.put({"msg": msg, "level": level, "time": time.strftime("%H:%M:%S")})
    log_func = getattr(log, level, log.info)
    log_func(f"[{task_id}] {msg}")


def _derive_domain(domain):
    d = domain.strip().lower()
    if not d.startswith("www."):
        d = "www." + d
    return d


def _wp_login(session, domain, password, max_retries=3):
    from bs4 import BeautifulSoup
    site_url = f"https://{_derive_domain(domain)}"
    name = domain.replace('www.', '').replace('.com', '').strip()
    username = f"Ad{name}Min"
    login_url = f"{site_url}/bbwllogin/"
    data = {"log": username, "pwd": password, "wp-submit": "Log In",
            "redirect_to": f"{site_url}/wp-admin/", "testcookie": "1"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}

    if any("wordpress_logged_in" in c.name for c in session.cookies):
        return username

    for attempt in range(max_retries):
        try:
            session.post(login_url, data=data, headers=headers, verify=False, timeout=20)
            logged_in = any("wordpress_logged_in" in c.name for c in session.cookies)
            if not logged_in:
                check = session.get(f"{site_url}/wp-admin/", verify=False, timeout=20)
                logged_in = check.status_code == 200 and "wp-admin" in check.url
            if logged_in:
                return username
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise RuntimeError(f"WP login failed for {domain}: {e}")
    raise RuntimeError(f"WP login failed for {domain}")


def _parse_order_row(row_html):
    import html as html_mod
    m = re.search(r'<tr[^>]*id="order-(\d+)"', row_html)
    if not m:
        return None
    order_id = int(m.group(1))
    order_view_id = ""
    m_ov = re.search(r'action=edit&amp;id=(\d+)', row_html)
    if not m_ov:
        m_ov = re.search(r'action=edit&id=(\d+)', row_html)
    if m_ov:
        order_view_id = m_ov.group(1)
    date_created = None
    m3 = re.search(r'<time datetime="([^"]+)"', row_html)
    if m3:
        dt_src = m3.group(1).replace('T', ' ')
        if '+' in dt_src: dt_src = dt_src[:dt_src.index('+')]
        if 'Z' in dt_src: dt_src = dt_src.replace('Z', '')
        date_created = dt_src
    status = ""
    m4 = re.search(r'<mark[^>]*class="order-status[^"]*"[^>]*><span>(.*?)</span></mark>', row_html, re.S)
    if m4:
        status = html_mod.unescape(m4.group(1).strip()).lower()
    total = 0
    m5 = re.search(r"<td[^>]*class='order_total[^']*'[^>]*>(.*?)</td>", row_html, re.S)
    if m5:
        raw = html_mod.unescape(re.sub(r'<[^>]+>', '', m5.group(1))).strip()
        nums = re.findall(r'([\d,]+\.\d{2})', raw)
        if nums:
            total = float(nums[-1].replace(',', ''))
    return {"order_id": order_id, "order_view_id": order_view_id, "order_time": date_created, "order_status": status, "order_amount": total}


class RetryableError(Exception):
    """可重试的错误（登录失败、请求超时等）"""
    pass


def _wc_get_nonce(session, site_url):
    """访问 wp-admin 订单列表页,提取 apiFetch nonce 用于 REST API cookie 认证"""
    list_url = f"{site_url}/wp-admin/admin.php?page=wc-orders&paged=1"
    r = session.get(list_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if r.status_code != 200:
        return ""
    m = re.search(r'createNonceMiddleware\(\s*"([a-f0-9]+)"\s*\)', r.text)
    return m.group(1) if m else ""


def _parse_wc_order(o, server_domain, order_category):
    """从 WooCommerce REST API 订单 JSON 提取统一字段(列表+详情一次拿全)"""
    dt = o.get("date_created", "") or ""
    dt = dt.replace("T", " ")
    if "+" in dt:
        dt = dt[:dt.index("+")]
    if "Z" in dt:
        dt = dt.replace("Z", "")

    def _addr(a):
        if not a:
            return {}
        d = {}
        if a.get("first_name") or a.get("last_name"):
            d["name"] = (a.get("first_name", "") + " " + a.get("last_name", "")).strip()
        if a.get("address_1"):
            d["address_1"] = a["address_1"]
        if a.get("address_2"):
            d["address_2"] = a["address_2"]
        if a.get("city"):
            d["city"] = a["city"]
        if a.get("state"):
            d["state"] = a["state"]
        if a.get("postcode"):
            d["postcode"] = a["postcode"]
        if a.get("city") or a.get("state") or a.get("postcode"):
            d["city_state_zip"] = ", ".join([x for x in [a.get("city", ""), a.get("state", ""), a.get("postcode", "")] if x])
        if a.get("phone"):
            d["phone"] = a["phone"]
        return d

    items = []
    for li in o.get("line_items", []) or []:
        item = {"product_name": li.get("name", "")}
        if li.get("sku"):
            item["sku"] = li["sku"]
        img = li.get("image") or {}
        if isinstance(img, dict) and img.get("src"):
            item["image"] = img["src"]
        if li.get("quantity") is not None:
            try:
                item["quantity"] = int(li["quantity"])
            except (ValueError, TypeError):
                item["quantity"] = 1
        if li.get("total") is not None:
            try:
                item["subtotal"] = float(li["total"])
            except (ValueError, TypeError):
                item["subtotal"] = 0
        if item.get("product_name"):
            items.append(item)

    billing = o.get("billing") or {}
    customer_name = (billing.get("first_name", "") + " " + billing.get("last_name", "")).strip()

    return {
        "order_view_id": str(o.get("id", "")),
        "order_time": dt,
        "order_status": o.get("status", ""),
        "order_amount": float(o.get("total") or 0),
        "customer_email": billing.get("email", ""),
        "customer_name": customer_name,
        "billing_address": _addr(billing),
        "shipping_address": _addr(o.get("shipping")),
        "items": items,
        "domain": server_domain,
        "order_category": order_category,
    }


def _parse_address_column(column):
    """解析 billing/shipping 地址列,返回 (email, name, address_dict)"""
    email = ""
    name = ""
    address_dict = {}

    email_link = column.find("a", href=re.compile(r"^mailto:"))
    if email_link:
        email = email_link.get("href", "").replace("mailto:", "").strip()

    tel_link = column.find("a", href=re.compile(r"^tel:"))
    if tel_link:
        phone = tel_link.get("href", "").replace("tel:", "").strip()
        if phone:
            address_dict["phone"] = phone

    address_div = column.find("div", class_="address")
    if address_div:
        p = address_div.find("p")
        if p:
            for br in p.find_all("br"):
                br.replace_with("\n")
            lines = [l.strip() for l in p.get_text().split("\n") if l.strip()]
            if lines:
                name = lines[0]
                address_dict["name"] = lines[0]
                if len(lines) > 1:
                    last_idx = len(lines) - 1
                    last = lines[last_idx]
                    m = re.match(r'^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', last)
                    if m and last_idx > 1:
                        address_dict["city"] = m.group(1)
                        address_dict["state"] = m.group(2)
                        address_dict["postcode"] = m.group(3)
                        address_dict["city_state_zip"] = last
                        middle = lines[1:last_idx]
                        if middle:
                            address_dict["address_1"] = middle[0]
                        if len(middle) > 1:
                            address_dict["address_2"] = ", ".join(middle[1:])
                    else:
                        address_dict["address_1"] = lines[1]
                        if len(lines) > 2:
                            address_dict["address_2"] = ", ".join(lines[2:])

    if not email:
        for p in column.find_all("p"):
            text = p.get_text(" ", strip=True)
            if "@" in text:
                email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', text)
                if email_match:
                    email = email_match.group(0)
                    break

    return email, name, address_dict


def _parse_order_detail(html_text):
    """解析订单详情页，提取商品和客户信息"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    result = {
        "items": [],
        "customer_email": "",
        "customer_name": "",
        "billing_address": {},
        "shipping_address": {}
    }

    items_table = soup.find("table", class_="woocommerce_order_items")
    if items_table:
        tbody = items_table.find("tbody")
        if tbody:
            for row in tbody.find_all("tr"):
                item = {}

                name_cell = row.find("td", class_="name")
                if name_cell:
                    name_link = name_cell.find("a", class_="wc-order-item-name")
                    if name_link:
                        item["product_name"] = name_link.get_text(strip=True)
                    sku_div = name_cell.find("div", class_="wc-order-item-sku")
                    if sku_div:
                        sku_text = sku_div.get_text(" ", strip=True)
                        sku_match = re.search(r"SKU[：:]\s*(\S+)", sku_text)
                        if sku_match:
                            item["sku"] = sku_match.group(1)

                thumb_cell = row.find("td", class_="thumb")
                if thumb_cell:
                    thumb_img = thumb_cell.find("img")
                    if thumb_img:
                        src = thumb_img.get("src", "") or thumb_img.get("data-src", "")
                        if src:
                            item["image"] = src

                qty_cell = row.find("td", class_="quantity")
                if qty_cell:
                    qty_text = qty_cell.get_text(strip=True)
                    try:
                        item["quantity"] = int(qty_text)
                    except ValueError:
                        item["quantity"] = 1

                total_cell = row.find("td", class_="line_total") or row.find("td", class_="line_cost")
                if total_cell:
                    total_text = total_cell.get_text(strip=True)
                    nums = re.findall(r'[\d,.]+', total_text)
                    if nums:
                        try:
                            item["subtotal"] = float(nums[-1].replace(",", ""))
                        except ValueError:
                            item["subtotal"] = 0

                if item.get("product_name"):
                    result["items"].append(item)

    order_data_columns = soup.find_all("div", class_="order_data_column")
    for column in order_data_columns:
        heading = column.find("h3")
        if not heading:
            continue

        heading_text = heading.get_text(strip=True).lower()

        if "billing" in heading_text or "账单" in heading_text:
            email, name, addr = _parse_address_column(column)
            if email:
                result["customer_email"] = email
            if name:
                result["customer_name"] = name
            if addr:
                result["billing_address"] = addr

        elif "shipping" in heading_text or "收货" in heading_text:
            _email, name, addr = _parse_address_column(column)
            if addr:
                result["shipping_address"] = addr
            if name and not result.get("customer_name"):
                result["customer_name"] = name

    return result


def _fetch_order_details_for_server(server, orders, log_func, wp_password, task_id=None):
    """获取单个服务器的订单详情(用 WooCommerce REST API)"""
    import requests as req
    req.packages.urllib3.disable_warnings()

    domain = _derive_domain(server["domain"])
    site_url = f"https://{domain}"
    ip = server.get("ip", "")

    if not ip:
        log_func("  [ERR] 无IP", "error")
        return {"success": 0, "failed": 0, "deduplicated": 0}

    order_db = get_order_db()
    if not order_db:
        log_func("  [ERR] 数据库连接失败", "error")
        return {"success": 0, "failed": 0, "deduplicated": 0}

    session = req.Session()
    session.verify = False
    try:
        _wp_login(session, domain, wp_password)
        log_func(f"  Login OK")
    except RuntimeError as e:
        log_func(f"  [ERR] {e}", "error")
        raise RetryableError(f"登录失败: {e}")

    nonce = _wc_get_nonce(session, site_url)
    if not nonce:
        log_func("  [ERR] 无法获取 REST API nonce", "error")
        return {"success": 0, "failed": len(orders), "deduplicated": 0}
    log_func(f"  nonce OK, 开始获取 {len(orders)} 个订单详情")

    success = 0
    failed = 0
    deduplicated = 0
    total = len(orders)
    api_headers = {"User-Agent": "Mozilla/5.0", "X-WP-Nonce": nonce}

    for i, order in enumerate(orders, 1):
        if task_id and task_manager.is_stopped(task_id):
            log_func(f"  [STOP] 任务被停止")
            break

        order_time = order.get("order_time", "")
        order_domain = order.get("domain", domain)
        order_view_id = order.get("order_view_id", "")

        try:
            if order_view_id:
                api_url = f"{site_url}/wp-json/wc/v3/orders/{order_view_id}"
            else:
                log_func(f"  [{i}/{total}] ⚠️ 无 order_id,跳过")
                failed += 1
                continue

            r = session.get(api_url, headers=api_headers, timeout=15)
            if r.status_code != 200:
                failed += 1
                log_func(f"  [{i}/{total}] ❌ API 失败: HTTP {r.status_code}")
                time.sleep(0.3)
                continue

            parsed = _parse_wc_order(r.json(), order_domain, order.get("order_category", ""))
            result = order_db.update_order_details(
                ip=ip,
                domain=order_domain,
                order_time=order_time,
                customer_email=parsed.get("customer_email", ""),
                order_amount=order.get("order_amount", 0),
                items=parsed.get("items", []),
                billing_address=parsed.get("billing_address", {}),
                shipping_address=parsed.get("shipping_address", {}),
                customer_name=parsed.get("customer_name", ""),
            )

            if result.get("updated"):
                success += 1
                items_count = len(parsed.get("items", []))
                log_func(f"  [{i}/{total}] ✅ 详情已更新 - {items_count}件商品")

            if result.get("deduplicated", 0) > 0:
                deduplicated += result["deduplicated"]
                log_func(f"  [{i}/{total}] ⚠️ 去重: 删除 {result['deduplicated']} 条")
        except Exception as e:
            failed += 1
            log_func(f"  [{i}/{total}] ❌ 请求失败: {e}")

        time.sleep(0.3)

    return {"success": success, "failed": failed, "deduplicated": deduplicated}


def _fetch_orders_for_server(server, year, month, log_func, date_from=None, date_to=None, wp_password="", task_id=None):
    """用 WooCommerce REST API 抓取订单(列表+详情一次拿全)"""
    import requests as req
    req.packages.urllib3.disable_warnings()
    domain = _derive_domain(server["domain"])
    site_url = f"https://{domain}"
    ip = server.get("ip", "")
    if not ip:
        log_func("  [ERR] 无IP", "error")
        return 0
    order_db = get_order_db()
    if not order_db:
        log_func("  [ERR] 数据库连接失败", "error")
        return 0
    order_db.ensure_orders_indexes(ip)

    session = req.Session()
    session.verify = False
    try:
        _wp_login(session, domain, wp_password)
        log_func(f"  Login OK")
    except RuntimeError as e:
        log_func(f"  [ERR] {e}", "error")
        raise RetryableError(f"登录失败: {e}")

    nonce = _wc_get_nonce(session, site_url)
    if not nonce:
        log_func("  [ERR] 无法获取 REST API nonce", "error")
        raise RetryableError("无法获取 REST API nonce")
    log_func(f"  nonce OK")

    # 构造日期过滤: 优先用 date_from/date_to,否则用 year-month
    if date_from and date_to:
        after = f"{date_from}T00:00:00"
        before = f"{date_to}T23:59:59"
    else:
        after = f"{year}-{month:02d}-01T00:00:00"
        if month == 12:
            before = f"{year + 1}-01-01T00:00:00"
        else:
            before = f"{year}-{month + 1:02d}-01T00:00:00"

    api_headers = {"User-Agent": "Mozilla/5.0", "X-WP-Nonce": nonce}
    page = 1
    total_fetched = 0

    while True:
        if task_id and task_manager.is_stopped(task_id):
            log_func(f"  [STOP] 任务被停止")
            return total_fetched

        api_url = (f"{site_url}/wp-json/wc/v3/orders?per_page=100&page={page}"
                   f"&status=any&after={after}&before={before}")
        try:
            r = session.get(api_url, headers=api_headers, timeout=20)
        except Exception as e:
            log_func(f"  [ERR] 请求失败: {e}", "error")
            raise RetryableError(f"请求失败: {e}")

        if r.status_code != 200:
            log_func(f"  [ERR] API HTTP {r.status_code}: {r.text[:200]}", "error")
            break

        try:
            orders = r.json()
        except ValueError:
            log_func("  [ERR] 返回非 JSON", "error")
            break

        if not orders:
            break

        log_func(f"  第 {page} 页: {len(orders)} 个订单")
        written = 0
        for o in orders:
            parsed = _parse_wc_order(o, server["domain"], server.get("main_category", ""))
            status_lower = (parsed.get("order_status") or "").lower()
            if status_lower in ("on-hold", "on hold"):
                continue
            if not parsed.get("order_time"):
                continue
            # 写入基础信息(含 order_view_id)
            order_db.insert_order(
                ip, server["domain"], parsed["order_time"],
                parsed["order_status"], parsed["order_amount"],
                server.get("main_category", ""), parsed.get("order_view_id", "")
            )
            # 同步写入详情(商品/地址/客户)
            if parsed.get("items") or parsed.get("customer_email") or parsed.get("customer_name"):
                order_db.update_order_details(
                    ip=ip, domain=server["domain"], order_time=parsed["order_time"],
                    customer_email=parsed.get("customer_email", ""),
                    order_amount=parsed.get("order_amount", 0),
                    items=parsed.get("items", []),
                    billing_address=parsed.get("billing_address", {}),
                    shipping_address=parsed.get("shipping_address", {}),
                    customer_name=parsed.get("customer_name", ""),
                )
            written += 1

        total_fetched += written
        log_func(f"    写入 {written} 条")
        if len(orders) < 100:
            break
        page += 1
        time.sleep(0.5)

    return total_fetched


def _run_fetch_all(year, month, task_id, date_from="", date_to=""):
    order_db = get_order_db()
    if not order_db:
        _order_log(task_id, "数据库连接失败", "error")
        task_manager.update(task_id, status="failed", message="数据库连接失败")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    servers = order_db.get_servers()
    if not servers:
        _order_log(task_id, "没有配置任何服务器", "warn")
        task_manager.update(task_id, status="completed", message="没有配置任何服务器")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    wp_password = ""
    try:
        from qmds.db.site_db import SiteDBClient
        site_db = SiteDBClient()
        settings = site_db.get_all_settings()
        site_db.close()
        wp_password = settings.get("wp_password", "")
    except:
        pass
    if not wp_password:
        wp_password = os.environ.get("WP_PASSWORD", "")
    if not wp_password:
        _order_log(task_id, "未配置 WordPress 密码，请在配置页面设置", "error")
        task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    _order_log(task_id, f"开始并发抓取 {len(servers)} 台服务器 (5线程)")
    task_manager.update(task_id, status="running", message=f"开始抓取 {len(servers)} 台服务器")

    failed_servers = []
    servers_with_orders = []
    done = 0
    total_servers = len(servers)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _log_wrapper(msg, level="info"):
        _order_log(task_id, msg, level)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
        for future in as_completed(futures):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{total_servers} 台服务器")
                executor.shutdown(wait=False, cancel_futures=True)
                break
            svr = futures[future]
            done += 1
            try:
                cnt = future.result()
                if cnt > 0:
                    servers_with_orders.append(svr)
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ✅ {cnt} 条")
            except RetryableError as e:
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ⚠️ {e}，待重试", "warning")
                failed_servers.append(svr)
            except Exception as e:
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ❌ {e}", "error")

            if not task_manager.is_stopped(task_id):
                task_manager.update(task_id, progress=int(done / total_servers * 50),
                                    message=f"第一轮: {done}/{total_servers} 台服务器")

    if failed_servers and not task_manager.is_stopped(task_id):
        _order_log(task_id, f"\n{'='*50}")
        _order_log(task_id, f"开始重试失败的服务器 ({len(failed_servers)} 台)", "warning")
        _order_log(task_id, f"{'='*50}")

        for attempt in range(1, 4):
            if not failed_servers:
                break
            if task_manager.is_stopped(task_id):
                break

            _order_log(task_id, f"\n--- 第 {attempt}/3 次重试 ({len(failed_servers)} 台) ---")
            time.sleep(2)

            retry_failed = []
            retry_done = 0

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in failed_servers}
                for future in as_completed(futures):
                    if task_manager.is_stopped(task_id):
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    svr = futures[future]
                    retry_done += 1
                    try:
                        cnt = future.result()
                        if cnt > 0:
                            servers_with_orders.append(svr)
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ✅ {cnt} 条")
                    except RetryableError:
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ⚠️ 仍然失败", "warning")
                        retry_failed.append(svr)
                    except Exception as e:
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ❌ {e}", "error")

            failed_servers = retry_failed

            progress = 50 + int(attempt * 50 / 3)
            if not task_manager.is_stopped(task_id):
                task_manager.update(task_id, progress=progress,
                                    message=f"重试 {attempt}/3: 还剩 {len(failed_servers)} 台失败")

        if failed_servers:
            _order_log(task_id, f"\n{'='*50}", "error")
            _order_log(task_id, f"以下服务器 3 次重试均失败:", "error")
            _order_log(task_id, f"{'='*50}", "error")
            for svr in failed_servers:
                _order_log(task_id, f"  域名: {svr.get('domain', 'N/A'):<30} IP: {svr.get('ip', 'N/A')}", "error")
            _order_log(task_id, f"{'='*50}", "error")
            _order_log(task_id, f"共 {len(failed_servers)} 台服务器最终失败", "error")

    if not task_manager.is_stopped(task_id):
        success_count = total_servers - len(failed_servers)
        msg = f"第一阶段完成: 成功 {success_count}/{total_servers} 台服务器"
        if failed_servers:
            msg += f"，{len(failed_servers)} 台失败"
        task_manager.update(task_id, status="running", message=msg, progress=50)
        _order_log(task_id, f"\n{'='*50}")
        _order_log(task_id, f"第一阶段完成: {msg}")
        _order_log(task_id, f"{'='*50}")

    if not task_manager.is_stopped(task_id):
        task_manager.update(task_id, status="completed",
                            message=f"完成: 订单列表抓取", progress=100)

    q = _order_log_queues.get(task_id)
    if q: q.put({"done": True})
    _order_log_queues.pop(task_id, None)


def _run_fetch_by_ip(ip, year, month, task_id, date_from="", date_to=""):
    order_db = get_order_db()
    if not order_db:
        _order_log(task_id, "数据库连接失败", "error")
        task_manager.update(task_id, status="failed", message="数据库连接失败")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    servers = list(order_db.servers_col.find({"ip": ip}))
    if not servers:
        _order_log(task_id, f"未找到 IP {ip} 的服务器", "error")
        task_manager.update(task_id, status="failed", message=f"未找到 IP {ip} 的服务器")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    wp_password = ""
    try:
        from qmds.db.site_db import SiteDBClient
        site_db = SiteDBClient()
        settings = site_db.get_all_settings()
        site_db.close()
        wp_password = settings.get("wp_password", "")
    except:
        pass
    if not wp_password:
        wp_password = os.environ.get("WP_PASSWORD", "")
    if not wp_password:
        _order_log(task_id, "未配置 WordPress 密码，请在配置页面设置", "error")
        task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)
        return
    _order_log(task_id, f"开始并发抓取 IP {ip} ({len(servers)} 个域名, 5线程)")
    task_manager.update(task_id, status="running", message=f"开始抓取 IP {ip} ({len(servers)} 个域名)")

    failed_servers = []
    done = 0
    total_servers = len(servers)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _log_wrapper(msg, level="info"):
        _order_log(task_id, msg, level)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
        for future in as_completed(futures):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{total_servers} 个域名")
                executor.shutdown(wait=False, cancel_futures=True)
                break
            svr = futures[future]
            done += 1
            try:
                cnt = future.result()
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ✅ {cnt} 条")
            except RetryableError as e:
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ⚠️ {e}，待重试", "warning")
                failed_servers.append(svr)
            except Exception as e:
                _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ❌ {e}", "error")

            if not task_manager.is_stopped(task_id):
                task_manager.update(task_id, progress=int(done / total_servers * 50),
                                    message=f"第一轮: {done}/{total_servers} 个域名")

    if failed_servers and not task_manager.is_stopped(task_id):
        _order_log(task_id, f"\n{'='*50}")
        _order_log(task_id, f"开始重试失败的域名 ({len(failed_servers)} 个)", "warning")
        _order_log(task_id, f"{'='*50}")

        for attempt in range(1, 4):
            if not failed_servers:
                break
            if task_manager.is_stopped(task_id):
                break

            _order_log(task_id, f"\n--- 第 {attempt}/3 次重试 ({len(failed_servers)} 个) ---")
            time.sleep(2)

            retry_failed = []
            retry_done = 0

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in failed_servers}
                for future in as_completed(futures):
                    if task_manager.is_stopped(task_id):
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    svr = futures[future]
                    retry_done += 1
                    try:
                        cnt = future.result()
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ✅ {cnt} 条")
                    except RetryableError:
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ⚠️ 仍然失败", "warning")
                        retry_failed.append(svr)
                    except Exception as e:
                        _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ❌ {e}", "error")

            failed_servers = retry_failed

            progress = 50 + int(attempt * 50 / 3)
            if not task_manager.is_stopped(task_id):
                task_manager.update(task_id, progress=progress,
                                    message=f"重试 {attempt}/3: 还剩 {len(failed_servers)} 个失败")

        if failed_servers:
            _order_log(task_id, f"\n{'='*50}", "error")
            _order_log(task_id, f"以下域名 3 次重试均失败:", "error")
            _order_log(task_id, f"{'='*50}", "error")
            for svr in failed_servers:
                _order_log(task_id, f"  域名: {svr.get('domain', 'N/A'):<30} IP: {svr.get('ip', 'N/A')}", "error")
            _order_log(task_id, f"{'='*50}", "error")
            _order_log(task_id, f"共 {len(failed_servers)} 个域名最终失败", "error")

    if not task_manager.is_stopped(task_id):
        success_count = total_servers - len(failed_servers)
        msg = f"完成: 成功 {success_count}/{total_servers} 个域名"
        if failed_servers:
            msg += f"，{len(failed_servers)} 个失败"
        task_manager.update(task_id, status="completed", message=msg, progress=100)

    q = _order_log_queues.get(task_id)
    if q: q.put({"done": True})
    _order_log_queues.pop(task_id, None)


@bp.route("/orders")
def orders_page():
    """订单分析主页"""
    order_db = get_order_db()
    ips = order_db.get_all_ips() if order_db else []
    return render_template("orders.html", ips=ips)


@bp.route("/orders/list")
def orders_list_page():
    """商品维度订单列表页"""
    order_db = get_order_db()
    ips = order_db.get_all_ips() if order_db else []
    return render_template("orders_list.html", ips=ips)


@bp.route("/log-stream/<task_id>")
def order_log_stream(task_id):
    def generate():
        q = _order_log_queues.get(task_id)
        if q is None:
            yield f"data: {json.dumps({'msg': 'Task not found', 'level': 'error', 'done': True})}\n\n"
            return
        yield f"data: {json.dumps({'msg': '日志连接已建立', 'level': 'info'})}\n\n"
        idle = 0
        while True:
            if not task_manager.is_active(task_id) and q.empty():
                yield f"data: {json.dumps({'msg': '任务已结束', 'level': 'info', 'done': True})}\n\n"
                break
            try:
                entry = q.get(timeout=2)
                idle = 0
                yield f"data: {json.dumps(entry)}\n\n"
                if entry.get("done"):
                    break
            except Empty:
                idle += 1
                yield ": keepalive\n\n"
                if idle > 60:
                    yield f"data: {json.dumps({'msg': '连接超时自动关闭', 'level': 'warning', 'done': True})}\n\n"
                    break
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/ips", methods=["GET"])
def api_get_ips():
    order_db = get_order_db()
    if not order_db:
        return jsonify([])
    return jsonify(order_db.get_all_ips())


@bp.route("/api/servers", methods=["GET"])
def api_get_servers():
    order_db = get_order_db()
    if not order_db:
        return jsonify([])
    page = request.args.get("page", type=int)
    limit = request.args.get("limit", 100, type=int)
    return jsonify(order_db.get_servers(page, limit))


@bp.route("/api/servers", methods=["POST"])
def api_add_server():
    data = request.json or {}
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    try:
        server_id = order_db.add_server(data.get("domain", ""), data.get("ip", ""), data.get("main_category", ""))
        return jsonify({"ok": True, "id": server_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/servers/<int:server_id>", methods=["PUT"])
def api_update_server(server_id):
    data = request.json or {}
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    order_db.update_server(server_id, data)
    return jsonify({"ok": True})


@bp.route("/api/servers/<int:server_id>", methods=["DELETE"])
def api_delete_server(server_id):
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    order_db.delete_server(server_id)
    return jsonify({"ok": True})


@bp.route("/api/servers/delete-all", methods=["DELETE"])
def api_delete_all_servers():
    """删除所有服务器"""
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    try:
        deleted = order_db.delete_all_servers()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        log.error(f"删除所有服务器失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/orders/delete-all", methods=["DELETE"])
def api_delete_all_orders():
    """清空所有订单数据"""
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    try:
        deleted = order_db.delete_all_orders()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        log.error(f"清空所有订单失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/servers/sync", methods=["POST"])
def api_sync_servers():
    """从上报平台同步服务器数据"""
    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500
    try:
        from qmds.db.site_db import SiteDBClient
        site_db = SiteDBClient()
        settings = site_db.get_all_settings()
        site_db.close()
        username = settings.get("report_username", "")
        password = settings.get("report_password", "")
        if not username or not password:
            return jsonify({"error": "请先在配置页面设置上报账号和密码"}), 400
        from qmds.utils.domain_reporter import DomainReporter, REPORT_API_BASE_URL
        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
        categories = reporter.fetch_categories()
        log.info(f"获取到 {len(categories)} 个类目映射")
        domains = reporter.fetch_all_domains()
        if not domains:
            return jsonify({"error": "上报平台未返回域名数据"}), 400
        result = order_db.sync_from_reporter(domains, categories)
        return jsonify({"ok": True, **result})
    except Exception as e:
        log.error(f"同步服务器数据失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/fetch-all", methods=["POST"])
def api_fetch_all():
    data = request.json or {}
    year = int(data.get("year", datetime.now().year))
    month = int(data.get("month", datetime.now().month))
    date_from = data.get("date_from", "")
    date_to = data.get("date_to", "")
    task_id = f"fetch_{int(time.time())}"
    _order_log_queues[task_id] = Queue()
    task_manager.create(task_id, "fetch_all_orders", f"{year}-{month:02d}")
    task_manager.start_task_thread(task_id, partial(_run_fetch_all, year, month, task_id, date_from, date_to))
    return jsonify({"task_id": task_id})


@bp.route("/api/fetch-one", methods=["POST"])
def api_fetch_one():
    data = request.json or {}
    ip = data.get("ip", "")
    server_id = data.get("server_id")
    year = int(data.get("year", datetime.now().year))
    month = int(data.get("month", datetime.now().month))
    date_from = data.get("date_from", "")
    date_to = data.get("date_to", "")
    if not ip and server_id:
        order_db = get_order_db()
        if order_db:
            server = order_db.servers_col.find_one({"id": server_id}, {"_id": 0, "ip": 1})
            if server:
                ip = server.get("ip", "")
    if not ip:
        return jsonify({"error": "缺少 ip"}), 400
    task_id = f"fetch_{int(time.time())}"
    _order_log_queues[task_id] = Queue()
    task_manager.create(task_id, "fetch_orders_by_ip", ip)
    task_manager.start_task_thread(task_id, partial(_run_fetch_by_ip, ip, year, month, task_id, date_from, date_to))
    return jsonify({"task_id": task_id})


@bp.route("/api/orders", methods=["GET"])
def api_get_orders():
    order_db = get_order_db()
    if not order_db:
        return jsonify({"total": 0, "data": []})
    ip = request.args.get("ip", "")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 30, type=int)
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    sort_by = request.args.get("sort_by", "order_time")
    sort_order = request.args.get("sort_order", -1, type=int)
    return jsonify(order_db.get_orders(ip=ip, page=page, limit=limit, year=year, month=month, date_from=date_from, date_to=date_to, sort_by=sort_by, sort_order=sort_order))


@bp.route("/api/order-stats", methods=["GET"])
def api_order_stats():
    order_db = get_order_db()
    if not order_db:
        return jsonify([])
    ip = request.args.get("ip", "")
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    return jsonify(order_db.get_order_stats(ip=ip, year=year, month=month, date_from=date_from, date_to=date_to))


@bp.route("/api/order-status-stats", methods=["GET"])
def api_order_status_stats():
    order_db = get_order_db()
    if not order_db:
        return jsonify([])
    ip = request.args.get("ip", "")
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    return jsonify(order_db.get_order_status_stats(ip=ip, year=year, month=month, date_from=date_from, date_to=date_to))


@bp.route("/api/fetch-details", methods=["POST"])
def api_fetch_details():
    """异步获取订单详情"""
    data = request.json or {}
    year = int(data.get("year", datetime.now().year))
    month = int(data.get("month", datetime.now().month))
    ip = data.get("ip", "")

    task_id = f"details_{int(time.time())}"
    _order_log_queues[task_id] = Queue()
    task_manager.create(task_id, "fetch_order_details", f"{year}-{month:02d}")

    def run_task():
        order_db = get_order_db()
        if not order_db:
            _order_log(task_id, "数据库连接失败", "error")
            task_manager.update(task_id, status="failed", message="数据库连接失败")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            _order_log_queues.pop(task_id, None)
            return

        if ip:
            servers = list(order_db.servers_col.find({"ip": ip}))
        else:
            servers = order_db.get_servers()

        if not servers:
            _order_log(task_id, "没有配置服务器", "warn")
            task_manager.update(task_id, status="completed", message="没有配置服务器")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            _order_log_queues.pop(task_id, None)
            return

        wp_password = ""
        try:
            from qmds.db.site_db import SiteDBClient
            site_db = SiteDBClient()
            settings = site_db.get_all_settings()
            site_db.close()
            wp_password = settings.get("wp_password", "")
        except:
            pass
        if not wp_password:
            wp_password = os.environ.get("WP_PASSWORD", "")
        if not wp_password:
            _order_log(task_id, "未配置 WordPress 密码", "error")
            task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            _order_log_queues.pop(task_id, None)
            return

        _order_log(task_id, f"开始获取订单详情: {len(servers)} 台服务器")
        task_manager.update(task_id, status="running", message=f"处理中: {len(servers)} 台服务器")

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _log_wrapper(msg, level="info"):
            _order_log(task_id, msg, level)

        total_success = 0
        total_failed = 0
        total_dedup = 0
        done = 0

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {}
            for svr in servers:
                orders = order_db.get_orders_without_details(svr.get("ip", ""), svr.get("domain", ""), year, month)
                if orders:
                    futures[executor.submit(_fetch_order_details_for_server, svr, orders, _log_wrapper, wp_password, task_id)] = svr

            for future in as_completed(futures):
                if task_manager.is_stopped(task_id):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                svr = futures[future]
                done += 1

                try:
                    result = future.result()
                    total_success += result.get("success", 0)
                    total_failed += result.get("failed", 0)
                    total_dedup += result.get("deduplicated", 0)
                    _order_log(task_id, f"[{done}/{len(futures)}] [{svr.get('name', '')}] ✅ {result.get('success', 0)} 条")
                except RetryableError as e:
                    _order_log(task_id, f"[{done}/{len(futures)}] [{svr.get('name', '')}] ⚠️ {e}", "warning")
                except Exception as e:
                    _order_log(task_id, f"[{done}/{len(futures)}] [{svr.get('name', '')}] ❌ {e}", "error")

                task_manager.update(task_id, progress=int(done / len(futures) * 100),
                                    message=f"处理中: {done}/{len(futures)} 台服务器")

        if not task_manager.is_stopped(task_id):
            msg = f"完成: 成功 {total_success}, 失败 {total_failed}, 去重 {total_dedup}"
            task_manager.update(task_id, status="completed", message=msg, progress=100)
            _order_log(task_id, f"\n{'='*50}")
            _order_log(task_id, msg)
            _order_log(task_id, f"{'='*50}")

        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})
        _order_log_queues.pop(task_id, None)

    task_manager.start_task_thread(task_id, run_task)
    return jsonify({"task_id": task_id})


@bp.route("/api/deduplicate", methods=["POST"])
def api_deduplicate():
    """执行订单去重任务"""
    data = request.json or {}
    ip = data.get("ip", "")
    domain = data.get("domain", "")
    year = int(data["year"]) if data.get("year") is not None else None
    month = int(data["month"]) if data.get("month") is not None else None

    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500

    try:
        stats = order_db.get_dedup_stats(ip=ip, domain=domain, year=year, month=month)
        result = order_db.deduplicate_orders(ip=ip, domain=domain, year=year, month=month)

        return jsonify({
            "ok": True,
            "before": {
                "total_orders": stats.get("total_orders", 0),
                "potential_duplicates": stats.get("potential_duplicates", 0)
            },
            "result": result
        })
    except Exception as e:
        log.error(f"去重失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/dedup-stats", methods=["GET"])
def api_dedup_stats():
    """获取去重统计信息（不执行删除）"""
    ip = request.args.get("ip", "")
    domain = request.args.get("domain", "")
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)

    order_db = get_order_db()
    if not order_db:
        return jsonify({"error": "数据库连接失败"}), 500

    try:
        stats = order_db.get_dedup_stats(ip=ip, domain=domain, year=year, month=month)
        return jsonify(stats)
    except Exception as e:
        log.error(f"获取去重统计失败: {e}")
        return jsonify({"error": str(e)}), 500
