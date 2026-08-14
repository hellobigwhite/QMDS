"""定时任务调度路由"""

import json
import time

from flask import Blueprint, Response, jsonify, request

from qmds.utils.logger import get_logger

log = get_logger("web.scheduler")

bp = Blueprint("scheduler", __name__)


def _get_scheduler():
    from qmds.modules.order_checker.scheduler import get_order_scheduler
    return get_order_scheduler()


@bp.route("/api/scheduler/status")
def api_scheduler_status():
    scheduler = _get_scheduler()
    return jsonify(scheduler.get_status())


@bp.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    scheduler = _get_scheduler()
    scheduler.start()
    return jsonify({"ok": True, "message": "定时任务已启动"})


@bp.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    scheduler = _get_scheduler()
    scheduler.stop()
    return jsonify({"ok": True, "message": "定时任务已停止"})


@bp.route("/api/scheduler/run-now", methods=["POST"])
def api_scheduler_run_now():
    scheduler = _get_scheduler()
    result = scheduler.run_now()
    return jsonify({"ok": True, "result": result})


@bp.route("/api/scheduler/set-time", methods=["POST"])
def api_scheduler_set_time():
    data = request.json or {}
    hour = data.get("hour", type=int)
    minute = data.get("minute", type=int)
    if hour is None or minute is None:
        return jsonify({"ok": False, "error": "请提供 hour 和 minute"}), 400
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return jsonify({"ok": False, "error": "时间格式无效"}), 400
    scheduler = _get_scheduler()
    scheduler.set_daily_time(hour, minute)
    return jsonify({"ok": True, "message": f"定时任务已设置为每天 {hour:02d}:{minute:02d}"})


@bp.route("/api/scheduler/log/<task_id>")
def api_scheduler_log(task_id):
    scheduler = _get_scheduler()

    def generate():
        q = scheduler.get_log_queue(task_id)
        if q is None:
            yield f"data: {json.dumps({'msg': 'Task not found', 'level': 'error'})}\n\n"
            return
        yield f"data: {json.dumps({'msg': '开始...', 'level': 'info', 'time': time.strftime('%H:%M:%S')})}\n\n"
        try:
            while True:
                try:
                    entry = q.get(timeout=2)
                    yield f"data: {json.dumps(entry)}\n\n"
                    if entry.get("done"):
                        break
                except Exception:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
