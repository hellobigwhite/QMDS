"""订单定时任务调度器 - 每天早上8:00自动更新订单信息"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Callable
from queue import Queue

from qmds.utils.logger import get_logger

log = get_logger("order_scheduler")


class RetryableError(Exception):
    """可重试的错误（登录失败、请求超时等）"""
    pass


class OrderScheduler:
    """订单定时任务调度器"""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._log_queues: dict[str, Queue] = {}
        self._last_run: Optional[datetime] = None
        self._next_run: Optional[datetime] = None
        self._status = "stopped"
        self._run_count = 0
        self._last_error: Optional[str] = None
        self._schedule_hour = 8
        self._schedule_minute = 0
        self._update_next_run_time()

    def _update_next_run_time(self):
        """计算下次运行时间"""
        now = datetime.now()
        scheduled_time = now.replace(
            hour=self._schedule_hour,
            minute=self._schedule_minute,
            second=0,
            microsecond=0
        )
        if scheduled_time <= now:
            scheduled_time += timedelta(days=1)
        self._next_run = scheduled_time

    def _log(self, task_id: str, msg: str, level: str = "info"):
        """发送日志到队列"""
        q = self._log_queues.get(task_id)
        if q is not None:
            q.put({
                "msg": msg,
                "level": level,
                "time": time.strftime("%H:%M:%S")
            })
        if level == "error":
            log.error(msg)
        else:
            log.info(msg)

    def _derive_domain(self, domain: str) -> str:
        """标准化域名"""
        d = domain.strip().lower()
        if not d.startswith("www."):
            d = "www." + d
        return d

    def _wp_login(self, session, domain: str, password: str, max_retries: int = 2):
        """WordPress 登录"""
        from bs4 import BeautifulSoup
        site_url = f"https://{self._derive_domain(domain)}"
        name = domain.replace('www.', '').replace('.com', '').strip()
        username = f"Ad{name}Min"
        login_url = f"{site_url}/bbwllogin/"
        data = {
            "log": username, 
            "pwd": password, 
            "wp-submit": "Log In",
            "redirect_to": f"{site_url}/wp-admin/", 
            "testcookie": "1"
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}
        
        for attempt in range(max_retries):
            try:
                session.post(login_url, data=data, headers=headers, verify=False, timeout=10)
                logged_in = any("wordpress_logged_in" in c.name for c in session.cookies)
                if not logged_in:
                    check = session.get(f"{site_url}/wp-admin/", verify=False, timeout=10)
                    logged_in = check.status_code == 200 and "wp-admin" in check.url
                if logged_in:
                    return username
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"WP login failed for {domain}: {e}")
        raise RuntimeError(f"WP login failed for {domain}")

    def _parse_order_row(self, row_html: str) -> Optional[dict]:
        """解析订单行HTML"""
        import re
        import html as html_mod
        m = re.search(r'<tr[^>]*id="order-(\d+)"', row_html)
        if not m:
            return None
        order_id = int(m.group(1))
        date_created = None
        m3 = re.search(r'<time datetime="([^"]+)"', row_html)
        if m3:
            dt_src = m3.group(1).replace('T', ' ')
            if '+' in dt_src: 
                dt_src = dt_src[:dt_src.index('+')]
            if 'Z' in dt_src: 
                dt_src = dt_src.replace('Z', '')
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
        return {
            "order_id": order_id, 
            "order_time": date_created, 
            "order_status": status, 
            "order_amount": total
        }

    def _fetch_server_orders(self, server: dict, year: int, month: int, task_id: str, wp_password: str) -> int:
        """抓取单个服务器的订单数据"""
        import requests as req
        import re
        req.packages.urllib3.disable_warnings()
        
        domain = self._derive_domain(server["domain"])
        site_url = f"https://{domain}"
        ip = server.get("ip", "")
        
        def log_func(msg, level="info"):
            self._log(task_id, f"[{server.get('name', '')}] {msg}", level)
        
        if not ip:
            log_func("无IP，跳过", "error")
            return 0
        
        from qmds.db.order_db import OrderDBClient
        order_db = OrderDBClient()
        if not order_db:
            log_func("数据库连接失败", "error")
            return 0
        
        order_db.ensure_orders_indexes(ip)
        m_param = f"{year}{month:02d}"
        page = 1
        total_fetched = 0
        session = req.Session()
        session.verify = False
        
        try:
            self._wp_login(session, domain, wp_password)
            log_func("Login OK")
        except RuntimeError as e:
            log_func(f"{e}", "error")
            raise RetryableError(f"登录失败: {e}")
        
        while True:
            log_func(f"第 {page} 页...")
            url = f"{site_url}/wp-admin/admin.php?page=wc-orders&m={m_param}&paged={page}"
            try:
                r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            except Exception as e:
                log_func(f"请求失败: {e}", "error")
                raise RetryableError(f"请求失败: {e}")
            if r.status_code != 200:
                log_func(f"HTTP {r.status_code}", "error")
                break
            rows = list(re.finditer(r'<tr[^>]*id="order-\d+"[^>]*>.*?</tr>', r.text, re.S))
            log_func(f"解析到 {len(rows)} 行")
            parsed_orders = []
            for m in rows:
                parsed = self._parse_order_row(m.group(0))
                if not parsed:
                    continue
                status_lower = (parsed.get("order_status") or "").lower()
                if status_lower in ("on hold", "on-hold"):
                    continue
                parsed_orders.append(parsed)
            if not parsed_orders:
                log_func(f"当前页无真实订单数据，跳过剩余页面")
                break
            for parsed in parsed_orders:
                order_db.insert_order(
                    ip, 
                    server["domain"], 
                    parsed["order_time"], 
                    parsed["order_status"], 
                    parsed["order_amount"], 
                    server.get("main_category", "")
                )
            total_fetched += len(parsed_orders)
            log_func(f"写入 {len(parsed_orders)} 条")
            page += 1
            time.sleep(0.5)
        return total_fetched

    def _execute_order_update(self, task_id: str):
        """执行订单更新任务"""
        try:
            self._status = "running"
            self._last_error = None
            self._log(task_id, "开始执行每日订单更新任务")

            from qmds.db.order_db import OrderDBClient
            from qmds.db.site_db import SiteDBClient

            # 获取数据库连接
            order_db = OrderDBClient()
            if not order_db:
                self._log(task_id, "数据库连接失败", "error")
                self._status = "error"
                self._last_error = "数据库连接失败"
                return

            # 获取服务器列表
            servers = order_db.get_servers()
            if not servers:
                self._log(task_id, "没有配置任何服务器", "warn")
                self._status = "idle"
                return

            # 读取 WordPress 密码
            wp_password = ""
            try:
                site_db = SiteDBClient()
                settings = site_db.get_all_settings()
                site_db.close()
                wp_password = settings.get("wp_password", "")
            except Exception as e:
                self._log(task_id, f"读取配置失败: {e}", "warn")

            if not wp_password:
                import os
                wp_password = os.environ.get("WP_PASSWORD", "")

            if not wp_password:
                self._log(task_id, "未配置 WordPress 密码", "error")
                self._status = "error"
                self._last_error = "未配置 WordPress 密码"
                return

            # 获取当前年月
            now = datetime.now()
            year = now.year
            month = now.month

            self._log(task_id, f"开始抓取 {year}-{month:02d} 订单数据，共 {len(servers)} 台服务器")

            # 并发抓取订单
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_orders_for_server(server):
                """抓取单个服务器的订单"""
                return self._fetch_server_orders(server, year, month, task_id, wp_password)

            failed_servers = []
            servers_with_orders = []  # 记录有订单的服务器
            success_count = 0
            fail_count = 0

            # 第一轮抓取
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(_fetch_orders_for_server, svr): svr 
                    for svr in servers
                }
                for future in as_completed(futures):
                    svr = futures[future]
                    try:
                        cnt = future.result()
                        if cnt > 0:  # 记录有订单的服务器
                            servers_with_orders.append(svr)
                        success_count += 1
                        self._log(task_id, f"[{svr.get('name', '')}] 完成: {cnt} 条订单")
                    except RetryableError as e:
                        fail_count += 1
                        self._log(task_id, f"[{svr.get('name', '')}] {e}，待重试", "warning")
                        failed_servers.append(svr)
                    except Exception as e:
                        fail_count += 1
                        self._log(task_id, f"[{svr.get('name', '')}] 失败: {e}", "error")

            # 重试失败的服务器（最多3次）
            if failed_servers:
                self._log(task_id, f"\n{'='*50}")
                self._log(task_id, f"开始重试失败的服务器 ({len(failed_servers)} 台)", "warning")
                self._log(task_id, f"{'='*50}")
                
                for attempt in range(1, 4):
                    if not failed_servers:
                        break
                    
                    self._log(task_id, f"\n--- 第 {attempt}/3 次重试 ({len(failed_servers)} 台) ---")
                    time.sleep(2)  # 重试前等待2秒
                    
                    retry_failed = []
                    
                    with ThreadPoolExecutor(max_workers=5) as executor:
                        futures = {
                            executor.submit(_fetch_orders_for_server, svr): svr 
                            for svr in failed_servers
                        }
                        for future in as_completed(futures):
                            svr = futures[future]
                            try:
                                cnt = future.result()
                                if cnt > 0:  # 重试成功且有订单，记录到有订单列表
                                    servers_with_orders.append(svr)
                                success_count += 1
                                fail_count -= 1
                                self._log(task_id, f"[重试{attempt}] [{svr.get('name', '')}] 完成: {cnt} 条订单")
                            except RetryableError:
                                retry_failed.append(svr)
                                self._log(task_id, f"[重试{attempt}] [{svr.get('name', '')}] 仍然失败", "warning")
                            except Exception as e:
                                self._log(task_id, f"[重试{attempt}] [{svr.get('name', '')}] 失败: {e}", "error")
                    
                    failed_servers = retry_failed
                
                # 输出最终失败列表
                if failed_servers:
                    self._log(task_id, f"\n{'='*50}", "error")
                    self._log(task_id, f"以下服务器 3 次重试均失败:", "error")
                    self._log(task_id, f"{'='*50}", "error")
                    for svr in failed_servers:
                        self._log(task_id, f"  域名: {svr.get('domain', 'N/A'):<30} IP: {svr.get('ip', 'N/A')}", "error")
                    self._log(task_id, f"{'='*50}", "error")
                    self._log(task_id, f"共 {len(failed_servers)} 台服务器最终失败", "error")

            self._log(task_id, f"第一阶段完成: 成功 {success_count} 台, 失败 {fail_count} 台")
            
            # 第二阶段：异步获取订单详情
            self._log(task_id, f"\n{'='*50}")
            self._log(task_id, f"第二阶段: 获取订单详情（{len(servers_with_orders)} 台有订单的服务器）")
            self._log(task_id, f"{'='*50}")
            
            detail_success = 0
            detail_failed = 0
            detail_dedup = 0
            detail_servers_done = 0
            
            for svr in servers_with_orders:  # 只处理有订单的服务器
                ip = svr.get("ip", "")
                domain = svr.get("domain", "")
                
                if not ip:
                    continue
                
                # 获取没有详情的订单
                orders_without_details = order_db.get_orders_without_details(ip, domain, year, month)
                
                if not orders_without_details:
                    self._log(task_id, f"[{svr.get('name', '')}] 无需获取详情")
                    detail_servers_done += 1
                    continue
                
                self._log(task_id, f"[{svr.get('name', '')}] 待处理: {len(orders_without_details)} 个订单")
                
                try:
                    # 复用 engine.py 中的详情获取函数
                    from qmds.modules.web.engine import create_app
                    # 由于 scheduler 是独立模块，需要直接实现详情获取逻辑
                    
                    import requests as req
                    req.packages.urllib3.disable_warnings()
                    
                    session = req.Session()
                    session.verify = False
                    
                    # 登录
                    site_url = f"https://{domain}"
                    name = domain.replace('www.', '').replace('.com', '').strip()
                    username = f"Ad{name}Min"
                    login_url = f"{site_url}/bbwllogin/"
                    login_data = {
                        "log": username, 
                        "pwd": wp_password, 
                        "wp-submit": "Log In",
                        "redirect_to": f"{site_url}/wp-admin/", 
                        "testcookie": "1"
                    }
                    headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}
                    
                    session.post(login_url, data=login_data, headers=headers, verify=False, timeout=10)
                    logged_in = any("wordpress_logged_in" in c.name for c in session.cookies)
                    
                    if not logged_in:
                        check = session.get(f"{site_url}/wp-admin/", verify=False, timeout=10)
                        logged_in = check.status_code == 200 and "wp-admin" in check.url
                    
                    if not logged_in:
                        self._log(task_id, f"[{svr.get('name', '')}] 登录失败", "warning")
                        detail_servers_done += 1
                        continue
                    
                    self._log(task_id, f"[{svr.get('name', '')}] Login OK")
                    
                    import re
                    from bs4 import BeautifulSoup
                    
                    for i, order in enumerate(orders_without_details, 1):
                        order_time = order.get("order_time", "")
                        
                        # 通过订单时间搜索订单ID
                        search_url = f"{site_url}/wp-admin/edit.php?post_type=shop_order&s={order_time}"
                        try:
                            r = session.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                            if r.status_code == 200:
                                order_id_match = re.search(r'post=(\d+)', r.text)
                                if order_id_match:
                                    order_id = order_id_match.group(1)
                                    
                                    # 访问订单详情页
                                    detail_url = f"{site_url}/wp-admin/post.php?post={order_id}&action=edit"
                                    detail_resp = session.get(detail_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                                    
                                    if detail_resp.status_code == 200:
                                        # 解析详情
                                        soup = BeautifulSoup(detail_resp.text, "html.parser")
                                        
                                        # 解析商品信息
                                        items = []
                                        items_table = soup.find("table", class_="woocommerce_order_items")
                                        if items_table:
                                            tbody = items_table.find("tbody")
                                            if tbody:
                                                for row in tbody.find_all("tr"):
                                                    item = {}
                                                    name_cell = row.find("td", class_="name")
                                                    if name_cell:
                                                        name_link = name_cell.find("a")
                                                        if name_link:
                                                            item["product_name"] = name_link.get_text(strip=True)
                                                        
                                                        sku_div = name_cell.find("div", class_="view") or name_cell.find("small")
                                                        if sku_div:
                                                            sku_text = sku_div.get_text(strip=True)
                                                            if "SKU" in sku_text:
                                                                item["sku"] = sku_text.replace("SKU:", "").strip()
                                                    
                                                    qty_cell = row.find("td", class_="quantity")
                                                    if qty_cell:
                                                        try:
                                                            item["quantity"] = int(qty_cell.get_text(strip=True))
                                                        except ValueError:
                                                            item["quantity"] = 1
                                                    
                                                    total_cell = row.find("td", class_="line_total") or row.find("td", class_="line_cost")
                                                    if total_cell:
                                                        nums = re.findall(r'[\d,.]+', total_cell.get_text(strip=True))
                                                        if nums:
                                                            try:
                                                                item["subtotal"] = float(nums[-1].replace(",", ""))
                                                            except ValueError:
                                                                item["subtotal"] = 0
                                                    
                                                    if item.get("product_name"):
                                                        items.append(item)
                                        
                                        # 解析邮箱
                                        customer_email = ""
                                        email_link = soup.find("a", href=re.compile(r"^mailto:"))
                                        if email_link:
                                            customer_email = email_link.get("href", "").replace("mailto:", "").strip()
                                        
                                        if not customer_email:
                                            # 备用解析
                                            order_data_columns = soup.find_all("div", class_="order_data_column")
                                            for column in order_data_columns:
                                                text = column.get_text()
                                                if "@" in text:
                                                    email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', text)
                                                    if email_match:
                                                        customer_email = email_match.group(0)
                                                        break
                                        
                                        # 更新数据库
                                        result = order_db.update_order_details(
                                            ip=ip,
                                            domain=domain,
                                            order_time=order_time,
                                            customer_email=customer_email,
                                            order_amount=order.get("order_amount", 0),
                                            items=items
                                        )
                                        
                                        if result.get("updated"):
                                            detail_success += 1
                                        
                                        if result.get("deduplicated", 0) > 0:
                                            detail_dedup += result["deduplicated"]
                                            self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ⚠️ 去重: 删除 {result['deduplicated']} 条")
                                        else:
                                            self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ✅ {len(items)}件商品, 邮箱: {customer_email or 'N/A'}")
                                    else:
                                        detail_failed += 1
                                        self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ❌ 详情页访问失败")
                                else:
                                    detail_failed += 1
                                    self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ❌ 未找到订单ID")
                            else:
                                detail_failed += 1
                                self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ❌ 搜索失败")
                        except Exception as e:
                            detail_failed += 1
                            self._log(task_id, f"[{svr.get('name', '')}] [{i}/{len(orders_without_details)}] ❌ {e}")
                        
                        time.sleep(0.5)
                    
                except Exception as e:
                    self._log(task_id, f"[{svr.get('name', '')}] ❌ {e}", "error")
                
                detail_servers_done += 1
            
            # 输出第二阶段统计
            self._log(task_id, f"\n{'='*50}")
            self._log(task_id, f"第二阶段完成:")
            self._log(task_id, f"  详情更新成功: {detail_success} 条")
            self._log(task_id, f"  详情更新失败: {detail_failed} 条")
            self._log(task_id, f"  去重删除: {detail_dedup} 条")
            self._log(task_id, f"{'='*50}")
            
            self._status = "idle"
            self._run_count += 1
            self._last_run = datetime.now()
            self._update_next_run_time()

        except Exception as e:
            self._log(task_id, f"订单更新任务异常: {e}", "error")
            self._status = "error"
            self._last_error = str(e)
        finally:
            # 清理日志队列
            if task_id in self._log_queues:
                q = self._log_queues[task_id]
                q.put({"done": True})
                # 延迟清理，让客户端有时间读取
                threading.Timer(60, lambda: self._log_queues.pop(task_id, None)).start()

    def _scheduler_loop(self):
        """调度器主循环"""
        log.info(f"订单定时任务调度器已启动，每天 {self._schedule_hour:02d}:{self._schedule_minute:02d} 执行")
        
        while self._running:
            try:
                now = datetime.now()
                
                # 检查是否到达执行时间
                if (now.hour == self._schedule_hour and 
                    now.minute == self._schedule_minute and
                    (self._last_run is None or (now - self._last_run).total_seconds() > 60)):
                    
                    task_id = f"scheduled_{int(time.time())}"
                    self._log_queues[task_id] = Queue()
                    
                    # 在新线程中执行任务
                    t = threading.Thread(
                        target=self._execute_order_update,
                        args=(task_id,),
                        daemon=True
                    )
                    t.start()
                    
                    # 等待一分钟，避免重复触发
                    time.sleep(60)
                
                # 每10秒检查一次
                time.sleep(10)
                
            except Exception as e:
                log.error(f"调度器循环异常: {e}")
                time.sleep(30)

    def start(self):
        """启动调度器"""
        with self._lock:
            if self._running:
                log.warning("调度器已在运行")
                return
            
            self._running = True
            self._status = "running"
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name="order-scheduler",
                daemon=True
            )
            self._thread.start()
            log.info("订单定时任务调度器已启动")

    def stop(self):
        """停止调度器"""
        with self._lock:
            if not self._running:
                return
            
            self._running = False
            self._status = "stopped"
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None
            log.info("订单定时任务调度器已停止")

    def run_now(self) -> str:
        """立即执行一次订单更新"""
        task_id = f"manual_{int(time.time())}"
        self._log_queues[task_id] = Queue()
        
        t = threading.Thread(
            target=self._execute_order_update,
            args=(task_id,),
            daemon=True
        )
        t.start()
        return task_id

    def get_status(self) -> dict:
        """获取调度器状态"""
        return {
            "running": self._running,
            "status": self._status,
            "schedule": f"{self._schedule_hour:02d}:{self._schedule_minute:02d}",
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "next_run": self._next_run.isoformat() if self._next_run else None,
            "run_count": self._run_count,
            "last_error": self._last_error,
        }

    def set_schedule(self, hour: int, minute: int):
        """设置执行时间"""
        with self._lock:
            self._schedule_hour = hour
            self._schedule_minute = minute
            self._update_next_run_time()
            log.info(f"定时任务已设置为每天 {hour:02d}:{minute:02d}")

    def get_log_queue(self, task_id: str) -> Optional[Queue]:
        """获取任务日志队列"""
        return self._log_queues.get(task_id)


# 全局调度器实例
_order_scheduler: Optional[OrderScheduler] = None


def get_order_scheduler() -> OrderScheduler:
    """获取全局订单调度器实例"""
    global _order_scheduler
    if _order_scheduler is None:
        _order_scheduler = OrderScheduler()
    return _order_scheduler
