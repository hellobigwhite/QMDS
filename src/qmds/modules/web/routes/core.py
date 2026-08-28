"""核心路由 — 仪表盘、发现、检测、提取、流水线、任务、API"""

import json
import threading
import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from qmds.modules.web.db_helpers import get_mongo_db, get_product_db, get_site_db
from qmds.modules.web.task_manager import make_progress_callback, task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.core")

bp = Blueprint("core", __name__)


def _get_module():
    from qmds.utils.http_client import HttpClient
    from qmds.utils.proxy_manager import ProxyManager
    from qmds.config import settings
    from qmds.modules.data_scraper import DataScraperModule
    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    http = HttpClient(proxy_manager=pm)
    return DataScraperModule(http_client=http)


@bp.route("/")
def dashboard():
    return render_template("dashboard.html")


@bp.route("/api/collection-counts")
def api_collection_counts():
    """统一集合计数 API，一次请求返回所有集合的状态计数"""
    try:
        mongo = get_mongo_db()
        data = mongo.get_all_collection_counts()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 计数器校准 ──────────────────────────────────────────────

_counter_calibration_lock = threading.Lock()


def _is_calibrating() -> bool:
    """是否已有校准任务在运行（running/stopping 状态的 calibrate_counters 任务）"""
    for t in task_manager.list():
        if t.get("action") == "calibrate_counters" and t.get("status") in ("running", "stopping"):
            return True
    return False


def _make_rebuild_progress_cb(task_id: str):
    """适配 db 层的三参数进度回调约定 fn(processed, total, message) -> task_manager"""
    def cb(processed, total, message):
        if task_manager.is_stopped(task_id):
            raise InterruptedError("任务被用户停止")
        task_manager.update(
            task_id,
            progress=int(processed / total * 100) if total else 0,
            current=processed, total=total,
            message=message,
        )
    return cb


def run_counter_calibration(task_id: str):
    """校准计数器：全量重建 qmds_url_stores 与 qmds_product_data 的 _counters。

    以集合实际数据为准（count_documents / aggregate），覆盖写入 _counters，
    修复 $inc 漂移、历史脏数据、外部直改导致的不一致。
    """
    from qmds.db.mongodb import MongoDBClient
    from qmds.db.product_db import ProductDBClient

    mongo = MongoDBClient()
    product_db = ProductDBClient()
    try:
        task_manager.update(task_id, message="校准店铺库计数器 (qmds_url_stores)...", progress=5)
        r1 = mongo.rebuild_counters(progress_callback=_make_rebuild_progress_cb(task_id))
        task_manager.add_log(task_id, f"店铺库: 重建 {r1['rebuilt']} 个集合, "
                                      f"错误 {len(r1['errors'])} 个", "info")
        for err in r1["errors"]:
            task_manager.add_log(task_id, f"  - {err}", "warning")

        task_manager.update(task_id, message="校准商品库计数器 (qmds_product_data)...", progress=60)
        r2 = product_db.rebuild_product_counters(progress_callback=_make_rebuild_progress_cb(task_id))
        task_manager.add_log(task_id, f"商品库: 重建 {r2['rebuilt']} 个集合, "
                                      f"错误 {len(r2['errors'])} 个", "info")
        for err in r2["errors"]:
            task_manager.add_log(task_id, f"  - {err}", "warning")

        total_rebuilt = r1["rebuilt"] + r2["rebuilt"]
        total_errors = len(r1["errors"]) + len(r2["errors"])
        summary = f"校准完成: 共重建 {total_rebuilt} 个集合计数器" + \
                  (f"，{total_errors} 个错误" if total_errors else "")
        task_manager.update(task_id, status="completed", message=summary,
                            result={"url_stores": r1["rebuilt"],
                                    "product_data": r2["rebuilt"],
                                    "errors": total_errors},
                            progress=100)
        task_manager.add_log(task_id, summary, "info")
        log.info(f"计数器校准完成: {summary}")
    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="校准已停止")
        task_manager.add_log(task_id, "校准被用户停止", "warning")
    except Exception as e:
        log.error(f"计数器校准失败: {e}")
        task_manager.update(task_id, status="failed", message=f"校准失败: {e}")
        task_manager.add_log(task_id, f"校准失败: {e}", "error")
    finally:
        mongo.close()
        product_db.close()
        _counter_calibration_lock.release()


