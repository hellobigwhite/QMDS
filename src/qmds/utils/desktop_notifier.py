"""桌面通知工具

在无需打开网页的情况下，从后台线程/调度器直接发送系统级弹窗提醒。
- Windows: 优先使用 win10toast / winotify 显示 Toast；不可用时回退到 MessageBox 弹窗
- 其他平台: 尝试 plyer，均不可用时仅记录日志
"""

import threading

from qmds.utils.logger import get_logger

log = get_logger("desktop_notifier")

_notified_lock = threading.Lock()
_notified_messages = set()


def _notify_windows_toast(title: str, message: str) -> bool:
    try:
        from win10toast import Win10Toast
        Win10Toast().show_toast(title, message, duration=10, threaded=True)
        return True
    except ImportError:
        pass
    try:
        from winotify import Notification
        Notification(app_id="QMDS", title=title, msg=message).show()
        return True
    except ImportError:
        pass
    return False


def _notify_windows_messagebox(title: str, message: str):
    import ctypes
    MB_SYSTEMMODAL = 0x1000
    MB_ICONINFORMATION = 0x40
    MB_TOPMOST = 0x40000
    ctypes.windll.user32.MessageBoxW(0, message, title,
                                     MB_SYSTEMMODAL | MB_ICONINFORMATION | MB_TOPMOST)


def _notify_plyer(title: str, message: str) -> bool:
    try:
        from plyer import notification
        notification.notify(title=title, message=message)
        return True
    except ImportError:
        pass
    return False


def send_desktop_notification(title: str, message: str, dedupe: bool = True):
    """发送桌面弹窗通知（非阻塞，在独立线程执行）

    Args:
        title: 通知标题
        message: 通知内容
        dedupe: 相同内容是否只提醒一次（进程生命周期内）
    """
    if dedupe:
        key = f"{title}|{message}"
        with _notified_lock:
            if key in _notified_messages:
                return
            _notified_messages.add(key)

    def _run():
        try:
            import sys
            if sys.platform == "win32":
                if _notify_windows_toast(title, message):
                    return
                _notify_windows_messagebox(title, message)
            else:
                if not _notify_plyer(title, message):
                    log.warning(f"桌面通知发送失败（无可用通知后端）: {title} - {message}")
        except Exception as e:
            log.error(f"桌面通知异常: {e}")

    threading.Thread(target=_run, daemon=True, name="desktop_notifier").start()
