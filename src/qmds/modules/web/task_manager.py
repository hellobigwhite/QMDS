"""任务管理器 — 线程安全的后台任务状态跟踪"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from qmds.utils.logger import get_logger

log = get_logger("task_manager")


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._logs: dict[str, list] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def create(self, task_id: str, action: str, target: str) -> str:
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id, "action": action, "target": target,
                "status": "running", "progress": 0, "current": 0, "total": 0,
                "message": "Starting...", "result": None, "error": None,
                "created_at": datetime.now().isoformat(),
            }
            self._stop_events[task_id] = threading.Event()
            self._logs[task_id] = []
        return task_id

    def register_thread(self, task_id: str, thread: threading.Thread):
        with self._lock:
            self._threads[task_id] = thread

    def start_task_thread(self, task_id: str, target, name: str = None) -> threading.Thread:
        """统一的后台任务线程启动入口：自动包装 try/finally，结束后即时清理线程引用。"""
        def _wrapped():
            try:
                target()
            except Exception as e:
                log.error(f"[{task_id}] 任务线程未捕获异常: {e}")
                self.update(task_id, status="failed", message=f"失败: {e}")
            finally:
                with self._lock:
                    self._threads.pop(task_id, None)

        thread = threading.Thread(target=_wrapped, daemon=True, name=name or f"task_{task_id}")
        self.register_thread(task_id, thread)
        thread.start()
        return thread

    def update(self, task_id: str, **kwargs):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(kwargs)

    def add_log(self, task_id: str, message: str, level: str = "info"):
        with self._lock:
            if task_id in self._logs:
                self._logs[task_id].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": level, "message": message,
                })
                if len(self._logs[task_id]) > 500:
                    self._logs[task_id] = self._logs[task_id][-500:]
        log_func = getattr(log, level, log.info)
        log_func(f"[{task_id}] {message}")

    def get_logs(self, task_id: str, limit: int = 100) -> list:
        with self._lock:
            if task_id in self._logs:
                return self._logs[task_id][-limit:]
            return []

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t["created_at"], reverse=True)[:50]

    def count_by_status(self) -> dict:
        with self._lock:
            counts = {"running": 0, "completed": 0, "failed": 0, "stopped": 0}
            today = datetime.now().date()
            for t in self._tasks.values():
                status = t.get("status", "")
                if status == "running":
                    counts["running"] += 1
                elif status == "completed":
                    counts["completed"] += 1
                elif status == "failed":
                    counts["failed"] += 1
                elif status in ("stopped", "stopping"):
                    counts["stopped"] += 1
            return counts

    def stop(self, task_id: str, join_timeout: float = 5.0) -> bool:
        """请求任务停止，并等待工作线程实际退出（最多 join_timeout 秒），
        确保手动停止时线程被即时回收。"""
        with self._lock:
            event = self._stop_events.get(task_id)
            thread = self._threads.get(task_id)
            if event is None:
                return False
            event.set()
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "stopping"
                self._tasks[task_id]["message"] = "正在停止..."
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                log.warning(f"[{task_id}] 线程在 {join_timeout}s 后仍未退出（可能阻塞于 IO）")
            else:
                log.info(f"[{task_id}] 工作线程已退出")
                with self._lock:
                    self._threads.pop(task_id, None)
        return True

    def is_stopped(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._stop_events:
                return self._stop_events[task_id].is_set()
            return False

    def get_stop_event(self, task_id: str):
        with self._lock:
            return self._stop_events.get(task_id)

    def cleanup(self, max_age_hours: int = 1):
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._lock:
            to_remove = [
                k for k, v in self._tasks.items()
                if v.get("status") in ("completed", "failed", "stopped")
                and datetime.fromisoformat(v["created_at"]) < cutoff
            ]
            for tid in to_remove:
                self._tasks.pop(tid, None)
                self._stop_events.pop(tid, None)
                self._logs.pop(tid, None)
                self._threads.pop(tid, None)
            if to_remove:
                log.info(f"清理了 {len(to_remove)} 个已完成任务")

    def is_active(self, task_id: str) -> bool:
        """任务是否仍在运行（线程存活且状态为 running/stopping）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            if t.get("status") not in ("running", "stopping"):
                return False
            thread = self._threads.get(task_id)
            if thread is None:
                return False
            return thread.is_alive()


task_manager = TaskManager()


def start_cleanup_scheduler():
    def cleanup_loop():
        while True:
            try:
                time.sleep(300)
                task_manager.cleanup(max_age_hours=1)
            except Exception as e:
                log.error(f"清理任务异常: {e}")
    t = threading.Thread(target=cleanup_loop, daemon=True, name="cleanup_scheduler")
    t.start()


def make_progress_callback(task_id: str):
    """创建统一的进度回调函数"""
    def cb(info):
        if task_manager.is_stopped(task_id):
            raise InterruptedError("任务被用户停止")
        if isinstance(info, dict):
            update = {k: v for k, v in info.items() if k in ("progress", "current", "total", "message")}
            if update:
                task_manager.update(task_id, **update)
            if info.get("message"):
                task_manager.add_log(task_id, info["message"], "info")
        else:
            task_manager.update(task_id, message=str(info))
            task_manager.add_log(task_id, str(info), "info")
    return cb