@bp.route("/api/calibrate-counters", methods=["POST"])
def api_calibrate_counters():
    """手动触发计数器校准（后台任务，防重复触发）"""
    result = run_counter_calibration_if_idle(source="manual")
    if result is None:
        return jsonify({"ok": False, "error": "校准任务正在运行中，请稍后"}), 409
    return jsonify({"ok": True, "task_id": result, "message": "计数器校准任务已启动"})


def run_counter_calibration_if_idle(source: str = "manual"):
    """启动校准任务（若未在运行）。返回 task_id，已在运行则返回 None。

    Args:
        source: 触发来源（manual=手动按钮 / scheduler=定时调度器）
    """
    if not _counter_calibration_lock.acquire(blocking=False):
        return None
    try:
        if _is_calibrating():
            return None
        task_id = f"calibrate_counters_{int(time.time())}"
        task_manager.create(task_id, "calibrate_counters", f"全部集合 ({source})")
        task_manager.start_task_thread(task_id, lambda: run_counter_calibration(task_id))
        return task_id
    except Exception:
        # 启动失败时释放锁（run_counter_calibration 的 finally 只在任务实际启动后负责释放）
        _counter_calibration_lock.release()
        raise


@bp.route("/api/counter-consistency")
def api_counter_consistency():
    """抽查计数器与实际数据的一致性（轻量：每个类型抽查第一个集合 + estimated_document_count 对比 total）"""
    try:
        mongo = get_mongo_db()
        checks = []
        counts = mongo.get_all_collection_counts()
        for ctype, items in counts.items():
            for item in items[:3]:
                col_name = item.get("_id")
                if not col_name:
                    continue
                actual = mongo.db[col_name].estimated_document_count()
                cached = item.get("total", 0)
                checks.append({
                    "collection": col_name,
                    "type": ctype,
                    "counter_total": cached,
                    "actual_total": actual,
                    "drift": actual - cached,
                    "consistent": abs(actual - cached) <= max(50, int(actual * 0.10)),
                })
        inconsistent = sum(1 for c in checks if not c["consistent"])
        return jsonify({"ok": True, "data": {
            "checks": checks, "inconsistent": inconsistent, "checked": len(checks),
        }})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/dashboard/stats")
def api_dashboard_stats():
    """仪表盘聚合统计 API"""
    stats = {}
    try:
        mongo = get_mongo_db()
        counts = mongo.get_all_collection_counts()
        unfiltered_list = counts.get("unfiltered", [])
        total_stores = sum(c.get("counts", {}).get("unfiltered", 0) for c in unfiltered_list)
        total_categories = len(unfiltered_list)
        stats["shopify_stores"] = total_stores
        stats["shopify_categories"] = total_categories
        stats["shopify_filtered"] = sum(c.get("counts", {}).get("filtered", 0) for c in counts.get("filtered", []))
        stats["shopify_failed"] = sum(c.get("counts", {}).get("filter_failed", 0) for c in counts.get("filtered_failed", []))
        stats["shopify_comprehensive"] = sum(c.get("counts", {}).get("filtered", 0) for c in counts.get("comprehensive", []))
        stats["shopify_info_pending"] = sum(c.get("counts", {}).get("pending", 0)
                                            for c in counts.get("info", []))
    except Exception:
        stats["shopify_stores"] = 0
        stats["shopify_categories"] = 0
        stats["shopify_filtered"] = 0
        stats["shopify_failed"] = 0
        stats["shopify_comprehensive"] = 0
        stats["shopify_info_pending"] = 0

    try:
        product_db = get_product_db()
        ps = product_db.get_all_stats()
        stats["products_raw"] = ps.get("total_raw", 0)
        stats["products_clean"] = ps.get("total_clean", 0)
        stats["products_exported"] = ps.get("total_exported", 0)
    except Exception:
        stats["products_raw"] = 0
        stats["products_clean"] = 0
        stats["products_exported"] = 0

    try:
        site_db = get_site_db()
        ss = site_db.get_stats()
        stats["sites_total"] = ss.get("total_sites", 0)
        stats["sites_local"] = ss.get("local_sites", 0)
        stats["sites_reported"] = ss.get("reported_sites", 0)
        stats["sites_built"] = ss.get("built_sites", 0)
    except Exception:
        stats["sites_total"] = 0
        stats["sites_local"] = 0
        stats["sites_reported"] = 0
        stats["sites_built"] = 0

    task_counts = task_manager.count_by_status()
    stats["tasks_running"] = task_counts.get("running", 0)
    stats["tasks_completed"] = task_counts.get("completed", 0)
    stats["tasks_failed"] = task_counts.get("failed", 0)

    return jsonify({"ok": True, "data": stats})


