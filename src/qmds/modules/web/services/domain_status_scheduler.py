"""域名状态自动更新调度器

每 10 分钟检查一次当天上报的域名：
- 若存在域名状态为空的域名，则调用上报 API 同步其状态
- 当当天所有上报域名均已解析（status 2/3），设置 ready_to_build 标志供前端弹窗提醒
"""

import threading
import time
from datetime import datetime

from qmds.utils.logger import get_logger
from qmds.utils.domain_reporter import DomainReporter, DOMAIN_STATUS_LABELS, REPORT_API_BASE_URL
from qmds.utils.desktop_notifier import send_desktop_notification
from qmds.db.site_db import SiteDBClient

log = get_logger("domain_status_scheduler")

INTERVAL_SECONDS = 600  # 10 分钟


class DomainStatusScheduler:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        # 自动更新状态
        self._last_run_time = None
        self._last_result = None  # dict: {checked, updated, failed, ready_to_build, message}
        # 当天 ready_to_build 标志（重置：跨天或手动重置）
        self._ready_date = None  # 触发 ready 的日期 (date 对象)
        self._ready_notified = False

    def start(self):
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="domain_status_scheduler")
            self._thread.start()
            self._running = True
            log.info("域名状态自动更新调度器已启动 (间隔 %ds)", INTERVAL_SECONDS)

    def stop(self):
        self._stop_event.set()
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def _loop(self):
        # 启动后先等待一小段，避免与 Web 初始化抢资源
        time.sleep(15)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error(f"域名状态自动更新异常: {e}")
            # 等待间隔，每秒检查 stop 信号以可快速退出
            for _ in range(INTERVAL_SECONDS):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _tick(self):
        site_db = SiteDBClient()
        try:
            today = datetime.utcnow().date()
            # 跨天重置 ready 标志
            if self._ready_date != today:
                self._ready_date = None
                self._ready_notified = False

            domains = site_db.list_domains_with_empty_status_today()
            if not domains:
                # 没有需要更新的，但仍检查是否全部已解析
                result = site_db.check_all_today_reported_resolved()
                self._last_run_time = datetime.now().isoformat()
                self._last_result = {
                    "checked": 0, "updated": 0, "failed": 0,
                    "ready_to_build": result["ready_to_build"],
                    "has_empty": result["has_empty"],
                    "total": result["total"],
                    "resolved": result["resolved"],
                    "message": "无可更新域名",
                }
                if result["ready_to_build"] and not self._ready_notified:
                    self._ready_date = today
                    self._ready_notified = True
                    self._notify_ready(result)
                log.info(f"域名状态自动更新: 无待更新域名，今日上报 {result['total']}，已解析 {result['resolved']}")
                return

            # 获取上报账号配置
            settings = site_db.get_all_settings()
            username = settings.get("report_username", "")
            password = settings.get("report_password", "")
            if not username or not password:
                self._last_run_time = datetime.now().isoformat()
                self._last_result = {
                    "checked": len(domains), "updated": 0, "failed": len(domains),
                    "ready_to_build": False, "has_empty": True,
                    "total": 0, "resolved": 0,
                    "message": "未配置上报账号密码，无法自动更新",
                }
                log.warning("域名状态自动更新: 未配置上报账号密码")
                return

            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
            updated = 0
            failed = 0
            for d in domains:
                domain = d.get("domain", "")
                if not domain:
                    failed += 1
                    continue
                try:
                    info = reporter.fetch_domain_info(domain)
                    report_id = str(info.get("id") or "")
                    status_val = info.get("status")
                    status_label = DOMAIN_STATUS_LABELS.get(status_val, "未知")
                    site_db.update_domain_status(domain, report_id,
                                                 str(status_val) if status_val is not None else "")
                    updated += 1
                    log.info(f"域名状态自动更新: {domain} -> {status_label}")
                except Exception as e:
                    failed += 1
                    log.warning(f"域名状态自动更新失败: {domain} - {e}")

            # 更新后重新检查是否全部已解析
            result = site_db.check_all_today_reported_resolved()
            self._last_run_time = datetime.now().isoformat()
            self._last_result = {
                "checked": len(domains), "updated": updated, "failed": failed,
                "ready_to_build": result["ready_to_build"],
                "has_empty": result["has_empty"],
                "total": result["total"],
                "resolved": result["resolved"],
                "message": f"已更新 {updated}/{len(domains)}",
            }
            if result["ready_to_build"] and not self._ready_notified:
                self._ready_date = today
                self._ready_notified = True
                self._notify_ready(result)
            log.info(f"域名状态自动更新完成: 更新 {updated}, 失败 {failed}, "
                     f"今日上报 {result['total']}, 已解析 {result['resolved']}, "
                     f"可建站 {result['ready_to_build']}")
        finally:
            site_db.close()

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "last_run_time": self._last_run_time,
                "last_result": self._last_result,
                "ready_notified": self._ready_notified,
                "ready_date": str(self._ready_date) if self._ready_date else None,
            }

    def _notify_ready(self, result: dict):
        """域名全部解析完成时发送桌面弹窗通知（无需打开网页即可看到）"""
        try:
            send_desktop_notification(
                title="QMDS 域名解析完成",
                message=f"当天上报的 {result.get('total', 0)} 个域名已全部解析完成，可以建站了！",
                dedupe=False,
            )
        except Exception as e:
            log.error(f"发送桌面通知失败: {e}")

    def reset_ready_notified(self):
        """前端用户确认后重置提醒标志，避免重复弹窗"""
        with self._lock:
            self._ready_notified = False
            self._ready_date = None


scheduler = DomainStatusScheduler()


def start_domain_status_scheduler():
    scheduler.start()
