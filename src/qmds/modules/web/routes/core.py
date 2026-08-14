"""核心路由 — 仪表盘、发现、检测、提取、流水线、任务、API"""

import json
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
        stats["shopify_filtered"] = sum(c.get("total", 0) for c in counts.get("filtered", []))
        stats["shopify_failed"] = sum(c.get("total", 0) for c in counts.get("filtered_failed", []))
        stats["shopify_comprehensive"] = sum(c.get("total", 0) for c in counts.get("comprehensive", []))
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