@bp.route("/api/tasks")
def api_tasks():
    return jsonify(task_manager.list())


@bp.route("/api/tasks/<task_id>/stop", methods=["POST"])
def api_stop_task(task_id):
    if task_manager.stop(task_id):
        return jsonify({"ok": True, "message": "任务停止请求已发送"})
    return jsonify({"ok": False, "error": "任务不存在或无法停止"}), 404


@bp.route("/api/tasks/<task_id>/logs")
def api_task_logs(task_id):
    limit = request.args.get("limit", 100, type=int)
    logs = task_manager.get_logs(task_id, limit=limit)
    return jsonify({"ok": True, "logs": logs})


@bp.route("/tasks")
def tasks():
    return render_template("tasks.html")


@bp.route("/discover", methods=["GET", "POST"])
def discover():
    if request.method == "POST":
        query = request.form.get("query", "inurl:collections/all")
        pages = int(request.form.get("pages", 0))

        task_id = f"discover_{int(time.time())}"
        task_manager.create(task_id, "discover", query)

        def run_task():
            try:
                module = _get_module()
                result = module.discover_stores(query, pages)
                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return
                task_manager.update(task_id, status="completed",
                                    message=f"发现 {result.total_found} 个店铺",
                                    result={"total": result.total_found, "data": result.data[:100]},
                                    progress=100)
                task_manager.add_log(task_id, f"完成: 发现 {result.total_found} 个店铺", "info")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
            except Exception as e:
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash("店铺发现任务已启动，可在任务页面查看进度", "info")
        return redirect(url_for("core.tasks"))
    return render_template("discover.html")


@bp.route("/detect", methods=["GET", "POST"])
def detect():
    result = None
    if request.method == "POST":
        url = request.form.get("url", "")
        if url:
            try:
                module = _get_module()
                result = module.detect_platform(url)
            except Exception as e:
                flash(f"检测失败: {e}", "error")
    return render_template("detect.html", result=result)


@bp.route("/extract", methods=["GET", "POST"])
def extract():
    if request.method == "POST":
        domain = request.form.get("domain", "")
        pages = int(request.form.get("pages", 5))
        if domain:
            task_id = f"extract_{int(time.time())}"
            task_manager.create(task_id, "extract", domain)

            def run_task():
                try:
                    module = _get_module()
                    result = module.extract_products(domain, pages)
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    task_manager.update(task_id, status="completed",
                                        message=f"提取 {result.total_scraped} 个商品",
                                        result={"count": result.total_scraped, "errors": len(result.errors)},
                                        progress=100)
                    task_manager.add_log(task_id, f"完成: 提取 {result.total_scraped} 个商品", "info")
                except InterruptedError:
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                except Exception as e:
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"失败: {e}", "error")

            task_manager.start_task_thread(task_id, run_task)
            flash("商品提取任务已启动，可在任务页面查看进度", "info")
            return redirect(url_for("core.tasks"))
    return render_template("extract.html")


@bp.route("/pipeline", methods=["GET", "POST"])
def pipeline():
    if request.method == "POST":
        query = request.form.get("query", "inurl:collections/all")
        pages = int(request.form.get("pages", 0))
        output = request.form.get("output", "pipeline_result.json")

        task_id = f"pipeline_{int(time.time())}"
        task_manager.create(task_id, "pipeline", query)

        def run_task():
            try:
                module = _get_module()
                result = module.run_pipeline(query, pages)
                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return
                with open(output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)
                task_manager.update(task_id, status="completed",
                                    message=f"完成: 发现 {result.total_found}, 提取 {result.total_scraped}",
                                    result={"found": result.total_found, "scraped": result.total_scraped},
                                    progress=100)
                task_manager.add_log(task_id, f"流水线完成: 发现 {result.total_found}, 提取 {result.total_scraped}", "info")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
            except Exception as e:
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash("流水线任务已启动，可在任务页面查看进度", "info")
        return redirect(url_for("core.tasks"))
    return render_template("pipeline.html")


@bp.route("/modules")
def list_modules():
    print("已安装模块:")
    print(f"  {'模块':<20} {'状态':<8} {'说明'}")
    print(f"  {'-'*20} {'-'*8} {'-'*30}")
    print(f"  {'data_scraper':<20} {'OK':<8} {'数据爬取：店铺发现、平台检测、商品提取'}")
    return redirect(url_for("core.dashboard"))
