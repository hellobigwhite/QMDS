import asyncio
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Optional

import pandas as pd

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for, Response, g

from qmds.config import settings
from qmds.config.categories import SHOPIFY_CATEGORIES
from qmds.db.mongodb import MongoDBClient
from qmds.db.product_db import ProductDBClient
from qmds.db.site_db import SiteDBClient
from qmds.modules.data_scraper import DataScraperModule
from qmds.modules.data_scraper.category_matcher import match_title
from qmds.modules.data_scraper.collections_fetcher import fetch_collections
from qmds.modules.data_scraper.product_crawler import create_crawler
from qmds.utils.http_client import HttpClient
from qmds.utils.proxy_manager import ProxyManager
from qmds.utils.logger import get_logger
from qmds.utils.domain_reporter import DomainReporter, DOMAIN_STATUS_LABELS, REPORT_API_BASE_URL, REPORT_CATEGORY_ID_MAP
from qmds.modules.order_checker import WooOrderChecker, ORDER_STATUS_LABELS

log = get_logger("web")


def _get_site_db() -> SiteDBClient:
    """获取请求级复用的 SiteDBClient"""
    if "site_db" not in g:
        g.site_db = SiteDBClient()
    return g.site_db


def _get_product_db() -> ProductDBClient:
    """获取请求级复用的 ProductDBClient"""
    if "product_db" not in g:
        g.product_db = ProductDBClient()
    return g.product_db


def _get_mongo_db() -> MongoDBClient:
    """获取请求级复用的 MongoDBClient"""
    if "mongo_db" not in g:
        g.mongo_db = MongoDBClient()
    return g.mongo_db


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._logs: dict[str, list] = {}
        self._lock = threading.Lock()

    def create(self, task_id: str, action: str, target: str) -> str:
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "action": action,
                "target": target,
                "status": "running",
                "progress": 0,
                "current": 0,
                "total": 0,
                "message": "Starting...",
                "result": None,
                "error": None,
                "created_at": datetime.now().isoformat(),
            }
            self._stop_events[task_id] = threading.Event()
            self._logs[task_id] = []
        return task_id

    def update(self, task_id: str, **kwargs):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(kwargs)

    def add_log(self, task_id: str, message: str, level: str = "info"):
        """添加任务日志"""
        with self._lock:
            if task_id in self._logs:
                from datetime import datetime
                self._logs[task_id].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": level,
                    "message": message
                })
                # 限制日志数量，保留最近500条
                if len(self._logs[task_id]) > 500:
                    self._logs[task_id] = self._logs[task_id][-500:]
        
        # 同时打印到终端
        log_func = getattr(log, level, log.info)
        log_func(f"[{task_id}] {message}")

    def get_logs(self, task_id: str, limit: int = 100) -> list:
        """获取任务日志"""
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

    def stop(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._stop_events:
                self._stop_events[task_id].set()
                if task_id in self._tasks:
                    self._tasks[task_id]["status"] = "stopping"
                    self._tasks[task_id]["message"] = "正在停止..."
                return True
            return False

    def is_stopped(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._stop_events:
                return self._stop_events[task_id].is_set()
            return False

    def get_stop_event(self, task_id: str):
        """获取任务的停止事件对象（可传入子模块实现即时停止）"""
        with self._lock:
            return self._stop_events.get(task_id)

    def cleanup(self, max_age_hours: int = 1):
        """清理已完成/失败/停止的任务，释放内存"""
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._lock:
            task_ids_to_remove = [
                k for k, v in self._tasks.items()
                if v.get("status") in ("completed", "failed", "stopped")
                and datetime.fromisoformat(v["created_at"]) < cutoff
            ]
            for task_id in task_ids_to_remove:
                # 清理result数据
                if self._tasks[task_id].get("result"):
                    self._tasks[task_id]["result"] = None
                self._tasks.pop(task_id, None)
                self._stop_events.pop(task_id, None)
                self._logs.pop(task_id, None)
            if task_ids_to_remove:
                log.info(f"清理了 {len(task_ids_to_remove)} 个已完成任务")


_task_manager = TaskManager()


def _start_cleanup_scheduler():
    """启动定时清理任务"""
    import threading
    
    def cleanup_loop():
        while True:
            try:
                time.sleep(300)  # 每5分钟清理一次
                _task_manager.cleanup(max_age_hours=1)
                import gc
                gc.collect()  # 强制垃圾回收
            except Exception as e:
                log.error(f"清理任务异常: {e}")
    
    t = threading.Thread(target=cleanup_loop, daemon=True, name="cleanup_scheduler")
    t.start()


def create_app(http_client: Optional[HttpClient] = None) -> Flask:
    load_dotenv()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
        static_url_path="/static",
    )
    app.secret_key = os.urandom(24)
    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    http = http_client or HttpClient(proxy_manager=pm)
    module = DataScraperModule(http_client=http, max_workers=20)  # 设置全局线程池大小
    
    # 启动定时清理
    _start_cleanup_scheduler()

    @app.teardown_appcontext
    def close_db_connections(exception):
        """请求结束时关闭数据库连接"""
        for key in ("site_db", "product_db", "mongo_db"):
            client = g.pop(key, None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @app.context_processor
    def inject_globals():
        return {
            "now": datetime.now(),
            "module_name": "QMDS 管理控制台",
        }

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/discover", methods=["GET", "POST"])
    def discover():
        result = None
        if request.method == "POST":
            query = request.form.get("query", "inurl:collections/all")
            pages = int(request.form.get("pages", 0))
            result = module.discover_stores(query, pages)
        return render_template("discover.html", result=result)

    @app.route("/detect", methods=["GET", "POST"])
    def detect():
        result = None
        if request.method == "POST":
            url = request.form.get("url", "")
            if url:
                result = module.detect_platform(url)
        return render_template("detect.html", result=result)

    @app.route("/extract", methods=["GET", "POST"])
    def extract():
        result = None
        if request.method == "POST":
            domain = request.form.get("domain", "")
            pages = int(request.form.get("pages", 5))
            if domain:
                result = module.extract_products(domain, pages)
        return render_template("extract.html", result=result)

    @app.route("/pipeline", methods=["GET", "POST"])
    def pipeline():
        result = None
        if request.method == "POST":
            query = request.form.get("query", "inurl:collections/all")
            pages = int(request.form.get("pages", 2))
            result = module.run_pipeline(query, pages)
        return render_template("pipeline.html", result=result)

    @app.route("/tasks")
    def tasks():
        return render_template("tasks.html", tasks=_task_manager.list())

    @app.route("/api/tasks")
    def api_tasks():
        return jsonify(_task_manager.list())

    @app.route("/api/tasks/<task_id>/stop", methods=["POST"])
    def api_stop_task(task_id):
        """停止指定任务"""
        if _task_manager.stop(task_id):
            return jsonify({"ok": True, "message": "任务停止请求已发送"})
        return jsonify({"ok": False, "error": "任务不存在或无法停止"}), 404

    @app.route("/api/tasks/<task_id>/logs")
    def api_task_logs(task_id):
        """获取指定任务的日志"""
        limit = request.args.get("limit", 100, type=int)
        logs = _task_manager.get_logs(task_id, limit=limit)
        return jsonify({"ok": True, "logs": logs})

    @app.route("/shopify/fetch-urls", methods=["GET", "POST"])
    def shopify_fetch_urls():
        api_status = module.searcher.get_api_status()
        selected_category = request.args.get("category", "")
        page = request.args.get("page", 1, type=int)
        per_page = 50
        stores = []
        stores_total = 0
        total_pages = 0
        
        # 查询选中类目的unfiltered数据
        if selected_category:
            db = _get_mongo_db()
            try:
                stores_total = db.get_unfiltered_count(selected_category)
                total_pages = (stores_total + per_page - 1) // per_page
                if page < 1:
                    page = 1
                elif page > total_pages and total_pages > 0:
                    page = total_pages
                skip = (page - 1) * per_page
                stores = db.get_unfiltered_stores(selected_category, limit=per_page, skip=skip)
            except Exception as e:
                log.error(f"查询unfiltered数据失败: {e}")
        
        if request.method == "POST":
            category = (request.form.get("category") or "").strip()
            keyword = (request.form.get("keyword") or "").strip()
            min_products = int(request.form.get("min_products", 0))
            storage = request.form.get("storage", "mongo")
            provider = (request.form.get("provider") or "").strip()
            save_mongo = storage == "mongo"
            save_excel = storage == "excel"
            if category and keyword:
                task_id = f"fetch_{category}_{int(time.time())}"
                _task_manager.create(task_id, "fetch_shopify_urls", f"{category} | {keyword}")

                def run_task():
                    try:
                        _task_manager.update(task_id, status="running", message=f"搜索中: {keyword}")
                        _task_manager.add_log(task_id, f"任务启动: 搜索Shopify店铺", "info")
                        _task_manager.add_log(task_id, f"关键词: {keyword}", "info")
                        _task_manager.add_log(task_id, f"类目: {category}", "info")
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return
                        _task_manager.add_log(task_id, "开始搜索...", "info")
                        
                        def progress_callback(msg):
                            _task_manager.add_log(task_id, msg, "info")
                        
                        result = module.fetch_shopify_urls_by_keyword(
                            category=category, keyword=keyword,
                            max_pages=0, min_products=min_products,
                            keyword_workers=3,  # 3个关键词并行
                            save_mongo=save_mongo, save_excel=save_excel,
                            provider_name=provider,
                            progress_callback=progress_callback,
                        )
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return
                        _task_manager.update(task_id, status="completed",
                            message=f"完成: 找到 {result['total_shopify']} 个店铺",
                            result=result, progress=100)
                        _task_manager.add_log(task_id, f"任务完成: 找到 {result['total_shopify']} 个店铺", "info")
                    except Exception as e:
                        log.error(f"fetch-urls task failed: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                        _task_manager.add_log(task_id, f"任务失败: {e}", "error")

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"任务已启动: {category} | {keyword}，可在任务页面查看进度")
                return redirect(url_for("shopify_fetch_urls", category=category))
        return render_template("shopify_urls.html", result=None, categories=SHOPIFY_CATEGORIES, db_name=settings.mongo_db_url, api_status=api_status, selected_category=selected_category, stores=stores, stores_total=stores_total, page=page, total_pages=total_pages)

    # ── Shopify店铺URL CRUD API ──────────────────────────────

    @app.route("/shopify/unfiltered/add", methods=["POST"])
    def shopify_unfiltered_add():
        """添加单条unfiltered记录"""
        category = request.form.get("category", "").strip()
        domain = request.form.get("domain", "").strip()
        if not category or not domain:
            flash("类目和域名不能为空", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))
        
        db = _get_mongo_db()
        try:
            store_data = {
                "domain": domain,
                "url": request.form.get("url", f"https://{domain}").strip(),
                "platform": request.form.get("platform", "Shopify").strip(),
                "product_count": int(request.form.get("product_count", 0)),
                "store_name": request.form.get("store_name", "").strip(),
                "currency": request.form.get("currency", "USD").strip(),
                "source": "manual",
            }
            if db.add_unfiltered(category, store_data):
                flash(f"已添加店铺: {domain}", "success")
            else:
                flash(f"添加失败: {domain}", "error")
        except Exception as e:
            flash(f"添加失败: {e}", "error")
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/shopify/unfiltered/import", methods=["POST"])
    def shopify_unfiltered_import():
        """批量导入店铺数据（Excel文件）"""
        category = request.form.get("category", "").strip()
        if not category:
            flash("类目不能为空", "error")
            return redirect(url_for("shopify_fetch_urls"))

        file = request.files.get("file")
        if not file or not file.filename:
            flash("请选择要导入的Excel文件", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))

        if not file.filename.endswith(('.xlsx', '.xls')):
            flash("请上传Excel文件（.xlsx或.xls格式）", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))

        db = _get_mongo_db()
        try:
            filepath = os.path.join(os.getcwd(), "uploads", file.filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)

            result = db.import_from_excel(category, filepath)

            messages = [f"新增: {result['created']}", f"更新: {result['updated']}", f"跳过: {result['skipped']}"]
            if result['errors']:
                messages.append(f"错误: {len(result['errors'])}")
                for error in result['errors'][:5]:
                    flash(error, "error")
                if len(result['errors']) > 5:
                    flash(f"还有 {len(result['errors']) - 5} 个错误...", "error")

            flash(f"导入完成: {', '.join(messages)}", "success")
        except Exception as e:
            flash(f"导入失败: {e}", "error")
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/shopify/unfiltered/edit", methods=["POST"])
    def shopify_unfiltered_edit():
        """编辑单条unfiltered记录"""
        category = request.form.get("category", "").strip()
        domain = request.form.get("domain", "").strip()
        if not category or not domain:
            flash("类目和域名不能为空", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))
        
        db = _get_mongo_db()
        try:
            update_data = {
                "url": request.form.get("url", "").strip(),
                "platform": request.form.get("platform", "").strip(),
                "product_count": int(request.form.get("product_count", 0)),
                "store_name": request.form.get("store_name", "").strip(),
                "currency": request.form.get("currency", "USD").strip(),
            }
            if db.update_unfiltered(category, domain, update_data):
                flash(f"已更新店铺: {domain}", "success")
            else:
                flash(f"更新失败或无变更: {domain}", "error")
        except Exception as e:
            flash(f"更新失败: {e}", "error")
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/shopify/unfiltered/delete", methods=["POST"])
    def shopify_unfiltered_delete():
        """删除单条或批量删除unfiltered记录"""
        category = request.form.get("category", "").strip()
        if not category:
            flash("类目不能为空", "error")
            return redirect(url_for("shopify_fetch_urls"))
        
        domains = request.form.getlist("domains")
        single_domain = request.form.get("domain", "").strip()
        if single_domain and not domains:
            domains = [single_domain]
        
        if not domains:
            flash("请选择要删除的记录", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))
        
        db = _get_mongo_db()
        try:
            deleted = db.delete_unfiltered_many(category, domains)
            flash(f"已删除 {deleted} 条记录", "success")
        except Exception as e:
            flash(f"删除失败: {e}", "error")
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/api/shopify/unfiltered/<category>/<domain>")
    def api_shopify_unfiltered_get(category, domain):
        """API: 获取单条unfiltered记录"""
        db = _get_mongo_db()
        try:
            doc = db.get_unfiltered_by_domain(category, domain)
            if doc:
                return jsonify({"ok": True, "data": doc})
            return jsonify({"ok": False, "error": "未找到记录"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/shopify/filter-categories", methods=["GET", "POST"])
    def shopify_filter_categories():
        selected_category = request.args.get("category", "")
        filtered_stores = []
        filtered_total = 0
        
        # 查询选中类目的 filtered 数据
        if selected_category:
            db = _get_mongo_db()
            try:
                filtered_stores = db.get_filtered_stores(selected_category, limit=100)
                filtered_total = db.get_filtered_count(selected_category)
            except Exception as e:
                log.error(f"查询 filtered 数据失败: {e}")
        
        if request.method == "POST":
            category = (request.form.get("category") or "").strip()
            action = request.form.get("action", "filter")
            
            if action == "filter" and category:
                task_id = f"filter_{category}_{int(time.time())}"
                _task_manager.create(task_id, "filter_categories", category)

                def run_task():
                    db = MongoDBClient()
                    try:
                        stores = db.get_all_urls(category)
                        total = len(stores)
                        log.info(f"[精准类目] 开始任务: category={category}, 待处理店铺={total}")
                        _task_manager.add_log(task_id, f"任务启动: 精准类目筛选", "info")
                        _task_manager.add_log(task_id, f"类目: {category}", "info")
                        _task_manager.add_log(task_id, f"待处理店铺: {total}", "info")
                        if total == 0:
                            _task_manager.update(task_id, status="completed",
                                message=f"类目 {category} 无待处理 URL", progress=100)
                            _task_manager.add_log(task_id, f"类目 {category} 无待处理 URL", "info")
                            return

                        matched_count = 0
                        removed_count = 0
                        processed = 0
                        for store in stores:
                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", 
                                    message=f"任务已停止: 处理 {processed}/{total}，已匹配 {matched_count} 条")
                                _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return
                            
                            store_url = store["url"]
                            domain = store["domain"]
                            processed += 1
                            domain_matched = False
                            try:
                                collections = fetch_collections(http, store_url)
                                log.info(f"[精准类目] [{processed}/{total}] {domain} - 获取 {len(collections)} 个 collection")
                                _task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 获取 {len(collections)} 个 collection", "info")
                                for coll in collections:
                                    if match_title(category, coll["title"]):
                                        # 检查集合是否有产品
                                        check_url = f"{store_url}/collections/{coll['handle']}/products.json?limit=1"
                                        try:
                                            check_resp = http.get(check_url, timeout=10)
                                            if check_resp.status_code == 200:
                                                check_data = check_resp.json()
                                                products = check_data.get("products", [])
                                                if not products:
                                                    log.info(f"[精准类目]   ⏭️ 跳过(无产品): {coll['title']}")
                                                    continue
                                        except Exception:
                                            pass
                                        
                                        if db.save_filtered_url(
                                            category, domain, store_url,
                                            coll["title"], coll["handle"],
                                        ):
                                            matched_count += 1
                                            domain_matched = True
                                            log.info(f"[精准类目]   ✅ 匹配: {coll['title']} -> {store_url}/collections/{coll['handle']}")
                                            _task_manager.add_log(task_id, f"匹配: {coll['title']}", "info")
                                
                                if db.delete_unfiltered(category, domain):
                                    removed_count += 1
                                    log.info(f"[精准类目]   🗑️ 已从 {category}_unfiltered 删除: {domain}")
                            except Exception as e:
                                log.warning(f"[精准类目] [{processed}/{total}] {domain} - 处理失败: {e}")
                                _task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 处理失败: {e}", "warning")
                                if db.delete_unfiltered(category, domain):
                                    removed_count += 1
                                    log.info(f"[精准类目]   🗑️ 已从 {category}_unfiltered 删除: {domain}")

                            if processed % 10 == 0 or processed == total:
                                _task_manager.update(task_id,
                                    progress=int(processed / total * 100),
                                    current=processed,
                                    total=total,
                                    message=f"处理中: {processed}/{total}，已匹配 {matched_count} 条，已删除 {removed_count} 个域名")

                        log.info(f"[精准类目] 任务完成: category={category}, 处理={total}, 匹配={matched_count}, 删除={removed_count}")
                        _task_manager.update(task_id, status="completed",
                            message=f"完成: 处理 {total} 个店铺，匹配 {matched_count} 条 collection，从 unfiltered 删除 {removed_count} 个域名",
                            result={"total_stores": total, "matched": matched_count, "removed": removed_count},
                            progress=100)
                        _task_manager.add_log(task_id, f"任务完成: 处理 {total} 个店铺，匹配 {matched_count} 条 collection，从 unfiltered 删除 {removed_count} 个域名", "info")
                    except Exception as e:
                        log.error(f"[精准类目] 任务异常: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                        _task_manager.add_log(task_id, f"任务异常: {e}", "error")
                    finally:
                        db.close()

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"精准类目筛选任务已启动: {category}，可在任务页面查看进度")
                return redirect(url_for("shopify_filter_categories", category=category))
            
            elif action == "delete_selected" and selected_category:
                selected_ids = request.form.getlist("selected_ids")
                if selected_ids:
                    db = MongoDBClient()
                    try:
                        count = db.delete_filtered_many(selected_category, selected_ids)
                        flash(f"已删除 {count} 条记录", "success")
                    except Exception as e:
                        log.error(f"删除 filtered 记录失败: {e}")
                        flash(f"删除失败: {e}", "error")
                    finally:
                        db.close()
                return redirect(url_for("shopify_filter_categories", category=selected_category))
        
        return render_template("shopify_categories.html", 
                             categories=SHOPIFY_CATEGORIES,
                             selected_category=selected_category,
                             filtered_stores=filtered_stores,
                             filtered_total=filtered_total)

    @app.route("/shopify/filter-categories/<category>/<doc_id>/edit", methods=["GET", "POST"])
    def shopify_filter_edit(category, doc_id):
        """编辑 filtered 记录"""
        db = _get_mongo_db()
        try:
            if request.method == "POST":
                updates = {
                    "domain": request.form.get("domain", "").strip(),
                    "store_url": request.form.get("store_url", "").strip(),
                    "url": request.form.get("url", "").strip(),
                    "collection_title": request.form.get("collection_title", "").strip(),
                    "collection_handle": request.form.get("collection_handle", "").strip(),
                }
                if db.update_filtered_by_id(category, doc_id, updates):
                    flash("记录已更新", "success")
                else:
                    flash("更新失败", "error")
                return redirect(url_for("shopify_filter_categories", category=category))
            
            doc = db.get_filtered_by_id(category, doc_id)
            if not doc:
                flash("记录不存在", "error")
                return redirect(url_for("shopify_filter_categories", category=category))
            
            return render_template("shopify_filter_edit.html", 
                                 category=category, 
                                 doc=doc)
        except Exception as e:
            log.error(f"编辑 filtered 记录失败: {e}")
            flash(f"操作失败: {e}", "error")
            return redirect(url_for("shopify_filter_categories", category=category))

    @app.route("/shopify/filter-categories/add", methods=["POST"])
    def shopify_filter_add():
        """手动添加单条记录到 filtered"""
        category = request.form.get("category", "").strip()
        store_url = request.form.get("store_url", "").strip()
        collection_url = request.form.get("collection_url", "").strip()

        if not category or not collection_url:
            flash("类目和 Collection URL 不能为空", "error")
            return redirect(url_for("shopify_filter_categories", category=category))

        db = _get_mongo_db()
        try:
            if db.add_filtered_manual(category, store_url, collection_url):
                flash("已添加记录", "success")
            else:
                flash("添加失败", "error")
        except Exception as e:
            flash(f"添加失败: {e}", "error")
        return redirect(url_for("shopify_filter_categories", category=category))

    @app.route("/shopify/filter-categories/import", methods=["POST"])
    def shopify_filter_import():
        """批量导入 URL 到 filtered（Excel文件或文本）"""
        category = request.form.get("category", "").strip()
        if not category:
            flash("类目不能为空", "error")
            return redirect(url_for("shopify_filter_categories"))

        urls_to_add = []
        errors = []

        file = request.files.get("file")
        if file and file.filename:
            if not file.filename.endswith(('.xlsx', '.xls', '.txt')):
                flash("请上传 Excel 或文本文件", "error")
                return redirect(url_for("shopify_filter_categories", category=category))

            import os
            filepath = os.path.join(os.getcwd(), "uploads", file.filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)

            if file.filename.endswith('.txt'):
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if '/collections/' in line:
                                urls_to_add.append({"collection_url": line})
                            elif '.' in line:
                                # 支持纯域名或带协议的URL
                                url = line if line.startswith('http') else f"https://{line}"
                                urls_to_add.append({"store_url": url})
            else:
                import pandas as pd
                df = pd.read_excel(filepath)
                for _, row in df.iterrows():
                    store_url = str(row.get("store_url", "") or row.get("店铺URL", "") or "").strip()
                    collection_url = str(row.get("collection_url", "") or row.get("collection URL", "") or "").strip()
                    if collection_url:
                        urls_to_add.append({"store_url": store_url, "collection_url": collection_url})

        urls_text = request.form.get("urls", "").strip()
        if urls_text:
            for line in urls_text.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    if '/collections/' in line:
                        urls_to_add.append({"collection_url": line})
                    elif '.' in line:
                        # 支持纯域名或带协议的URL
                        url = line if line.startswith('http') else f"https://{line}"
                        urls_to_add.append({"store_url": url})

        if not urls_to_add:
            flash("未找到有效的 URL", "error")
            return redirect(url_for("shopify_filter_categories", category=category))

        db = _get_mongo_db()
        try:
            result = db.add_filtered_batch(category, urls_to_add)
            messages = [f"新增: {result['created']}", f"更新: {result['updated']}"]
            if result['errors']:
                messages.append(f"错误: {len(result['errors'])}")
                for error in result['errors'][:5]:
                    flash(error, "error")
            flash(f"导入完成: {', '.join(messages)}", "success")
        except Exception as e:
            flash(f"导入失败: {e}", "error")
        return redirect(url_for("shopify_filter_categories", category=category))

    @app.route("/shopify/filter-categories/<category>/<doc_id>/delete", methods=["POST"])
    def shopify_filter_delete(category, doc_id):
        """删除 filtered 记录"""
        db = MongoDBClient()
        try:
            if db.delete_filtered_by_id(category, doc_id):
                flash("记录已删除", "success")
            else:
                flash("删除失败", "error")
        except Exception as e:
            log.error(f"删除 filtered 记录失败: {e}")
            flash(f"删除失败: {e}", "error")
        return redirect(url_for("shopify_filter_categories", category=category))

    @app.route("/product-data", methods=["GET"])
    def product_data():
        return redirect(url_for("product_data_overview"))

    @app.route("/api/product-data/stats")
    def api_product_data_stats():
        """异步获取产品数据统计"""
        try:
            product_db = _get_product_db()
            stats = product_db.get_all_stats()
            return jsonify({"ok": True, "data": stats})
        except Exception as e:
            log.error(f"获取产品数据统计失败: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/product-data/overview", methods=["GET"])
    def product_data_overview():
        try:
            product_db = _get_product_db()
            stats = product_db.get_all_stats()
            collections = product_db.list_all_collections()
            
            # 获取可用的_filtered类目（用于爬取）
            source_db = _get_mongo_db()
            filtered_categories = source_db.list_filtered_categories()
            
            return render_template("product_overview.html",
                                   total_collections=stats["total_categories"],
                                   non_empty_collections=stats["total_categories"],
                                   total_rows=stats["total_raw"],
                                   total_clean_rows=stats["total_clean"],
                                   total_exported=stats.get("total_exported", 0),
                                   total_unclean=stats.get("total_unclean", 0),
                                   total_cleaned=stats.get("total_cleaned", 0),
                                   total_failed=stats.get("total_failed", 0),
                                   collections=collections,
                                   category_stats=stats["categories"],
                                   filtered_categories=filtered_categories)
        except Exception as e:
            log.error(f"获取产品数据失败: {e}")
            return render_template("product_overview.html",
                                   total_collections=0,
                                   non_empty_collections=0,
                                   total_rows=0,
                                   total_clean_rows=0,
                                   total_exported=0,
                                   total_unclean=0,
                                   total_cleaned=0,
                                   total_failed=0,
                                   collections=[],
                                   category_stats=[],
                                   filtered_categories=[],
                                   error=str(e))

    @app.route("/product-data/crawl", methods=["GET", "POST"])
    def product_data_crawl():
        # 获取可用的_filtered类目（用于爬取）
        source_db = _get_mongo_db()
        filtered_categories = source_db.list_filtered_categories()
        
        if request.method == "POST":
            category = request.form.get("category", "").strip()
            max_sites = int(request.form.get("max_sites", 10))
            max_workers = int(request.form.get("max_workers", 10))
            
            if not category:
                flash("请选择类目", "error")
                return redirect(url_for("product_data_crawl"))
            
            task_id = f"crawl_{category}_{int(time.time())}"
            _task_manager.create(task_id, "crawl_products", category)
            
            def run_task():
                crawler = None
                try:
                    _task_manager.update(task_id, status="running", message=f"开始爬取类目: {category} ({max_workers} 线程)")
                    _task_manager.add_log(task_id, f"任务启动: 爬取类目 {category}", "info")
                    _task_manager.add_log(task_id, f"线程数: {max_workers}", "info")
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return
                    
                    # 创建爬取器
                    crawler = create_crawler()
                    _task_manager.add_log(task_id, "爬取器创建成功", "info")
                    
                    # 定义进度回调
                    def progress_callback(info):
                        if _task_manager.is_stopped(task_id):
                            raise InterruptedError("任务被用户停止")
                        if isinstance(info, dict):
                            msg = info.get("message", "")
                            prog = info.get("progress")
                            current = info.get("current")
                            total = info.get("total")
                            update_data = {"message": msg}
                            if prog is not None:
                                update_data["progress"] = prog
                            if current is not None:
                                update_data["current"] = current
                            if total is not None:
                                update_data["total"] = total
                            _task_manager.update(task_id, **update_data)
                            if msg:
                                _task_manager.add_log(task_id, msg, "info")
                        else:
                            _task_manager.update(task_id, message=str(info))
                            _task_manager.add_log(task_id, str(info), "info")
                    
                    # 爬取类目数据
                    _task_manager.add_log(task_id, "开始爬取类目数据...", "info")
                    stop_event = _task_manager.get_stop_event(task_id)
                    result = crawler.crawl_category(category, max_sites=max_sites,
                                                    workers=max_workers,
                                                    progress_callback=progress_callback,
                                                    stop_event=stop_event)
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 爬取 {result['success_sites']}/{result['total_sites']} 个站点，获取 {result['total_products']} 件商品",
                                        progress=100)
                    _task_manager.add_log(task_id, f"任务完成: 成功爬取 {result['success_sites']}/{result['total_sites']} 个站点", "info")
                except InterruptedError:
                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                    _task_manager.add_log(task_id, "任务被用户停止", "warning")
                except Exception as e:
                    log.error(f"爬取任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                finally:
                    # 释放资源
                    if crawler:
                        crawler.close()
                        del crawler
                    import gc
                    gc.collect()
            
            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据爬取任务已启动: {category}，可在任务页面查看进度")
            return redirect(url_for("product_data_crawl"))
        
        return render_template("product_crawl.html", filtered_categories=filtered_categories)

    @app.route("/product-data/clean", methods=["GET", "POST"])
    def product_data_clean():
        if request.method == "POST":
            category = request.form.get("category", "__all__")
            force = request.form.get("force") == "1"
            task_id = f"clean_{category}_{int(time.time())}"
            _task_manager.create(task_id, "clean_products", category)
            
            def run_task():
                product_db = None
                try:
                    force_msg = "（强制模式）" if force else ""
                    _task_manager.update(task_id, status="running", message=f"开始清洗: {category}{force_msg}")
                    _task_manager.add_log(task_id, f"任务启动: 清洗数据 {category}{force_msg}", "info")
                    
                    product_db = ProductDBClient()
                    _task_manager.add_log(task_id, "数据库连接成功", "info")
                    
                    if category == "__all__":
                        # 清洗所有类目
                        categories = product_db.list_categories()
                        _task_manager.add_log(task_id, f"获取到 {len(categories)} 个类目", "info")
                    else:
                        categories = [category]
                    
                    total_processed = 0
                    total_cleaned = 0
                    total_removed = 0
                    
                    for cat in categories:
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", 
                                message=f"任务已停止: 已处理 {total_processed} 条数据")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return
                        
                        _task_manager.update(task_id, message=f"清洗类目: {cat}")
                        _task_manager.add_log(task_id, f"开始清洗类目: {cat}", "info")
                        
                        result = product_db.clean_category(cat, force=force)
                        total_processed += result["processed"]
                        total_cleaned += result["cleaned"]
                        total_removed += result["removed"]
                        
                        log.info(f"类目 {cat}: 处理 {result['processed']} 条，清洗后 {result['cleaned']} 条")
                        _task_manager.add_log(task_id, f"类目 {cat}: 处理 {result['processed']} 条，通过 {result['cleaned']} 条，移除 {result['removed']} 条", "info")
                        
                        # 输出各过滤步骤统计
                        filter_stats = result.get("stats", {})
                        for reason, count in filter_stats.items():
                            if count > 0:
                                _task_manager.add_log(task_id, f"  ├─ {reason}: {count} 条", "info")
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 处理 {total_processed} 条数据，清洗后 {total_cleaned} 条，移除 {total_removed} 条",
                                        progress=100)
                    _task_manager.add_log(task_id, f"任务完成: 处理 {total_processed} 条数据，清洗后 {total_cleaned} 条，移除 {total_removed} 条", "info")
                except Exception as e:
                    import traceback
                    log.error(f"清洗任务失败: {e}\n{traceback.format_exc()}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                finally:
                    if product_db:
                        product_db.close()
                    import gc
                    gc.collect()
            
            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据清洗任务已启动: {category}，可在任务页面查看进度")
            return redirect(url_for("product_data_clean"))
        
        # GET请求：直接渲染模板，统计数据通过AJAX异步获取
        return render_template("product_clean.html", category_stats=[])

    @app.route("/product-data/export", methods=["GET", "POST"])
    def product_data_export():
        if request.method == "POST":
            category = request.form.get("category", "").strip()
            export_format = request.form.get("format", "excel")
            limit = request.form.get("limit", "").strip()
            limit = int(limit) if limit and limit.isdigit() else None
            
            if not category:
                flash("请选择要导出的类目", "error")
                return redirect(url_for("product_data_export"))
            
            task_id = f"export_{category}_{int(time.time())}"
            _task_manager.create(task_id, "export_products", category)
            
            def run_task():
                product_db = None
                try:
                    limit_msg = f"（限制 {limit} 条）" if limit else ""
                    _task_manager.update(task_id, status="running", message=f"开始导出: {category}{limit_msg}")
                    _task_manager.add_log(task_id, f"任务启动: 导出数据 {category}{limit_msg}", "info")
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return
                    
                    product_db = ProductDBClient()
                    export_dir = str(settings.data_dir / "exports")
                    _task_manager.add_log(task_id, f"导出目录: {export_dir}", "info")
                    
                    # 定义进度回调
                    def progress_callback(info):
                        if _task_manager.is_stopped(task_id):
                            raise InterruptedError("任务被用户停止")
                        if isinstance(info, dict):
                            _task_manager.update(task_id, **info)
                            if info.get("message"):
                                _task_manager.add_log(task_id, info["message"], "info")
                        else:
                            _task_manager.update(task_id, message=str(info))
                            _task_manager.add_log(task_id, str(info), "info")
                    
                    _task_manager.add_log(task_id, "开始导出数据...", "info")
                    filepath = product_db.export_category_to_excel(
                        category, export_dir, limit=limit, progress_callback=progress_callback
                    )
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return
                    
                    if filepath:
                        # 获取实际导出数量（用聚合精确计数）
                        count_pipeline = [{"$count": "count"}]
                        count_result = list(product_db.clean_col(category).aggregate(count_pipeline))
                        actual_count = count_result[0]["count"] if count_result else 0
                        count = min(limit, actual_count) if limit else actual_count
                        
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 导出 {count} 条数据到 {os.path.basename(filepath)}",
                                            progress=100,
                                            current=count,
                                            total=count)
                        _task_manager.add_log(task_id, f"任务完成: 导出 {count} 条数据到 {os.path.basename(filepath)}", "info")
                    else:
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 类目 {category} 无清洗后数据",
                                            progress=100)
                        _task_manager.add_log(task_id, f"任务完成: 类目 {category} 无清洗后数据", "info")
                except InterruptedError:
                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                    _task_manager.add_log(task_id, "任务被用户停止", "warning")
                except Exception as e:
                    log.error(f"导出任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                finally:
                    if product_db:
                        product_db.close()
                    import gc
                    gc.collect()
            
            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据导出任务已启动: {category}，可在任务页面查看进度")
            return redirect(url_for("product_data_export"))
        
        # GET请求：直接返回页面，统计数据通过API异步加载
        return render_template("product_export.html", category_stats=[])

    # === 建站管理路由 ===

    @app.route("/site-management", methods=["GET"])
    def site_management():
        """建站管理主页 - 显示统计概览"""
        site_db = _get_site_db()
        try:
            stats = site_db.get_stats()
        except Exception as e:
            log.error(f"获取站点统计失败: {e}")
            stats = {"total_sites": 0, "local_sites": 0, "reported_sites": 0, "scheduled_sites": 0, "built_sites": 0}
        return render_template("site_management.html", stats=stats)

    @app.route("/site-management/local", methods=["GET", "POST"])
    def site_local():
        """本地站点管理"""
        site_db = _get_site_db()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))

            if request.method == "POST":
                action = request.form.get("action", "")

                if action == "add":
                    domain = request.form.get("domain", "").strip()
                    if domain:
                        site_data = {
                            "domain": domain,
                            "template": request.form.get("template", ""),
                            "server": request.form.get("server", ""),
                            "category": request.form.get("category", ""),
                            "main_category": request.form.get("main_category", ""),
                            "main_data_source_id": request.form.get("main_data_source_id", ""),
                            "extra_data_source_id": request.form.get("extra_data_source_id", ""),
                            "title": request.form.get("title", ""),
                            "description": request.form.get("description", ""),
                            "address": request.form.get("address", ""),
                        }
                        site_db.add_site(site_data)
                        flash(f"站点 {domain} 已添加", "success")
                    return redirect(url_for("site_local"))

                elif action == "import":
                    file = request.files.get("file")
                    if file and file.filename:
                        filepath = os.path.join(os.getcwd(), "uploads", file.filename)
                        os.makedirs(os.path.dirname(filepath), exist_ok=True)
                        file.save(filepath)
                        result = site_db.import_from_excel(filepath)
                        
                        # 构建详细反馈信息
                        messages = []
                        messages.append(f"新增: {result['created']}")
                        messages.append(f"更新: {result['updated']}")
                        messages.append(f"跳过: {result['skipped']}")
                        
                        if result['errors']:
                            messages.append(f"错误: {len(result['errors'])}")
                            for error in result['errors'][:5]:  # 最多显示5个错误
                                flash(error, "error")
                            if len(result['errors']) > 5:
                                flash(f"还有 {len(result['errors']) - 5} 个错误...", "error")
                        
                        flash(f"导入完成: {', '.join(messages)}", "success")
                    return redirect(url_for("site_local"))

                elif action == "delete_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.delete_sites_by_ids(selected_ids)
                        flash(f"已删除 {count} 个站点", "success")
                    return redirect(url_for("site_local"))

                elif action == "report_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if not selected_ids:
                        flash("请先勾选要上报的站点", "error")
                        return redirect(url_for("site_local"))

                    task_id = f"report_sites_{int(time.time())}"
                    _task_manager.create(task_id, "report_domains", f"域名上报 ({len(selected_ids)} sites)")

                    def run_report_task():
                        _site_db = SiteDBClient()
                        try:
                            settings = _site_db.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                _task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            total = len(selected_ids)
                            success = 0
                            failed = 0
                            errors = []
                            _task_manager.add_log(task_id, f"任务启动: 域名上报", "info")
                            _task_manager.add_log(task_id, f"待上报站点: {total}", "info")

                            for i, site_id in enumerate(selected_ids):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                                    _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                    return

                                site = _site_db.get_site_by_id(site_id)
                                if not site:
                                    failed += 1
                                    errors.append(f"ID {site_id}: 站点不存在")
                                    _task_manager.add_log(task_id, f"ID {site_id}: 站点不存在", "error")
                                    continue

                                domain = (site.get("domain") or "").strip()
                                server = (site.get("server") or "").strip()
                                template = (site.get("template") or "").strip()
                                category_name = (site.get("category") or "").strip()

                                current = i + 1

                                # 检查必填字段
                                missing = []
                                if not domain:
                                    missing.append("域名")
                                if not server:
                                    missing.append("服务器")
                                if not template:
                                    missing.append("模板")
                                if not category_name:
                                    missing.append("大类")

                                if missing:
                                    failed += 1
                                    errors.append(f"{domain or site_id}: 缺少字段 {', '.join(missing)}")
                                    _task_manager.update(task_id, current=current,
                                                       progress=int(current / total * 100),
                                                       message=f"[{current}/{total}] ✗ {domain or site_id} - 缺少字段")
                                    _task_manager.add_log(task_id, f"[{current}/{total}] {domain or site_id} - 缺少字段: {', '.join(missing)}", "error")
                                    continue

                                # 映射分类ID
                                category_id = REPORT_CATEGORY_ID_MAP.get(category_name)
                                if not category_id:
                                    failed += 1
                                    errors.append(f"{domain}: 无效分类 {category_name}")
                                    _task_manager.update(task_id, current=current,
                                                       progress=int(current / total * 100),
                                                       message=f"[{current}/{total}] ✗ {domain} - 无效分类")
                                    _task_manager.add_log(task_id, f"[{current}/{total}] {domain} - 无效分类: {category_name}", "error")
                                    continue

                                # 构建上报数据
                                payload = {
                                    "name": domain,
                                    "serverip": server,
                                    "template": template,
                                    "category": category_id,
                                    "categoryTag": None,
                                    "language": None,
                                }

                                try:
                                    _task_manager.update(task_id, current=current,
                                                       progress=int((current - 0.5) / total * 100),
                                                       message=f"[{current}/{total}] [{domain}] 正在上报...")
                                    _task_manager.add_log(task_id, f"[{current}/{total}] [{domain}] 正在上报...", "info")
                                    reporter.submit_domain(payload)

                                    # 获取上报后的域名信息
                                    report_id = ""
                                    domain_status = ""
                                    try:
                                        info = reporter.fetch_domain_info(domain)
                                        report_id = str(info.get("id") or "")
                                        status_val = info.get("status")
                                        domain_status = str(status_val) if status_val is not None else ""
                                    except Exception as e:
                                        log.warning(f"获取域名信息失败: {domain} - {e}")

                                    # 更新本地数据库
                                    now = datetime.utcnow().isoformat()
                                    _site_db.update_site(domain, {
                                        "report_status": "已报",
                                        "report_time": now,
                                        "report_id": report_id,
                                        "domain_status": domain_status,
                                        "schedule_enabled": "0",
                                    })

                                    success += 1
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✓ {domain} 上报成功")
                                    log.info(f"[上报] [{domain}] ✓ 上报成功, report_id={report_id}")

                                except Exception as e:
                                    failed += 1
                                    errors.append(f"{domain}: {e}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✗ {domain} 上报失败 - {e}")
                                    log.error(f"[上报] [{domain}] 失败: {e}")

                            # 汇总
                            summary = f"域名上报完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                            _task_manager.update(task_id, status="completed", message=summary,
                                              progress=100, current=total, total=total)

                            if errors:
                                log.warning("[上报] 失败明细:")
                                for err in errors:
                                    log.warning(f"  {err}")

                        except Exception as e:
                            log.error(f"[上报] 任务异常: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                        finally:
                            _site_db.close()

                    threading.Thread(target=run_report_task, daemon=True).start()
                    flash(f"域名上报任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                    return redirect(url_for("tasks"))

                elif action == "schedule_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    schedule_time = request.form.get("schedule_time", "")
                    if selected_ids and schedule_time:
                        count = site_db.batch_set_schedule(selected_ids, schedule_time)
                        flash(f"已设置 {count} 个站点的计划时间", "success")
                    return redirect(url_for("site_local"))

                elif action == "clear_schedule_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.batch_clear_schedule(selected_ids)
                        flash(f"已清除 {count} 个站点的计划", "success")
                    return redirect(url_for("site_local"))

            result = site_db.list_local_sites(q, page=page, page_size=page_size)
            stats = site_db.get_stats()
            return render_template("site_local.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"本地站点页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_local.html", sites=[], stats={"local_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)

    @app.route("/site-management/generate-logos", methods=["GET", "POST"])
    def site_generate_logos():
        """批量生成Logo"""
        from qmds.utils.logo_generator import LogoGenerator, get_available_fonts
        
        if request.method == "POST":
            action = request.form.get("action", "")
            
            if action == "start":
                # 获取选中的站点ID
                selected_ids = request.form.getlist("selected_ids")
                logo_dir = request.form.get("logo_dir", "").strip()
                font_name = request.form.get("font", "").strip() or None
                
                if not selected_ids:
                    flash("请选择要生成Logo的站点", "error")
                    return redirect(url_for("site_generate_logos"))
                
                if not logo_dir:
                    logo_dir = os.path.join(settings.data_dir, "logos", "setting")
                
                # 创建任务
                task_id = f"logo_gen_{int(time.time())}"
                _task_manager.create(task_id, "generate_logos", f"{len(selected_ids)} sites")
                
                def run_task():
                    site_db = SiteDBClient()
                    try:
                        _task_manager.add_log(task_id, f"任务启动: 批量生成Logo", "info")
                        _task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                        # 获取选中的站点域名
                        domains = []
                        for site_id in selected_ids:
                            site = site_db.get_site_by_id(site_id)
                            if site and site.get("domain"):
                                domains.append(site["domain"])

                        if not domains:
                            _task_manager.update(task_id, status="failed", message="未找到有效域名")
                            _task_manager.add_log(task_id, "未找到有效域名", "error")
                            return

                        _task_manager.add_log(task_id, f"有效域名: {len(domains)}", "info")
                        _task_manager.add_log(task_id, f"输出目录: {logo_dir}", "info")

                        _task_manager.update(task_id, status="running",
                                          message=f"开始生成 {len(domains)} 个Logo",
                                          total=len(domains))

                        # 定义进度回调
                        def progress_callback(current, total, domain, result):
                            if _task_manager.is_stopped(task_id):
                                generator.stop()
                                return
                            status_icon = "✓" if result["success"] else "✗"
                            _task_manager.update(
                                task_id,
                                progress=int(current / total * 100),
                                current=current,
                                message=f"[{current}/{total}] {status_icon} {domain}"
                            )
                            _task_manager.add_log(task_id, f"[{current}/{total}] {status_icon} {domain}", "info" if result["success"] else "warning")

                        # 生成Logo
                        generator = LogoGenerator()
                        result = generator.generate_batch(domains, logo_dir, progress_callback)

                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return

                        # 更新站点的logo路径
                        for domain in domains:
                            logo_path = os.path.join(logo_dir, domain, "logo.png")
                            if os.path.exists(logo_path):
                                site_db.update_site(domain, {"logo": logo_path})

                        _task_manager.update(
                            task_id,
                            status="completed",
                            message=f"完成: 成功 {result['success']}, 失败 {result['failed']}",
                            progress=100,
                            current=result["total"],
                            total=result["total"]
                        )
                        _task_manager.add_log(task_id, f"任务完成: 成功 {result['success']}, 失败 {result['failed']}", "info")

                        if result["errors"]:
                            for error in result["errors"][:5]:
                                _task_manager.add_log(task_id, f"错误: {error}", "error")
                    
                    except Exception as e:
                        log.error(f"Logo生成任务失败: {e}")
                        _task_manager.update(task_id, status="failed", message=str(e))
                        _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                    finally:
                        site_db.close()
                
                threading.Thread(target=run_task, daemon=True).start()
                flash(f"Logo生成任务已启动: {len(selected_ids)} 个站点", "success")
                return redirect(url_for("tasks"))
        
        # GET请求：显示页面
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))
            result = site_db.list_local_sites(q, page=page, page_size=page_size)
            fonts = get_available_fonts()
            default_logo_dir = os.path.join(settings.data_dir, "logos", "setting")
            
            return render_template("site_generate_logos.html", 
                                 sites=result["items"],
                                 total=result["total"],
                                 page=result["page"],
                                 page_size=result["page_size"],
                                 q=q,
                                 fonts=fonts,
                                 default_logo_dir=default_logo_dir)
        except Exception as e:
            log.error(f"Logo生成页面错误: {e}")
            flash(f"加载失败: {e}", "error")
            return render_template("site_generate_logos.html", sites=[], fonts=[], default_logo_dir="")
        finally:
            site_db.close()

    @app.route("/site-management/generate-images", methods=["GET", "POST"])
    def site_generate_images():
        """批量生成图片（Banner + Icon + Logo + 合并）"""
        from qmds.utils.image_generator import ImageGenerator

        # 获取API密钥配置
        site_db = _get_site_db()
        try:
            all_settings = site_db.get_all_settings()
            jisuai_api_key = all_settings.get("jisuai_api_key", "")
            jisuai_icon_api_key = all_settings.get("jisuai_icon_api_key", "")
        except Exception:
            jisuai_api_key = ""
            jisuai_icon_api_key = ""

        has_api_key = bool(jisuai_api_key)

        if request.method == "POST":
            action = request.form.get("action", "")

            if action == "start":
                selected_ids = request.form.getlist("selected_ids")
                output_dir = request.form.get("output_dir", "").strip()
                keyword_source = request.form.get("keyword_source", "main_category")
                manual_keywords = request.form.get("manual_keywords", "").strip()
                gen_banner = request.form.get("gen_banner") == "1"
                gen_icon = request.form.get("gen_icon") == "1"
                gen_logo = request.form.get("gen_logo") == "1"
                gen_combine = request.form.get("gen_combine") == "1"

                if not selected_ids:
                    flash("请选择要生成图片的站点", "error")
                    return redirect(url_for("site_generate_images"))

                if not has_api_key:
                    flash("请先在配置页面设置极速AI API密钥", "error")
                    return redirect(url_for("site_generate_images"))

                if not (gen_banner or gen_icon or gen_logo or gen_combine):
                    flash("请至少选择一项生成内容", "error")
                    return redirect(url_for("site_generate_images"))

                if not output_dir:
                    output_dir = str(settings.data_dir / "logos" / "setting")

                # 解析手动关键词
                manual_kw_list = [k.strip() for k in manual_keywords.split("\n") if k.strip()] if manual_keywords else []

                # 创建任务
                task_id = f"img_gen_{int(time.time())}"
                _task_manager.create(task_id, "generate_images", f"{len(selected_ids)} sites")

                def run_task():
                    _site_db = SiteDBClient()
                    try:
                        # 在任务线程中重新获取API密钥
                        _all_settings = _site_db.get_all_settings()
                        _jisuai_api_key = _all_settings.get("jisuai_api_key", "")
                        _jisuai_icon_api_key = _all_settings.get("jisuai_icon_api_key", "")
                        
                        _task_manager.add_log(task_id, f"任务启动: 批量生成图片", "info")
                        _task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")
                        _task_manager.add_log(task_id, f"生成内容: Banner={gen_banner}, Icon={gen_icon}, Logo={gen_logo}, 合并={gen_combine}", "info")
                        _task_manager.add_log(task_id, f"API密钥长度: {len(_jisuai_api_key) if _jisuai_api_key else 0}", "info")

                        # 获取站点信息和关键词
                        items = []
                        for idx, site_id in enumerate(selected_ids):
                            site = _site_db.get_site_by_id(site_id)
                            if not site or not site.get("domain"):
                                continue

                            domain = site["domain"]

                            # 获取关键词
                            if keyword_source == "manual" and idx < len(manual_kw_list):
                                keyword = manual_kw_list[idx]
                            else:
                                keyword = site.get("main_category", "")
                                # 如果主分类包含 |||，取最后一个 ||| 后的词
                                if keyword and "|||" in keyword:
                                    keyword = keyword.split("|||")[-1].strip()
                                if not keyword:
                                    # 从域名提取关键词
                                    keyword = domain.replace(".com", "").replace(".net", "").replace(".org", "")

                            items.append((domain, keyword))

                        if not items:
                            _task_manager.update(task_id, status="failed", message="未找到有效域名")
                            _task_manager.add_log(task_id, "未找到有效域名", "error")
                            return

                        _task_manager.add_log(task_id, f"有效域名: {len(items)}", "info")
                        _task_manager.add_log(task_id, f"输出目录: {output_dir}", "info")

                        # 显示域名和关键词映射
                        for i, (d, k) in enumerate(items[:10], 1):
                            _task_manager.add_log(task_id, f"  {i}. {d} → {k}", "info")
                        if len(items) > 10:
                            _task_manager.add_log(task_id, f"  ... 还有 {len(items) - 10} 个站点", "info")

                        _task_manager.update(task_id, status="running",
                                           message=f"开始生成 {len(items)} 个站点的图片",
                                           total=len(items))

                        # 定义进度回调
                        def progress_callback(step, current, total, domain, result):
                            if _task_manager.is_stopped(task_id):
                                generator.stop()
                                return

                            step_labels = {
                                "banner": "Banner",
                                "icon": "Icon",
                                "logo": "Logo",
                                "rename": "重命名",
                                "combine": "合并",
                            }
                            step_label = step_labels.get(step, step)
                            status_icon = "✓" if result.get("success") else "✗"
                            skipped = " [跳过]" if result.get("skipped") else ""

                            _task_manager.update(
                                task_id,
                                progress=int(current / total * 100),
                                current=current,
                                message=f"[{step_label}] [{current}/{total}] {status_icon} {domain}{skipped}"
                            )
                            _task_manager.add_log(
                                task_id,
                                f"[{step_label}] [{current}/{total}] {status_icon} {domain}{skipped}",
                                "info" if result.get("success") else "warning"
                            )

                        # 创建生成器并执行
                        generator = ImageGenerator(
                            api_key=_jisuai_api_key,
                            icon_api_key=_jisuai_icon_api_key or _jisuai_api_key
                        )

                        # 运行异步任务
                        try:
                            result = asyncio.run(
                                generator.generate_batch(
                                    items=items,
                                    base_dir=output_dir,
                                    generate_banner=gen_banner,
                                    generate_icon=gen_icon,
                                    generate_logo=gen_logo,
                                    do_combine=gen_combine,
                                    progress_callback=progress_callback
                                )
                            )
                        except RuntimeError as e:
                            # 如果已有事件循环在运行，使用 nest_asyncio
                            if "cannot be called from a running event loop" in str(e):
                                import nest_asyncio
                                nest_asyncio.apply()
                                result = asyncio.run(
                                    generator.generate_batch(
                                        items=items,
                                        base_dir=output_dir,
                                        generate_banner=gen_banner,
                                        generate_icon=gen_icon,
                                        generate_logo=gen_logo,
                                        do_combine=gen_combine,
                                        progress_callback=progress_callback
                                    )
                                )
                            else:
                                raise

                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return

                        # 更新站点的图片路径和状态
                        for domain, _ in items:
                            site_dir = os.path.join(output_dir, domain)
                            has_banner = os.path.isfile(os.path.join(site_dir, "banner.jpg"))
                            has_icon = os.path.isfile(os.path.join(site_dir, "icon.png"))
                            has_logo = os.path.isfile(os.path.join(site_dir, "logo.png"))

                            update_data = {}
                            if has_banner:
                                update_data["banner"] = os.path.join(site_dir, "banner.jpg")
                            if has_icon:
                                update_data["icon"] = os.path.join(site_dir, "icon.png")
                            if has_logo:
                                update_data["logo"] = os.path.join(site_dir, "logo.png")

                            if update_data:
                                _site_db.update_site(domain, update_data)
                            _site_db.update_image_status(domain, has_banner, has_icon, has_logo)

                        # 构建完成消息
                        summary_parts = []
                        if gen_banner:
                            summary_parts.append(f"Banner: {result['banner']['success']}/{len(items)}")
                        if gen_icon:
                            summary_parts.append(f"Icon: {result['icon']['success']}/{len(items)}")
                        if gen_logo:
                            summary_parts.append(f"Logo: {result['logo']['success']}/{len(items)}")
                        if gen_combine:
                            summary_parts.append(f"合并: {result['combine']['success']}/{len(items)}")

                        summary = f"完成: {', '.join(summary_parts)}"
                        _task_manager.update(task_id, status="completed", message=summary, progress=100)
                        _task_manager.add_log(task_id, summary, "info")

                        if result["errors"]:
                            for error in result["errors"][:10]:
                                _task_manager.add_log(task_id, f"错误: {error}", "error")

                    except Exception as e:
                        log.error(f"图片生成任务失败: {e}")
                        _task_manager.update(task_id, status="failed", message=str(e))
                        _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                    finally:
                        _site_db.close()

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"图片生成任务已启动: {len(selected_ids)} 个站点", "success")
                return redirect(url_for("tasks"))

        # GET请求：显示页面
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))
            result = site_db.list_active_sites(q, page=page, page_size=page_size)
            default_output_dir = str(settings.data_dir / "logos" / "setting")

            # 检查每个站点的图片状态 - 优先从DB读取
            for site in result["items"]:
                domain = site.get("domain", "")
                if domain:
                    site["has_banner"] = site.get("has_banner", False)
                    site["has_icon"] = site.get("has_icon", False)
                    site["has_logo"] = site.get("has_logo", False)

            return render_template("site_generate_images.html",
                                 sites=result["items"],
                                 total=result["total"],
                                 page=result["page"],
                                 page_size=result["page_size"],
                                 q=q,
                                 has_api_key=has_api_key,
                                 default_output_dir=default_output_dir)
        except Exception as e:
            log.error(f"图片生成页面错误: {e}")
            flash(f"加载失败: {e}", "error")
            return render_template("site_generate_images.html",
                                 sites=[],
                                 has_api_key=has_api_key,
                                 default_output_dir="")
        finally:
            site_db.close()

    @app.route("/site-management/refresh-image-status", methods=["POST"])
    def refresh_image_status():
        """从文件系统扫描并更新所有站点的图片状态"""
        site_db = _get_site_db()
        try:
            logos_dir = str(settings.data_dir / "logos")
            setting_dir = os.path.join(logos_dir, "setting")
            result = site_db.list_active_sites(page=1, page_size=99999)
            updated = 0

            for site in result["items"]:
                domain = site.get("domain", "")
                if not domain:
                    continue
                primary_dir = os.path.join(logos_dir, domain)
                fallback_dir = os.path.join(setting_dir, domain)
                has_banner = os.path.isfile(os.path.join(primary_dir, "banner.jpg")) or os.path.isfile(os.path.join(fallback_dir, "banner.jpg"))
                has_icon = os.path.isfile(os.path.join(primary_dir, "icon.png")) or os.path.isfile(os.path.join(fallback_dir, "icon.png"))
                has_logo = os.path.isfile(os.path.join(primary_dir, "logo.png")) or os.path.isfile(os.path.join(fallback_dir, "logo.png"))
                site_db.update_image_status(domain, has_banner, has_icon, has_logo)
                updated += 1

            flash(f"已刷新 {updated} 个站点的图片状态", "success")
        except Exception as e:
            log.error(f"刷新图片状态失败: {e}")
            flash(f"刷新失败: {e}", "error")
        finally:
            site_db.close()
        return redirect(url_for("site_generate_images"))

    @app.route("/site-management/reported", methods=["GET", "POST"])
    def site_reported():
        """已报域名管理"""
        site_db = _get_site_db()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))

            if request.method == "POST":
                action = request.form.get("action", "")

                if action == "build_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if not selected_ids:
                        flash("请先勾选要建站的站点", "error")
                        return redirect(url_for("site_reported"))

                    # 创建建站任务
                    task_id = f"build_sites_{int(time.time())}"
                    _task_manager.create(task_id, "build_sites", f"ERP建站 ({len(selected_ids)} sites)")

                    def run_build_task():
                        _site_db = SiteDBClient()
                        try:
                            from qmds.utils.erp_builder import get_erp_builder
                            from qmds.config import settings as qmds_settings

                            _task_manager.add_log(task_id, f"任务启动: ERP建站", "info")
                            _task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                            # 获取站点信息
                            sites = []
                            for sid in selected_ids:
                                site = _site_db.get_site_by_id(sid)
                                if site and site.get("domain"):
                                    sites.append(site)

                            if not sites:
                                _task_manager.update(task_id, status="failed", message="未找到有效站点")
                                _task_manager.add_log(task_id, "未找到有效站点", "error")
                                return

                            total = len(sites)
                            _task_manager.add_log(task_id, f"有效站点: {total}", "info")

                            _task_manager.update(task_id, status="running",
                                              message=f"开始ERP建站: {total} 个站点",
                                              total=total, current=0)

                            # 登录ERP
                            _task_manager.update(task_id, current=0,
                                              message="登录ERP系统...")
                            _task_manager.add_log(task_id, "正在登录ERP系统...", "info")

                            image_root = str(qmds_settings.data_dir / "logos")
                            erp_username = _site_db.get_setting("erp_username")
                            erp_password = _site_db.get_setting("erp_password")
                            if not erp_username or not erp_password:
                                _task_manager.add_log(task_id, "未配置ERP账号密码", "error")
                                raise Exception("未配置ERP账号密码，请在设置中配置 erp_username/erp_password")

                            erp = get_erp_builder(username=erp_username, password=erp_password,
                                                  image_root=image_root)
                            login_result = erp.login()
                            if not login_result["success"]:
                                _task_manager.update(task_id, status="failed",
                                                  message=f"ERP登录失败: {login_result['message']}")
                                _task_manager.add_log(task_id, f"ERP登录失败: {login_result['message']}", "error")
                                return

                            _task_manager.add_log(task_id, "ERP登录成功，开始建站...", "info")
                            log.info("ERP登录成功，开始建站...")

                            success = 0
                            failed = 0
                            errors = []

                            for i, site in enumerate(sites):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                                    return

                                current = i + 1
                                domain = site.get("domain", "")
                                server = site.get("server", "")
                                template = site.get("template", "")
                                title = site.get("title", "")
                                desc = site.get("description", "")
                                address = site.get("address", "")
                                category = site.get("category", "")

                                try:
                                    # 检查必填字段
                                    missing = []
                                    if not server:
                                        missing.append("服务器")
                                    if not template:
                                        missing.append("模板")
                                    if not title:
                                        missing.append("标题")
                                    if not desc:
                                        missing.append("描述")
                                    if not address:
                                        missing.append("地址")
                                    if not category:
                                        missing.append("大类")

                                    if missing:
                                        raise Exception(f"缺少字段: {', '.join(missing)}")

                                    # 建站
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.5) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 开始建站...")

                                    def build_progress(msg):
                                        _task_manager.update(task_id, current=current,
                                                          progress=int((current - 0.3) / total * 100),
                                                          message=f"[{current}/{total}] [{domain}] {msg}")

                                    result = erp.build_site(
                                        domain=domain,
                                        server=server,
                                        template=template,
                                        title=title,
                                        description=desc,
                                        address=address,
                                        category=category,
                                        progress_callback=build_progress
                                    )

                                    if result["success"]:
                                        # 更新状态为已建站
                                        _site_db.update_site(domain, {
                                            "build_status": "已建站",
                                            "build_time": datetime.utcnow().isoformat()
                                        })
                                        success += 1
                                        _task_manager.update(task_id, current=current,
                                                          progress=int(current / total * 100),
                                                          message=f"[{current}/{total}] ✓ [{domain}] 建站成功")
                                        log.info(f"[建站] [{domain}] ✓ 建站成功")
                                    else:
                                        raise Exception(result["message"])

                                except Exception as e:
                                    failed += 1
                                    errors.append(f"{domain}: {e}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✗ [{domain}] 建站失败 - {e}")
                                    _task_manager.add_log(task_id, f"[{current}/{total}] ✗ [{domain}] 建站失败 - {e}", "error")
                                    log.error(f"[建站] [{domain}] 失败: {e}")

                            # 汇总
                            summary = f"ERP建站完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                            _task_manager.update(task_id, status="completed", message=summary,
                                              progress=100, current=total, total=total)
                            _task_manager.add_log(task_id, summary, "info")

                            if errors:
                                log.warning("[建站] 失败明细:")
                                for err in errors:
                                    log.warning(f"  {err}")
                                    _task_manager.add_log(task_id, f"失败: {err}", "error")

                        except Exception as e:
                            log.error(f"[建站] 任务异常: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                            _task_manager.add_log(task_id, f"任务异常: {e}", "error")
                        finally:
                            _site_db.close()

                    threading.Thread(target=run_build_task, daemon=True).start()
                    flash(f"ERP建站任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                    return redirect(url_for("tasks"))

                elif action == "delete_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.batch_update_report_status(selected_ids, "未报")
                        flash(f"已取消上报 {count} 个站点", "success")
                    return redirect(url_for("site_reported"))

                elif action == "update_status":
                    selected_ids = request.form.getlist("selected_ids")
                    if not selected_ids:
                        flash("请先勾选要更新状态的站点", "error")
                        return redirect(url_for("site_reported"))

                    task_id = f"update_status_{int(time.time())}"
                    _task_manager.create(task_id, "update_domain_status", f"{len(selected_ids)} 个站点")

                    def run_task():
                        site_db_inner = SiteDBClient()
                        try:
                            _task_manager.add_log(task_id, f"任务启动: 更新域名状态", "info")
                            _task_manager.add_log(task_id, f"待更新站点: {len(selected_ids)}", "info")

                            settings = site_db_inner.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                _task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            success_count = 0
                            fail_count = 0

                            for site_id in selected_ids:
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped",
                                        message=f"任务已停止: 成功 {success_count} 个, 失败 {fail_count} 个")
                                    _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                    return
                                
                                site = site_db_inner.get_site_by_id(site_id)
                                if not site:
                                    fail_count += 1
                                    _task_manager.add_log(task_id, f"ID {site_id}: 站点不存在", "warning")
                                    continue

                                domain = site.get("domain", "")
                                if not domain:
                                    fail_count += 1
                                    continue

                                try:
                                    info = reporter.fetch_domain_info(domain)
                                    report_id = str(info.get("id") or "")
                                    status_val = info.get("status")
                                    status_label = DOMAIN_STATUS_LABELS.get(status_val, "未知")
                                    site_db_inner.update_domain_status(domain, report_id, str(status_val) if status_val is not None else "")
                                    success_count += 1
                                    _task_manager.add_log(task_id, f"✓ {domain} → {status_label}", "info")
                                    log.info(f"更新域名状态成功: {domain} -> {status_label}")
                                except Exception as e:
                                    fail_count += 1
                                    _task_manager.add_log(task_id, f"✗ {domain} - {e}", "error")
                                    log.error(f"更新域名状态失败: {domain} - {e}")

                            summary = f"完成: 成功 {success_count} 个, 失败 {fail_count} 个"
                            _task_manager.update(
                                task_id,
                                status="completed",
                                message=summary,
                                progress=100
                            )
                            _task_manager.add_log(task_id, summary, "info")
                        except Exception as e:
                            log.error(f"更新域名状态任务失败: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                            _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                        finally:
                            site_db_inner.close()

                    threading.Thread(target=run_task, daemon=True).start()
                    flash(f"更新域名状态任务已启动，可在任务页面查看进度", "info")
                    return redirect(url_for("site_reported"))

                elif action == "review_reported":
                    task_id = f"review_reported_{int(time.time())}"
                    _task_manager.create(task_id, "review_reported", "审查已报域名")

                    def run_review_task():
                        site_db_inner = SiteDBClient()
                        try:
                            _task_manager.add_log(task_id, f"任务启动: 审查已报域名", "info")

                            settings = site_db_inner.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                _task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            all_reported = site_db_inner.list_reported_domains_for_sync()
                            total = len(all_reported)
                            found_count = 0
                            not_found_count = 0
                            error_count = 0

                            _task_manager.add_log(task_id, f"已报域名总数: {total}", "info")

                            for i, site in enumerate(all_reported):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped",
                                        message=f"任务已停止: 审查 {i}/{total}, 平台存在 {found_count}, 不存在 {not_found_count}")
                                    _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                    return
                                
                                domain = site.get("domain", "")
                                if not domain:
                                    continue

                                try:
                                    info = reporter.fetch_domain_info(domain)
                                    if info and info.get("id"):
                                        report_id = str(info.get("id") or "")
                                        status_val = info.get("status")
                                        site_db_inner.update_domain_status(domain, report_id, str(status_val) if status_val is not None else "")
                                        found_count += 1
                                        _task_manager.add_log(task_id, f"[{i+1}/{total}] ✓ {domain} - 平台存在", "info")
                                    else:
                                        site_db_inner.update_site(domain, {"report_status": "未报"})
                                        not_found_count += 1
                                        _task_manager.add_log(task_id, f"[{i+1}/{total}] △ {domain} - 平台不存在，已标记未报", "warning")
                                except Exception:
                                    site_db_inner.update_site(domain, {"report_status": "未报"})
                                    error_count += 1
                                    _task_manager.add_log(task_id, f"[{i+1}/{total}] ✗ {domain} - 查询失败，已标记未报", "error")

                                if (i + 1) % 10 == 0 or i + 1 == total:
                                    _task_manager.update(task_id,
                                        progress=int((i + 1) / total * 100),
                                        message=f"审查中: {i + 1}/{total}")

                            summary = f"审查完成: 平台存在 {found_count}, 不存在 {not_found_count}, 失败 {error_count}"
                            _task_manager.update(
                                task_id,
                                status="completed",
                                message=summary,
                                progress=100
                            )
                            _task_manager.add_log(task_id, summary, "info")
                        except Exception as e:
                            log.error(f"审查已报域名任务失败: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                            _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                        finally:
                            site_db_inner.close()

                    threading.Thread(target=run_review_task, daemon=True).start()
                    flash(f"审查已报域名任务已启动，可在任务页面查看进度", "info")
                    return redirect(url_for("site_reported"))

                elif action == "generate_logos":
                    selected_ids = request.form.getlist("selected_ids")
                    if not selected_ids:
                        flash("请先勾选要生成Logo的站点", "error")
                        return redirect(url_for("site_reported"))

                    task_id = f"logo_gen_reported_{int(time.time())}"
                    _task_manager.create(task_id, "generate_logos", f"批量生成Logo ({len(selected_ids)} sites)")

                    def run_logo_task():
                        from qmds.utils.logo_generator import LogoGenerator
                        _site_db = SiteDBClient()
                        try:
                            logo_dir = str(settings.data_dir / "logos" / "setting")
                            _task_manager.add_log(task_id, f"任务启动: 批量生成Logo (已报域名)", "info")
                            _task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                            # 获取选中的站点域名
                            domains = []
                            for site_id in selected_ids:
                                site = _site_db.get_site_by_id(site_id)
                                if site and site.get("domain"):
                                    domains.append(site["domain"])

                            if not domains:
                                _task_manager.update(task_id, status="failed", message="未找到有效域名")
                                _task_manager.add_log(task_id, "未找到有效域名", "error")
                                return

                            _task_manager.add_log(task_id, f"有效域名: {len(domains)}", "info")
                            _task_manager.add_log(task_id, f"输出目录: {logo_dir}", "info")

                            _task_manager.update(task_id, status="running",
                                              message=f"开始生成 {len(domains)} 个Logo",
                                              total=len(domains))

                            # 定义进度回调
                            def progress_callback(current, total, domain, result):
                                if _task_manager.is_stopped(task_id):
                                    generator.stop()
                                    return
                                status_icon = "✓" if result["success"] else "✗"
                                _task_manager.update(
                                    task_id,
                                    progress=int(current / total * 100),
                                    current=current,
                                    message=f"[{current}/{total}] {status_icon} {domain}"
                                )
                                _task_manager.add_log(task_id, f"[{current}/{total}] {status_icon} {domain}", "info" if result["success"] else "warning")

                            # 生成Logo
                            generator = LogoGenerator()
                            result = generator.generate_batch(domains, logo_dir, progress_callback)

                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", message="任务已停止")
                                _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return

                            # 更新站点的logo路径
                            for domain in domains:
                                logo_path = os.path.join(logo_dir, domain, "logo.png")
                                if os.path.exists(logo_path):
                                    _site_db.update_site(domain, {"logo": logo_path})

                            _task_manager.update(
                                task_id,
                                status="completed",
                                message=f"完成: 成功 {result['success']}, 失败 {result['failed']}",
                                progress=100,
                                current=result["total"],
                                total=result["total"]
                            )
                            _task_manager.add_log(task_id, f"任务完成: 成功 {result['success']}, 失败 {result['failed']}", "info")

                            if result["errors"]:
                                for error in result["errors"][:5]:
                                    _task_manager.add_log(task_id, f"错误: {error}", "error")

                        except Exception as e:
                            log.error(f"Logo生成任务失败: {e}")
                            _task_manager.update(task_id, status="failed", message=str(e))
                            _task_manager.add_log(task_id, f"任务失败: {e}", "error")
                        finally:
                            _site_db.close()

                    threading.Thread(target=run_logo_task, daemon=True).start()
                    flash(f"Logo生成任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                    return redirect(url_for("tasks"))

                elif action == "batch_update":
                    selected_ids = request.form.getlist("selected_ids")
                    field = request.form.get("batch_field", "").strip()
                    value = request.form.get("batch_value", "").strip()

                    if not selected_ids:
                        flash("请先勾选要更新的站点", "error")
                        return redirect(url_for("site_reported"))

                    if not field:
                        flash("请选择要更新的字段", "error")
                        return redirect(url_for("site_reported"))

                    allowed_fields = {"template", "server", "category", "main_category",
                                      "main_data_source_id", "extra_data_source_id",
                                      "title", "description", "address", "build_status"}
                    if field not in allowed_fields:
                        flash("不允许修改该字段", "error")
                        return redirect(url_for("site_reported"))

                    count = site_db.batch_update_fields(selected_ids, field, value)
                    flash(f"已更新 {count} 个站点的 {field} 字段", "success")
                    return redirect(url_for("site_reported"))

            result = site_db.list_reported_sites(q, page=page, page_size=page_size)
            stats = site_db.get_stats()
            return render_template("site_reported.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"已报域名页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_reported.html", sites=[], stats={"reported_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)

    @app.route("/site-management/scheduled", methods=["GET", "POST"])
    def site_scheduled():
        """计划上报管理"""
        site_db = _get_site_db()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))

            if request.method == "POST":
                action = request.form.get("action", "")

                if action == "report_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if not selected_ids:
                        flash("请先勾选要上报的站点", "error")
                        return redirect(url_for("site_scheduled"))

                    task_id = f"report_scheduled_{int(time.time())}"
                    _task_manager.create(task_id, "report_domains", f"计划上报 ({len(selected_ids)} sites)")

                    def run_report_task():
                        _site_db = SiteDBClient()
                        try:
                            settings = _site_db.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            total = len(selected_ids)
                            success = 0
                            failed = 0
                            errors = []

                            for i, site_id in enumerate(selected_ids):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                                    return

                                site = _site_db.get_site_by_id(site_id)
                                if not site:
                                    failed += 1
                                    errors.append(f"ID {site_id}: 站点不存在")
                                    continue

                                domain = (site.get("domain") or "").strip()
                                server = (site.get("server") or "").strip()
                                template = (site.get("template") or "").strip()
                                category_name = (site.get("category") or "").strip()

                                current = i + 1

                                # 检查必填字段
                                missing = []
                                if not domain:
                                    missing.append("域名")
                                if not server:
                                    missing.append("服务器")
                                if not template:
                                    missing.append("模板")
                                if not category_name:
                                    missing.append("大类")

                                if missing:
                                    failed += 1
                                    errors.append(f"{domain or site_id}: 缺少字段 {', '.join(missing)}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✗ {domain or site_id} - 缺少字段")
                                    continue

                                # 映射分类ID
                                category_id = REPORT_CATEGORY_ID_MAP.get(category_name)
                                if not category_id:
                                    failed += 1
                                    errors.append(f"{domain}: 无效分类 {category_name}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✗ {domain} - 无效分类")
                                    continue

                                # 构建上报数据
                                payload = {
                                    "name": domain,
                                    "serverip": server,
                                    "template": template,
                                    "category": category_id,
                                    "categoryTag": None,
                                    "language": None,
                                }

                                try:
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.5) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 正在上报...")
                                    reporter.submit_domain(payload)

                                    # 获取上报后的域名信息
                                    report_id = ""
                                    domain_status = ""
                                    try:
                                        info = reporter.fetch_domain_info(domain)
                                        report_id = str(info.get("id") or "")
                                        status_val = info.get("status")
                                        domain_status = str(status_val) if status_val is not None else ""
                                    except Exception as e:
                                        log.warning(f"获取域名信息失败: {domain} - {e}")

                                    # 更新本地数据库
                                    now = datetime.utcnow().isoformat()
                                    _site_db.update_site(domain, {
                                        "report_status": "已报",
                                        "report_time": now,
                                        "report_id": report_id,
                                        "domain_status": domain_status,
                                        "schedule_enabled": "0",
                                    })

                                    success += 1
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✓ {domain} 上报成功")
                                    log.info(f"[上报] [{domain}] ✓ 上报成功, report_id={report_id}")

                                except Exception as e:
                                    failed += 1
                                    errors.append(f"{domain}: {e}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int(current / total * 100),
                                                      message=f"[{current}/{total}] ✗ {domain} 上报失败 - {e}")
                                    log.error(f"[上报] [{domain}] 失败: {e}")

                            # 汇总
                            summary = f"域名上报完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                            _task_manager.update(task_id, status="completed", message=summary,
                                              progress=100, current=total, total=total)

                            if errors:
                                log.warning("[上报] 失败明细:")
                                for err in errors:
                                    log.warning(f"  {err}")

                        except Exception as e:
                            log.error(f"[上报] 任务异常: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                        finally:
                            _site_db.close()

                    threading.Thread(target=run_report_task, daemon=True).start()
                    flash(f"域名上报任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                    return redirect(url_for("tasks"))

                elif action == "reschedule":
                    selected_ids = request.form.getlist("selected_ids")
                    schedule_time = request.form.get("schedule_time", "")
                    if selected_ids and schedule_time:
                        count = site_db.batch_set_schedule(selected_ids, schedule_time)
                        flash(f"已重新设置 {count} 个站点的计划时间", "success")
                    return redirect(url_for("site_scheduled"))

                elif action == "clear_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.batch_clear_schedule(selected_ids)
                        flash(f"已清除 {count} 个站点的计划", "success")
                    return redirect(url_for("site_scheduled"))

            result = site_db.list_scheduled_sites(q, page=page, page_size=page_size)
            stats = site_db.get_stats()
            return render_template("site_scheduled.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"计划上报页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_scheduled.html", sites=[], stats={"scheduled_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)

    @app.route("/site-management/built", methods=["GET", "POST"])
    def site_built():
        """已建站管理"""
        site_db = _get_site_db()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))

            if request.method == "POST":
                action = request.form.get("action", "")
                selected_ids = request.form.getlist("selected_ids")

                if not selected_ids:
                    flash("请先勾选要操作的站点", "error")
                    return redirect(url_for("site_built", q=q))

                if action == "delete_selected":
                    count = site_db.delete_sites_by_ids(selected_ids)
                    flash(f"已删除 {count} 个站点", "success")
                    return redirect(url_for("site_built", q=q))

                elif action == "update_status":
                    field = request.form.get("status_field", "").strip()
                    value = request.form.get("status_value", "").strip()

                    if not field:
                        flash("请选择要更新的状态字段", "error")
                        return redirect(url_for("site_built", q=q))

                    status_updaters = {
                        "health_status": site_db.batch_update_health_status,
                        "main_data_status": site_db.batch_update_main_data_status,
                        "extra_data_status": site_db.batch_update_extra_data_status,
                        "main_category_status": site_db.batch_update_main_category_status,
                        "auto_category_status": site_db.batch_update_auto_category_status,
                        "plugin_status": site_db.batch_update_plugin_status,
                        "media_status": site_db.batch_update_media_status,
                    }

                    updater = status_updaters.get(field)
                    if not updater:
                        flash("不允许修改该字段", "error")
                        return redirect(url_for("site_built", q=q))

                    count = updater(selected_ids, value)
                    flash(f"已更新 {count} 个站点的 {field}", "success")
                    return redirect(url_for("site_built", q=q))

                # ── 任务化操作 ──────────────────────────────

                task_id = f"built_{action}_{int(time.time())}"
                action_labels = {
                    "health_check": "健康检查",
                    "upload_main": "上传主数据",
                    "upload_extra": "上传补充数据",
                    "set_main_category": "设置主分类",
                    "clear_cache": "清理缓存",
                    "configure_menu": "设置菜单",
                    "configure_sites": "配置站点",
                }
                label = action_labels.get(action, action)
                _task_manager.create(task_id, action, f"{label} ({len(selected_ids)} sites)")

                def run_task():
                    _site_db = SiteDBClient()
                    try:
                        # 获取站点域名
                        _task_manager.update(task_id, message=f"[准备] 读取 {len(selected_ids)} 个站点信息...", current=0, total=len(selected_ids))
                        _task_manager.add_log(task_id, f"任务启动: {label}", "info")
                        _task_manager.add_log(task_id, f"读取 {len(selected_ids)} 个站点信息...", "info")
                        domains = []
                        site_map = {}
                        for sid in selected_ids:
                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", message="任务已停止")
                                _task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return
                            site = _site_db.get_site_by_id(sid)
                            if site and site.get("domain"):
                                domains.append(site["domain"])
                                site_map[site["domain"]] = site

                        if not domains:
                            _task_manager.update(task_id, status="failed", message="未找到有效站点")
                            _task_manager.add_log(task_id, "未找到有效站点", "error")
                            return

                        total = len(domains)
                        _task_manager.update(task_id, status="running",
                                          message=f"[初始化] 共 {total} 个站点待处理",
                                          total=total, current=0)
                        _task_manager.add_log(task_id, f"共 {total} 个站点待处理", "info")

                        success = 0
                        failed = 0
                        errors = []

                        if action in ("upload_main", "upload_extra"):
                            completed = 0
                            def _worker(idx_domain):
                                idx, domain = idx_domain
                                site_info = site_map.get(domain, {})
                                from qmds.utils.site_operator import get_operator
                                operator = get_operator()
                                try:
                                    if action == "upload_main":
                                        data_source = site_info.get("main_data_source_id", "")
                                        if not data_source:
                                            return (domain, False, "未配置主数据源ID", None)
                                        from qmds.utils.site_operator import SiteOperator
                                        try:
                                            data_source = SiteOperator._normalize_data_source_ids(data_source)
                                        except ValueError as e:
                                            return (domain, False, str(e), None)
                                        start_cs = site_info.get("main_data_cs", "0")
                                        if start_cs and start_cs != "0":
                                            _task_manager.add_log(task_id, f"[{domain}] 从断点 {start_cs} 继续上传")
                                        def progress(msg):
                                            _task_manager.add_log(task_id, f"[{domain}] {msg}")
                                            _task_manager.update(task_id, current=idx + 1,
                                                              progress=int((idx + 0.5) / total * 100),
                                                              message=f"[{idx + 1}/{total}] [{domain}] {msg}")
                                        def save_breakpoint(cs_val):
                                            _site_db.update_site(domain, {"main_data_cs": cs_val})
                                        def check_stop():
                                            return _task_manager.is_stopped(task_id)
                                        result = operator.upload_data(domain, data_source, progress, start_cs=start_cs, breakpoint_callback=save_breakpoint, stop_callback=check_stop)
                                        final_cs = result.get("final_cs", "0")
                                        if result["success"]:
                                            return (domain, True, "", final_cs)
                                        return (domain, False, result["message"], final_cs)
                                    elif action == "upload_extra":
                                        extra_source = site_info.get("extra_data_source_id", "")
                                        if not extra_source:
                                            return (domain, False, "未配置补充数据源ID", None)
                                        from qmds.utils.site_operator import SiteOperator
                                        try:
                                            extra_source = SiteOperator._normalize_data_source_ids(extra_source)
                                        except ValueError as e:
                                            return (domain, False, str(e), None)
                                        start_cs = site_info.get("extra_data_cs", "0")
                                        if start_cs and start_cs != "0":
                                            _task_manager.add_log(task_id, f"[{domain}] 从断点 {start_cs} 继续上传")
                                        def progress(msg):
                                            _task_manager.add_log(task_id, f"[{domain}] {msg}")
                                            _task_manager.update(task_id, current=idx + 1,
                                                              progress=int((idx + 0.5) / total * 100),
                                                              message=f"[{idx + 1}/{total}] [{domain}] {msg}")
                                        def save_breakpoint(cs_val):
                                            _site_db.update_site(domain, {"extra_data_cs": cs_val})
                                        def check_stop():
                                            return _task_manager.is_stopped(task_id)
                                        result = operator.upload_data(domain, extra_source, progress, start_cs=start_cs, breakpoint_callback=save_breakpoint, stop_callback=check_stop)
                                        final_cs = result.get("final_cs", "0")
                                        if result["success"]:
                                            return (domain, True, "", final_cs)
                                        return (domain, False, result["message"], final_cs)
                                except Exception as e:
                                    return (domain, False, str(e), None)

                            with ThreadPoolExecutor(max_workers=10) as executor:
                                futures = {executor.submit(_worker, (i, d)): d for i, d in enumerate(domains)}
                                for future in as_completed(futures):
                                    if _task_manager.is_stopped(task_id):
                                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                                        executor.shutdown(wait=False, cancel_futures=True)
                                        return
                                    domain, ok, msg, final_cs = future.result()
                                    completed += 1
                                    if ok:
                                        if action == "upload_main":
                                            _site_db.update_site(domain, {"main_data_status": "已上传", "main_data_time": datetime.utcnow().isoformat(), "main_data_cs": "0"})
                                        elif action == "upload_extra":
                                            _site_db.update_site(domain, {"extra_data_status": "已上传", "extra_data_time": datetime.utcnow().isoformat(), "extra_data_cs": "0"})
                                        success += 1
                                        _task_manager.add_log(task_id, f"[{domain}] ✓ 上传成功", "info")
                                        log.info(f"[{label}] [{domain}] ✓ 成功")
                                    else:
                                        if final_cs and final_cs != "0":
                                            cs_field = "main_data_cs" if action == "upload_main" else "extra_data_cs"
                                            _site_db.update_site(domain, {cs_field: final_cs})
                                            _task_manager.add_log(task_id, f"[{domain}] 断点已保存: {final_cs}", "warning")
                                        failed += 1
                                        errors.append(f"{domain}: {msg}")
                                        _task_manager.add_log(task_id, f"[{domain}] ✗ 上传失败: {msg}", "error")
                                        log.error(f"[{label}] [{domain}] 失败: {msg}")
                                    _task_manager.update(task_id, current=completed,
                                                      progress=int(completed / total * 100),
                                                      message=f"[{completed}/{total}] [{domain}] {'成功' if ok else '失败'}")

                        elif action == "configure_sites":
                            for i, domain in enumerate(domains):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                                    return
                                current = i + 1
                                site_info = site_map.get(domain, {})
                                try:
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.5) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 登录站点...")
                                    login_result = operator.login(domain)
                                    if not login_result["success"]:
                                        raise Exception(f"登录失败: {login_result['message']}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.4) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 配置WP Rocket...")
                                    rocket_result = operator.process_rocket(domain)
                                    log.info(f"[配置站点] [{domain}] WP Rocket: {rocket_result['message']}")
                                    if not rocket_result["success"]:
                                        log.warning(f"[配置站点] [{domain}] WP Rocket配置失败: {rocket_result['message']}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.3) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 配置Yoast SEO...")
                                    yoast_result = operator.process_yoast(domain)
                                    log.info(f"[配置站点] [{domain}] Yoast: {yoast_result['message']}")
                                    if not yoast_result["success"]:
                                        log.warning(f"[配置站点] [{domain}] Yoast配置失败: {yoast_result['message']}")
                                    _site_db.update_site(domain, {"plugin_status": "已配置",
                                                                  "plugin_time": datetime.utcnow().isoformat()})
                                    log.info(f"[配置站点] [{domain}] ✓ 插件已配置")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.2) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 配置媒体...")
                                    from qmds.config import settings as qmds_settings
                                    media_root = str(qmds_settings.data_dir / "logos")
                                    media_result = operator.configure_media(domain, media_root)
                                    log.info(f"[配置站点] [{domain}] 媒体: {media_result['message']}")
                                    _site_db.update_site(domain, {"media_status": "已配置",
                                                                  "media_time": datetime.utcnow().isoformat()})
                                    log.info(f"[配置站点] [{domain}] ✓ 媒体已配置")
                                    success += 1
                                    log.info(f"[配置站点] [{domain}] ✓ 成功")
                                except Exception as e:
                                    failed += 1
                                    errors.append(f"{domain}: {e}")
                                    log.error(f"[配置站点] [{domain}] 失败: {e}")
                                _task_manager.update(task_id, current=current,
                                                  progress=int(current / total * 100),
                                                  message=f"[{current}/{total}] [{domain}] 完成")

                        else:
                          for i, domain in enumerate(domains):
                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", message="任务已停止")
                                return

                            current = i + 1
                            site_info = site_map.get(domain, {})

                            try:
                                # ── 阶段1: 准备 ──
                                _task_manager.update(task_id, current=current,
                                                  progress=int((current - 0.5) / total * 100),
                                                  message=f"[{current}/{total}] [{domain}] 读取站点信息...")
                                template = site_info.get("template", "-")
                                server = site_info.get("server", "-")
                                log.info(f"[{label}] [{domain}] 模板={template}, 服务器={server}")

                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                                    return

                                # ── 阶段2: 执行 ──
                                _task_manager.update(task_id, current=current,
                                                  progress=int((current - 0.3) / total * 100),
                                                  message=f"[{current}/{total}] [{domain}] 正在执行{label}...")

                                if action == "health_check":
                                    log.info(f"[健康检查] [{domain}] 开始健康检查...")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.2) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 正在检查站点可访问性...")
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    result = operator.health_check(domain)
                                    if result["success"]:
                                        _site_db.update_site(domain, {"health_status": "正常"})
                                        log.info(f"[健康检查] [{domain}] ✓ 站点正常 (HTTP {result['status_code']})")
                                    else:
                                        log.warning(f"[健康检查] [{domain}] ✗ {result['message']}")
                                        raise Exception(result["message"])

                                elif action == "set_main_category":
                                    log.info(f"[设置主分类] [{domain}] 开始设置主分类...")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.2) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 读取分类配置...")
                                    main_cat = site_info.get("main_category", "")
                                    log.info(f"[设置主分类] [{domain}] 主分类: {main_cat}")
                                    if not main_cat:
                                        raise Exception("未配置主分类")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.15) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 登录站点...")
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    login_result = operator.login(domain)
                                    if not login_result["success"]:
                                        raise Exception(f"登录失败: {login_result['message']}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.1) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 设置分类中...")
                                    def main_cat_progress(msg):
                                        _task_manager.update(task_id, current=current,
                                                          progress=int((current - 0.05) / total * 100),
                                                          message=f"[{current}/{total}] [{domain}] {msg}")
                                    set_result = operator.set_main_category(domain, main_cat, main_cat_progress)
                                    if not set_result["success"]:
                                        raise Exception(set_result["message"])
                                    _site_db.update_site(domain, {"main_category_status": "已上传",
                                                                  "main_category_time": datetime.utcnow().isoformat()})
                                    log.info(f"[设置主分类] [{domain}] ✓ 主分类已设置")

                                elif action == "clear_cache":
                                    log.info(f"[清理缓存] [{domain}] 开始清理缓存...")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.3) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 登录站点...")
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    login_result = operator.login(domain)
                                    if not login_result["success"]:
                                        raise Exception(f"登录失败: {login_result['message']}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.2) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 清理WP Rocket缓存...")
                                    cache_result = operator.clear_cache(domain)
                                    if not cache_result["success"]:
                                        raise Exception(cache_result["message"])
                                    log.info(f"[清理缓存] [{domain}] ✓ 缓存已清理")

                                elif action == "configure_menu":
                                    log.info(f"[设置菜单] [{domain}] 开始设置菜单...")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.2) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 读取菜单配置...")
                                    auto_cat = site_info.get("auto_category_status", "未配置")
                                    log.info(f"[设置菜单] [{domain}] 当前菜单状态: {auto_cat}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.15) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 登录站点...")
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    login_result = operator.login(domain)
                                    if not login_result["success"]:
                                        raise Exception(f"登录失败: {login_result['message']}")
                                    _task_manager.update(task_id, current=current,
                                                      progress=int((current - 0.1) / total * 100),
                                                      message=f"[{current}/{total}] [{domain}] 获取WP分类并构建菜单...")
                                    wp_password = _site_db.get_setting("wp_password") or os.environ.get("WP_PASSWORD", "f!XsS$J2WneOkMyUgQ")
                                    import sys as _sys
                                    _ysqd_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))), "YSQD")
                                    if _ysqd_path not in _sys.path:
                                        _sys.path.insert(0, _ysqd_path)
                                    from wp_menu_config import WpMenuConfigurator
                                    configurator = WpMenuConfigurator(wp_password)
                                    configurator.configure(domain)
                                    _site_db.update_site(domain, {"auto_category_status": "已配置",
                                                                  "auto_category_time": datetime.utcnow().isoformat()})
                                    log.info(f"[设置菜单] [{domain}] ✓ 菜单已配置")

                                # ── 阶段3: 完成 ──
                                _task_manager.update(task_id, current=current,
                                                  progress=int(current / total * 100),
                                                  message=f"[{current}/{total}] ✓ [{domain}] {label}完成")
                                success += 1

                            except Exception as e:
                                failed += 1
                                errors.append(f"{domain}: {e}")
                                log.error(f"[{label}] [{domain}] 失败: {e}")
                                _task_manager.update(task_id, current=current,
                                                  progress=int(current / total * 100),
                                                  message=f"[{current}/{total}] ✗ [{domain}] {label}失败 - {e}")

                        # ── 汇总 ──
                        summary = f"{label}完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                        _task_manager.update(task_id, status="completed", message=summary,
                                          progress=100, current=total, total=total)
                        _task_manager.add_log(task_id, summary, "info")

                        if errors:
                            log.warning(f"[{label}] 失败明细:")
                            for err in errors:
                                log.warning(f"  {err}")
                                _task_manager.add_log(task_id, f"失败: {err}", "error")

                    except Exception as e:
                        log.error(f"[{label}] 任务异常: {e}")
                        _task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                        _task_manager.add_log(task_id, f"任务异常: {e}", "error")
                    finally:
                        _site_db.close()

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"{label}任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("tasks"))

            result = site_db.list_built_sites(q, page=page, page_size=page_size)
            stats = site_db.get_stats()
            built_stats = site_db.get_built_stats()
            return render_template("site_built.html", sites=result["items"], stats=stats, built_stats=built_stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"已建站页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_built.html", sites=[], stats={"built_sites": 0}, built_stats={}, q=q,
                                   total=0, page=1, page_size=20)

    @app.route("/site-management/<site_id>/edit", methods=["GET", "POST"])
    def site_edit(site_id):
        """编辑站点"""
        site_db = _get_site_db()
        try:
            site = site_db.get_site_by_id(site_id)
            if not site:
                flash("站点不存在", "error")
                return redirect(url_for("site_management"))

            if request.method == "POST":
                updates = {
                    "domain": request.form.get("domain", ""),
                    "template": request.form.get("template", ""),
                    "server": request.form.get("server", ""),
                    "category": request.form.get("category", ""),
                    "main_category": request.form.get("main_category", ""),
                    "main_data_source_id": request.form.get("main_data_source_id", ""),
                    "extra_data_source_id": request.form.get("extra_data_source_id", ""),
                    "title": request.form.get("title", ""),
                    "description": request.form.get("description", ""),
                    "address": request.form.get("address", ""),
                    "report_status": request.form.get("report_status", ""),
                    "build_status": request.form.get("build_status", ""),
                    "schedule_enabled": request.form.get("schedule_enabled", "0"),
                    "schedule_time": request.form.get("schedule_time", ""),
                }
                site_db.update_site_by_id(site_id, updates)
                flash("站点信息已更新", "success")
                return redirect(url_for("site_edit", site_id=site_id))

            return render_template("site_edit.html", site=site)
        except Exception as e:
            log.error(f"编辑站点错误: {e}")
            flash(f"操作失败: {e}", "error")
            return redirect(url_for("site_management"))

    @app.route("/site-management/<site_id>/delete", methods=["POST"])
    def site_delete(site_id):
        """删除站点"""
        site_db = _get_site_db()
        try:
            site = site_db.get_site_by_id(site_id)
            if site:
                site_db.delete_site(site.get("domain", ""))
                flash("站点已删除", "success")
        except Exception as e:
            log.error(f"删除站点错误: {e}")
            flash(f"删除失败: {e}", "error")
        return redirect(url_for("site_management"))

    @app.route("/site-management/<site_id>/report", methods=["POST"])
    def site_report(site_id):
        """将站点标记为已上报"""
        site_db = _get_site_db()
        try:
            site_db.update_site_by_id(site_id, {"report_status": "已报"})
            flash("站点已标记为已上报", "success")
        except Exception as e:
            log.error(f"上报站点错误: {e}")
            flash(f"上报失败: {e}", "error")
        return redirect(url_for("site_local"))

    @app.route("/site-management/export-weekly", methods=["GET"])
    def site_export_weekly():
        """导出本周已报域名Excel"""
        site_db = _get_site_db()
        try:
            keyword = request.args.get("q", "").strip()
            export_data = site_db.export_reported_weekly(keyword)

            if not export_data:
                flash("本周没有可导出的已报数据", "error")
                return redirect(url_for("site_reported", q=keyword))

            from io import BytesIO
            output = BytesIO()
            pd.DataFrame(export_data).to_excel(output, index=False)
            output.seek(0)

            from datetime import timedelta
            now = datetime.utcnow()
            week_start = now - timedelta(days=now.weekday())
            filename = f"weekly_report_{week_start.strftime('%Y%m%d')}.xlsx"

            from flask import send_file
            return send_file(
                output,
                as_attachment=True,
                download_name=filename,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        except Exception as e:
            log.error(f"导出本周数据错误: {e}")
            flash(f"导出失败: {e}", "error")
            return redirect(url_for("site_reported"))

    @app.route("/site-management/batch-update", methods=["POST"])
    def site_batch_update():
        """批量更新站点字段"""
        site_db = _get_site_db()
        try:
            selected_ids = request.form.getlist("selected_ids")
            field = request.form.get("batch_field", "").strip()
            value = request.form.get("batch_value", "").strip()

            if not selected_ids:
                flash("请先勾选要更新的站点", "error")
                return redirect(url_for("site_local"))

            if not field:
                flash("请选择要更新的字段", "error")
                return redirect(url_for("site_local"))

            count = site_db.batch_update_fields(selected_ids, field, value)
            flash(f"已更新 {count} 个站点的{field}字段", "success")
        except Exception as e:
            log.error(f"批量更新错误: {e}")
            flash(f"更新失败: {e}", "error")
        return redirect(url_for("site_local"))

    @app.route("/site-management/<site_id>/detail", methods=["GET"])
    def site_detail(site_id):
        """站点详情"""
        site_db = _get_site_db()
        try:
            site = site_db.get_site_by_id(site_id)
            if not site:
                flash("站点不存在", "error")
                return redirect(url_for("site_management"))
            return render_template("site_detail.html", site=site)
        except Exception as e:
            log.error(f"获取站点详情错误: {e}")
            flash(f"获取详情失败: {e}", "error")
            return redirect(url_for("site_management"))

    # === 配置管理路由 ===

    @app.route("/config", methods=["GET", "POST"])
    def site_config():
        """网页配置页面"""
        site_db = _get_site_db()
        try:
            if request.method == "POST":
                # 记录表单提交的数据
                log.info(f"表单提交数据: {dict(request.form)}")
                
                # 保存配置（跳过值为...的密码字段）
                settings_to_save = {
                    "report_username": request.form.get("report_username", ""),
                    "report_password": request.form.get("report_password", ""),
                    "erp_username": request.form.get("erp_username", ""),
                    "erp_password": request.form.get("erp_password", ""),
                    "wp_password": request.form.get("wp_password", ""),
                    "media_root": request.form.get("media_root", ""),
                    "jisuai_api_key": request.form.get("jisuai_api_key", ""),
                    "jisuai_icon_api_key": request.form.get("jisuai_icon_api_key", ""),
                    "seo_proxy": request.form.get("seo_proxy", ""),
                    "seo_api_key": request.form.get("seo_api_key", ""),
                    "rocket_cleanup_frequency": request.form.get("rocket_cleanup_frequency", "daily"),
                    "rocket_preload_links": request.form.get("rocket_preload_links", "1"),
                    "rocket_minify_css": request.form.get("rocket_minify_css", "1"),
                    "rocket_minify_js": request.form.get("rocket_minify_js", "1"),
                    "rocket_lazyload": request.form.get("rocket_lazyload", "1"),
                    "rocket_remove_unused_css": request.form.get("rocket_remove_unused_css", "1"),
                }
                log.info(f"保存配置: {list(settings_to_save.keys())}")
                for key, value in settings_to_save.items():
                    # 跳过值为点号的密码字段（表示未修改）
                    if value and all(c == '.' for c in value):
                        log.info(f"  跳过 {key} (值为点号，未修改)")
                        continue
                    result = site_db.set_setting(key, value)
                    log.info(f"  set_setting({key}) = {result}")
                
                # 验证保存结果
                saved_settings = site_db.get_all_settings()
                log.info(f"保存后的配置keys: {list(saved_settings.keys())}")
                log.info(f"jisuai_api_key 保存结果: {saved_settings.get('jisuai_api_key', 'NOT FOUND')}")
                
                flash("配置已保存", "success")
                return redirect(url_for("site_config"))

            # 获取当前配置
            settings = site_db.get_all_settings()
            return render_template("site_config.html", settings=settings)
        except Exception as e:
            log.error(f"配置页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_config.html", settings={})

    @app.route("/config/templates", methods=["GET", "POST"])
    def config_templates():
        """模板选项管理"""
        site_db = _get_site_db()
        try:
            if request.method == "POST":
                action = request.form.get("action", "")
                if action == "add":
                    name = request.form.get("name", "").strip()
                    if name:
                        if site_db.add_template_option(name):
                            flash(f"模板 '{name}' 已添加", "success")
                        else:
                            flash(f"模板 '{name}' 已存在", "error")
                elif action == "delete":
                    name = request.form.get("name", "").strip()
                    if name:
                        site_db.delete_template_option(name)
                        flash(f"模板 '{name}' 已删除", "success")
                return redirect(url_for("config_templates"))

            templates = site_db.get_template_options()
            return render_template("site_options.html", option_type="模板", options=templates)
        except Exception as e:
            log.error(f"模板选项错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_options.html", option_type="模板", options=[])

    @app.route("/config/servers", methods=["GET", "POST"])
    def config_servers():
        """服务器选项管理"""
        site_db = _get_site_db()
        try:
            if request.method == "POST":
                action = request.form.get("action", "")
                if action == "add":
                    name = request.form.get("name", "").strip()
                    if name:
                        if site_db.add_server_option(name):
                            flash(f"服务器 '{name}' 已添加", "success")
                        else:
                            flash(f"服务器 '{name}' 已存在", "error")
                elif action == "delete":
                    name = request.form.get("name", "").strip()
                    if name:
                        site_db.delete_server_option(name)
                        flash(f"服务器 '{name}' 已删除", "success")
                return redirect(url_for("config_servers"))

            servers = site_db.get_server_options()
            return render_template("site_options.html", option_type="服务器", options=servers)
        except Exception as e:
            log.error(f"服务器选项错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_options.html", option_type="服务器", options=[])

    @app.route("/config/categories", methods=["GET", "POST"])
    def config_categories():
        """主分类选项管理"""
        site_db = _get_site_db()
        try:
            if request.method == "POST":
                action = request.form.get("action", "")
                if action == "add":
                    name = request.form.get("name", "").strip()
                    if name:
                        if site_db.add_main_category_option(name):
                            flash(f"主分类 '{name}' 已添加", "success")
                        else:
                            flash(f"主分类 '{name}' 已存在", "error")
                elif action == "delete":
                    name = request.form.get("name", "").strip()
                    if name:
                        site_db.delete_main_category_option(name)
                        flash(f"主分类 '{name}' 已删除", "success")
                return redirect(url_for("config_categories"))

            categories = site_db.get_main_category_options()
            return render_template("site_options.html", option_type="主分类", options=[c["name"] for c in categories])
        except Exception as e:
            log.error(f"主分类选项错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_options.html", option_type="主分类", options=[])

    # === 订单分析路由 ===

    _order_log_queues = {}

    def _get_order_db():
        try:
            from qmds.db.order_db import OrderDBClient
            return OrderDBClient()
        except Exception as e:
            log.error(f"订单数据库连接失败: {e}")
            return None

    def _order_log(task_id, msg, level="info"):
        q = _order_log_queues.get(task_id)
        if q is not None:
            q.put({"msg": msg, "level": level, "time": time.strftime("%H:%M:%S")})
        # 同时打印到终端
        log_func = getattr(log, level, log.info)
        log_func(f"[{task_id}] {msg}")

    def _derive_domain(domain):
        d = domain.strip().lower()
        if not d.startswith("www."):
            d = "www." + d
        return d

    def _wp_login(session, domain, password, max_retries=2):
        from bs4 import BeautifulSoup
        site_url = f"https://{_derive_domain(domain)}"
        name = domain.replace('www.', '').replace('.com', '').strip()
        username = f"Ad{name}Min"
        login_url = f"{site_url}/bbwllogin/"
        data = {"log": username, "pwd": password, "wp-submit": "Log In",
                "redirect_to": f"{site_url}/wp-admin/", "testcookie": "1"}
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

    def _parse_order_row(row_html):
        import html as html_mod
        m = re.search(r'<tr[^>]*id="order-(\d+)"', row_html)
        if not m:
            return None
        order_id = int(m.group(1))
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
        return {"order_id": order_id, "order_time": date_created, "order_status": status, "order_amount": total}

    class RetryableError(Exception):
        """可重试的错误（登录失败、请求超时等）"""
        pass

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
        
        # 解析商品表格
        items_table = soup.find("table", class_="woocommerce_order_items")
        if items_table:
            tbody = items_table.find("tbody")
            if tbody:
                for row in tbody.find_all("tr"):
                    item = {}
                    
                    # 商品名称
                    name_cell = row.find("td", class_="name")
                    if name_cell:
                        name_link = name_cell.find("a")
                        if name_link:
                            item["product_name"] = name_link.get_text(strip=True)
                        # SKU
                        sku_div = name_cell.find("div", class_="view") or name_cell.find("small")
                        if sku_div:
                            sku_text = sku_div.get_text(strip=True)
                            if "SKU" in sku_text:
                                item["sku"] = sku_text.replace("SKU:", "").strip()
                    
                    # 数量
                    qty_cell = row.find("td", class_="quantity")
                    if qty_cell:
                        qty_text = qty_cell.get_text(strip=True)
                        try:
                            item["quantity"] = int(qty_text)
                        except ValueError:
                            item["quantity"] = 1
                    
                    # 金额
                    total_cell = row.find("td", class_="line_total") or row.find("td", class_="line_cost")
                    if total_cell:
                        total_text = total_cell.get_text(strip=True)
                        # 提取数字
                        import re
                        nums = re.findall(r'[\d,.]+', total_text)
                        if nums:
                            try:
                                item["subtotal"] = float(nums[-1].replace(",", ""))
                            except ValueError:
                                item["subtotal"] = 0
                    
                    if item.get("product_name"):
                        result["items"].append(item)
        
        # 解析账单信息区域
        order_data_columns = soup.find_all("div", class_="order_data_column")
        for column in order_data_columns:
            heading = column.find("h3")
            if not heading:
                continue
            
            heading_text = heading.get_text(strip=True).lower()
            
            # 账单信息
            if "billing" in heading_text or "账单" in heading_text:
                # 邮箱
                email_link = column.find("a", href=re.compile(r"^mailto:"))
                if email_link:
                    result["customer_email"] = email_link.get("href", "").replace("mailto:", "").strip()
                
                # 解析所有段落
                paragraphs = column.find_all("p")
                for p in paragraphs:
                    text = p.get_text(strip=True)
                    
                    # 邮箱（备用解析）
                    if not result["customer_email"] and "@" in text:
                        email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', text)
                        if email_match:
                            result["customer_email"] = email_match.group(0)
                    
                    # 姓名
                    if "name" in text.lower() or "姓名" in text:
                        name_match = re.search(r':\s*(.+)', text)
                        if name_match:
                            result["customer_name"] = name_match.group(1).strip()
                    
                    # 地址解析
                    if "address" in text.lower() or "地址" in text:
                        address_lines = text.split("\n")
                        if len(address_lines) >= 2:
                            result["billing_address"]["address_1"] = address_lines[1].strip()
                    
                    # 电话
                    if "phone" in text.lower() or "电话" in text:
                        phone_match = re.search(r'[\d\s\-\+\(\)]+', text)
                        if phone_match:
                            result["billing_address"]["phone"] = phone_match.group(0).strip()
            
            # 收货信息
            elif "shipping" in heading_text or "收货" in heading_text:
                paragraphs = column.find_all("p")
                for p in paragraphs:
                    text = p.get_text(strip=True)
                    
                    # 姓名
                    if "name" in text.lower() or "姓名" in text:
                        name_match = re.search(r':\s*(.+)', text)
                        if name_match:
                            result["shipping_address"]["name"] = name_match.group(1).strip()
                    
                    # 地址
                    if "address" in text.lower() or "地址" in text:
                        address_lines = text.split("\n")
                        if len(address_lines) >= 2:
                            result["shipping_address"]["address_1"] = address_lines[1].strip()
        
        return result

    def _fetch_order_details_for_server(server, orders, log_func, wp_password, task_id=None):
        """获取单个服务器的订单详情"""
        import requests as req
        req.packages.urllib3.disable_warnings()
        
        domain = _derive_domain(server["domain"])
        site_url = f"https://{domain}"
        ip = server.get("ip", "")
        
        if not ip:
            log_func("  [ERR] 无IP", "error")
            return {"success": 0, "failed": 0, "deduplicated": 0}
        
        order_db = _get_order_db()
        if not order_db:
            log_func("  [ERR] 数据库连接失败", "error")
            return {"success": 0, "failed": 0, "deduplicated": 0}
        
        # 创建会话并登录
        session = req.Session()
        session.verify = False
        try:
            _wp_login(session, domain, wp_password)
            log_func(f"  Login OK")
        except RuntimeError as e:
            log_func(f"  [ERR] {e}", "error")
            raise RetryableError(f"登录失败: {e}")
        
        success = 0
        failed = 0
        deduplicated = 0
        total = len(orders)
        
        for i, order in enumerate(orders, 1):
            if task_id and _task_manager.is_stopped(task_id):
                log_func(f"  [STOP] 任务被停止")
                break
            
            # 从订单时间中提取订单ID（如果有）
            # 否则需要从详情页URL中获取
            order_time = order.get("order_time", "")
            order_domain = order.get("domain", domain)
            
            # 访问订单详情页
            # WooCommerce订单详情页URL格式：/wp-admin/post.php?post={order_id}&action=edit
            # 但由于我们没有order_id，需要通过其他方式获取
            # 这里我们使用订单列表页中已有的信息，跳过详情获取
            # 或者可以通过搜索订单时间来定位
            
            # 尝试通过订单时间搜索
            search_url = f"{site_url}/wp-admin/edit.php?post_type=shop_order&s={order_time}"
            try:
                r = session.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if r.status_code == 200:
                    # 从搜索结果中提取订单ID
                    import re
                    order_id_match = re.search(r'post=(\d+)', r.text)
                    if order_id_match:
                        order_id = order_id_match.group(1)
                        
                        # 访问订单详情页
                        detail_url = f"{site_url}/wp-admin/post.php?post={order_id}&action=edit"
                        detail_resp = session.get(detail_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                        
                        if detail_resp.status_code == 200:
                            # 解析详情
                            detail = _parse_order_detail(detail_resp.text)
                            
                            # 更新数据库
                            result = order_db.update_order_details(
                                ip=ip,
                                domain=order_domain,
                                order_time=order_time,
                                customer_email=detail.get("customer_email", ""),
                                order_amount=order.get("order_amount", 0),
                                items=detail.get("items", []),
                                billing_address=detail.get("billing_address", {}),
                                shipping_address=detail.get("shipping_address", {})
                            )
                            
                            if result.get("updated"):
                                success += 1
                                email_display = detail.get("customer_email", "N/A")
                                items_count = len(detail.get("items", []))
                                log_func(f"  [{i}/{total}] ✅ 订单详情已更新 - {items_count}件商品, 邮箱: {email_display}")
                            
                            if result.get("deduplicated", 0) > 0:
                                deduplicated += result["deduplicated"]
                                log_func(f"  [{i}/{total}] ⚠️ 去重: 删除 {result['deduplicated']} 条重复订单")
                        else:
                            failed += 1
                            log_func(f"  [{i}/{total}] ❌ 详情页访问失败: HTTP {detail_resp.status_code}")
                    else:
                        failed += 1
                        log_func(f"  [{i}/{total}] ❌ 未找到订单ID")
                else:
                    failed += 1
                    log_func(f"  [{i}/{total}] ❌ 搜索失败: HTTP {r.status_code}")
            except Exception as e:
                failed += 1
                log_func(f"  [{i}/{total}] ❌ 请求失败: {e}")
            
            # 请求间隔
            time.sleep(0.5)
        
        return {"success": success, "failed": failed, "deduplicated": deduplicated}

    def _fetch_orders_for_server(server, year, month, log_func, date_from=None, date_to=None, wp_password="", task_id=None):
        import requests as req
        req.packages.urllib3.disable_warnings()
        domain = _derive_domain(server["domain"])
        site_url = f"https://{domain}"
        ip = server.get("ip", "")
        if not ip:
            log_func("  [ERR] 无IP", "error")
            return 0
        order_db = _get_order_db()
        if not order_db:
            log_func("  [ERR] 数据库连接失败", "error")
            return 0
        order_db.ensure_orders_indexes(ip)
        m_param = f"{year}{month:02d}"
        page = 1
        total_fetched = 0
        session = req.Session()
        session.verify = False
        try:
            _wp_login(session, domain, wp_password)
            log_func(f"  Login OK")
        except RuntimeError as e:
            log_func(f"  [ERR] {e}", "error")
            raise RetryableError(f"登录失败: {e}")
        while True:
            if task_id and _task_manager.is_stopped(task_id):
                log_func(f"  [STOP] 任务被停止")
                return total_fetched
            log_func(f"  第 {page} 页...")
            url = f"{site_url}/wp-admin/admin.php?page=wc-orders&m={m_param}&paged={page}"
            try:
                r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            except Exception as e:
                log_func(f"  [ERR] 请求失败: {e}", "error")
                raise RetryableError(f"请求失败: {e}")
            if r.status_code != 200:
                log_func(f"  [ERR] HTTP {r.status_code}", "error")
                break
            rows = list(re.finditer(r'<tr[^>]*id="order-\d+"[^>]*>.*?</tr>', r.text, re.S))
            log_func(f"    解析到 {len(rows)} 行")
            if not rows:
                break
            for m in rows:
                parsed = _parse_order_row(m.group(0))
                if not parsed:
                    continue
                status_lower = (parsed.get("order_status") or "").lower()
                if status_lower in ("on hold", "on-hold"):
                    continue
                order_d = parsed["order_time"][:10] if parsed["order_time"] else ""
                if date_from and date_to and (order_d < date_from or order_d > date_to):
                    continue
                order_db.insert_order(ip, server["domain"], parsed["order_time"], parsed["order_status"], parsed["order_amount"], server.get("main_category", ""))
            total_fetched += len(rows)
            log_func(f"    写入 {len(rows)} 条")
            if len(rows) < 20:
                break
            page += 1
            time.sleep(0.5)
        return total_fetched

    def _run_fetch_all(year, month, task_id, date_from="", date_to=""):
        order_db = _get_order_db()
        if not order_db:
            _order_log(task_id, "数据库连接失败", "error")
            _task_manager.update(task_id, status="failed", message="数据库连接失败")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        servers = order_db.get_servers()
        if not servers:
            _order_log(task_id, "没有配置任何服务器", "warn")
            _task_manager.update(task_id, status="completed", message="没有配置任何服务器")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        # 读取 WordPress 密码
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
            _task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        _order_log(task_id, f"开始并发抓取 {len(servers)} 台服务器 (5线程)")
        _task_manager.update(task_id, status="running", message=f"开始抓取 {len(servers)} 台服务器")
        
        failed_servers = []  # 记录可重试失败的服务器
        servers_with_orders = []  # 记录有订单的服务器
        done = 0
        total_servers = len(servers)
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _log_wrapper(msg, level="info"):
            _order_log(task_id, msg, level)
        
        # 第一轮抓取
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
            for future in as_completed(futures):
                if _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{total_servers} 台服务器")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                svr = futures[future]
                done += 1
                try:
                    cnt = future.result()
                    if cnt > 0:  # 记录有订单的服务器
                        servers_with_orders.append(svr)
                    _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ✅ {cnt} 条")
                except RetryableError as e:
                    _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ⚠️ {e}，待重试", "warning")
                    failed_servers.append(svr)
                except Exception as e:
                    _order_log(task_id, f"[{done}/{total_servers}] [{svr['name']}] ❌ {e}", "error")
                
                if not _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, progress=int(done / total_servers * 50), 
                                        message=f"第一轮: {done}/{total_servers} 台服务器")
        
        # 重试失败的服务器（最多3次）
        if failed_servers and not _task_manager.is_stopped(task_id):
            _order_log(task_id, f"\n{'='*50}")
            _order_log(task_id, f"开始重试失败的服务器 ({len(failed_servers)} 台)", "warning")
            _order_log(task_id, f"{'='*50}")
            
            for attempt in range(1, 4):
                if not failed_servers:
                    break
                if _task_manager.is_stopped(task_id):
                    break
                
                _order_log(task_id, f"\n--- 第 {attempt}/3 次重试 ({len(failed_servers)} 台) ---")
                time.sleep(2)  # 重试前等待2秒
                
                retry_failed = []
                retry_done = 0
                
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in failed_servers}
                    for future in as_completed(futures):
                        if _task_manager.is_stopped(task_id):
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        svr = futures[future]
                        retry_done += 1
                        try:
                            cnt = future.result()
                            if cnt > 0:  # 重试成功且有订单，记录到有订单列表
                                servers_with_orders.append(svr)
                            _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ✅ {cnt} 条")
                        except RetryableError:
                            _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ⚠️ 仍然失败", "warning")
                            retry_failed.append(svr)
                        except Exception as e:
                            _order_log(task_id, f"[重试{attempt}] [{svr['name']}] ❌ {e}", "error")
                
                failed_servers = retry_failed
                
                progress = 50 + int(attempt * 50 / 3)
                if not _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, progress=progress, 
                                        message=f"重试 {attempt}/3: 还剩 {len(failed_servers)} 台失败")
            
            # 输出最终失败列表
            if failed_servers:
                _order_log(task_id, f"\n{'='*50}", "error")
                _order_log(task_id, f"以下服务器 3 次重试均失败:", "error")
                _order_log(task_id, f"{'='*50}", "error")
                for svr in failed_servers:
                    _order_log(task_id, f"  域名: {svr.get('domain', 'N/A'):<30} IP: {svr.get('ip', 'N/A')}", "error")
                _order_log(task_id, f"{'='*50}", "error")
                _order_log(task_id, f"共 {len(failed_servers)} 台服务器最终失败", "error")
        
        if not _task_manager.is_stopped(task_id):
            success_count = total_servers - len(failed_servers)
            msg = f"第一阶段完成: 成功 {success_count}/{total_servers} 台服务器"
            if failed_servers:
                msg += f"，{len(failed_servers)} 台失败"
            _task_manager.update(task_id, status="running", message=msg, progress=50)
            _order_log(task_id, f"\n{'='*50}")
            _order_log(task_id, f"第一阶段完成: {msg}")
            _order_log(task_id, f"{'='*50}")
        
        # # 第二阶段：异步获取订单详情（已关闭，如需开启请取消注释）
        # if not _task_manager.is_stopped(task_id):
        #     _order_log(task_id, f"\n{'='*50}")
        #     _order_log(task_id, f"第二阶段: 获取订单详情（{len(servers_with_orders)} 台有订单的服务器）")
        #     _order_log(task_id, f"{'='*50}")
        #     
        #     detail_success = 0
        #     detail_failed = 0
        #     detail_dedup = 0
        #     detail_servers_done = 0
        #     
        #     for svr in servers_with_orders:  # 只处理有订单的服务器
        #         if _task_manager.is_stopped(task_id):
        #             break
        #         
        #         ip = svr.get("ip", "")
        #         domain = svr.get("domain", "")
        #         
        #         if not ip:
        #             continue
        #         
        #         # 获取没有详情的订单
        #         orders_without_details = order_db.get_orders_without_details(ip, domain, year, month)
        #         
        #         if not orders_without_details:
        #             _order_log(task_id, f"[{svr.get('name', '')}] 无需获取详情")
        #             detail_servers_done += 1
        #             continue
        #         
        #         _order_log(task_id, f"[{svr.get('name', '')}] 待处理: {len(orders_without_details)} 个订单")
        #         
        #         try:
        #             result = _fetch_order_details_for_server(
        #                 svr, orders_without_details, _log_wrapper, wp_password, task_id
        #             )
        #             detail_success += result.get("success", 0)
        #             detail_failed += result.get("failed", 0)
        #             detail_dedup += result.get("deduplicated", 0)
        #             
        #             _order_log(task_id, f"[{svr.get('name', '')}] 完成: {result.get('success', 0)} 成功, {result.get('failed', 0)} 失败, {result.get('deduplicated', 0)} 去重")
        #         except RetryableError as e:
        #             _order_log(task_id, f"[{svr.get('name', '')}] ⚠️ {e}", "warning")
        #         except Exception as e:
        #             _order_log(task_id, f"[{svr.get('name', '')}] ❌ {e}", "error")
        #         
        #         detail_servers_done += 1
        #         progress = 50 + int(detail_servers_done / len(servers_with_orders) * 50) if servers_with_orders else 50
        #         _task_manager.update(task_id, progress=progress,
        #                             message=f"第二阶段: {detail_servers_done}/{len(servers_with_orders)} 台服务器")
        #     
        #     # 输出第二阶段统计
        #     _order_log(task_id, f"\n{'='*50}")
        #     _order_log(task_id, f"第二阶段完成:")
        #     _order_log(task_id, f"  详情更新成功: {detail_success} 条")
        #     _order_log(task_id, f"  详情更新失败: {detail_failed} 条")
        #     _order_log(task_id, f"  去重删除: {detail_dedup} 条")
        #     _order_log(task_id, f"{'='*50}")
        
        if not _task_manager.is_stopped(task_id):
            _task_manager.update(task_id, status="completed", 
                                message=f"完成: 订单列表抓取", progress=100)
        
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})

    def _run_fetch_by_ip(ip, year, month, task_id, date_from="", date_to=""):
        order_db = _get_order_db()
        if not order_db:
            _order_log(task_id, "数据库连接失败", "error")
            _task_manager.update(task_id, status="failed", message="数据库连接失败")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        # 从 MongoDB 获取指定 IP 的服务器
        servers = list(order_db.servers_col.find({"ip": ip}))
        if not servers:
            _order_log(task_id, f"未找到 IP {ip} 的服务器", "error")
            _task_manager.update(task_id, status="failed", message=f"未找到 IP {ip} 的服务器")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        # 读取 WordPress 密码
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
            _task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
            return
        _order_log(task_id, f"开始并发抓取 IP {ip} ({len(servers)} 个域名, 5线程)")
        _task_manager.update(task_id, status="running", message=f"开始抓取 IP {ip} ({len(servers)} 个域名)")
        
        failed_servers = []  # 记录可重试失败的服务器
        done = 0
        total_servers = len(servers)
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _log_wrapper(msg, level="info"):
            _order_log(task_id, msg, level)
        
        # 第一轮抓取
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
            for future in as_completed(futures):
                if _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{total_servers} 个域名")
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
                
                if not _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, progress=int(done / total_servers * 50),
                                        message=f"第一轮: {done}/{total_servers} 个域名")
        
        # 重试失败的服务器（最多3次）
        if failed_servers and not _task_manager.is_stopped(task_id):
            _order_log(task_id, f"\n{'='*50}")
            _order_log(task_id, f"开始重试失败的域名 ({len(failed_servers)} 个)", "warning")
            _order_log(task_id, f"{'='*50}")
            
            for attempt in range(1, 4):
                if not failed_servers:
                    break
                if _task_manager.is_stopped(task_id):
                    break
                
                _order_log(task_id, f"\n--- 第 {attempt}/3 次重试 ({len(failed_servers)} 个) ---")
                time.sleep(2)  # 重试前等待2秒
                
                retry_failed = []
                retry_done = 0
                
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in failed_servers}
                    for future in as_completed(futures):
                        if _task_manager.is_stopped(task_id):
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
                if not _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, progress=progress,
                                        message=f"重试 {attempt}/3: 还剩 {len(failed_servers)} 个失败")
            
            # 输出最终失败列表
            if failed_servers:
                _order_log(task_id, f"\n{'='*50}", "error")
                _order_log(task_id, f"以下域名 3 次重试均失败:", "error")
                _order_log(task_id, f"{'='*50}", "error")
                for svr in failed_servers:
                    _order_log(task_id, f"  域名: {svr.get('domain', 'N/A'):<30} IP: {svr.get('ip', 'N/A')}", "error")
                _order_log(task_id, f"{'='*50}", "error")
                _order_log(task_id, f"共 {len(failed_servers)} 个域名最终失败", "error")
        
        if not _task_manager.is_stopped(task_id):
            success_count = total_servers - len(failed_servers)
            msg = f"完成: 成功 {success_count}/{total_servers} 个域名"
            if failed_servers:
                msg += f"，{len(failed_servers)} 个失败"
            _task_manager.update(task_id, status="completed", message=msg, progress=100)
        
        q = _order_log_queues.get(task_id)
        if q: q.put({"done": True})

    @app.route("/orders")
    def orders_page():
        """订单分析主页"""
        order_db = _get_order_db()
        ips = order_db.get_all_ips() if order_db else []
        return render_template("orders.html", ips=ips)

    @app.route("/log-stream/<task_id>")
    def order_log_stream(task_id):
        from queue import Empty
        def generate():
            q = _order_log_queues.get(task_id)
            if q is None:
                yield f"data: {json.dumps({'msg': 'Task not found', 'level': 'error', 'done': True})}\n\n"
                return
            yield f"data: {json.dumps({'msg': '日志连接已建立', 'level': 'info'})}\n\n"
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield f"data: {json.dumps(entry)}\n\n"
                    if entry.get("done"):
                        break
                except Empty:
                    yield ": keepalive\n\n"
        return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/ips", methods=["GET"])
    def api_get_ips():
        order_db = _get_order_db()
        if not order_db:
            return jsonify([])
        return jsonify(order_db.get_all_ips())

    @app.route("/api/servers", methods=["GET"])
    def api_get_servers():
        order_db = _get_order_db()
        if not order_db:
            return jsonify([])
        page = request.args.get("page", type=int)
        limit = request.args.get("limit", 100, type=int)
        return jsonify(order_db.get_servers(page, limit))

    @app.route("/api/servers", methods=["POST"])
    def api_add_server():
        data = request.json or {}
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        try:
            server_id = order_db.add_server(data.get("domain", ""), data.get("ip", ""), data.get("main_category", ""))
            return jsonify({"ok": True, "id": server_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/servers/<int:server_id>", methods=["PUT"])
    def api_update_server(server_id):
        data = request.json or {}
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        order_db.update_server(server_id, data)
        return jsonify({"ok": True})

    @app.route("/api/servers/<int:server_id>", methods=["DELETE"])
    def api_delete_server(server_id):
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        order_db.delete_server(server_id)
        return jsonify({"ok": True})

    @app.route("/api/servers/delete-all", methods=["DELETE"])
    def api_delete_all_servers():
        """删除所有服务器"""
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        try:
            deleted = order_db.delete_all_servers()
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as e:
            log.error(f"删除所有服务器失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/orders/delete-all", methods=["DELETE"])
    def api_delete_all_orders():
        """清空所有订单数据"""
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        try:
            deleted = order_db.delete_all_orders()
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as e:
            log.error(f"清空所有订单失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/servers/sync", methods=["POST"])
    def api_sync_servers():
        """从上报平台同步服务器数据"""
        order_db = _get_order_db()
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
            # 获取类目列表
            categories = reporter.fetch_categories()
            log.info(f"获取到 {len(categories)} 个类目映射")
            # 获取域名列表
            domains = reporter.fetch_all_domains()
            if not domains:
                return jsonify({"error": "上报平台未返回域名数据"}), 400
            # 同步到数据库，传入类目映射
            result = order_db.sync_from_reporter(domains, categories)
            return jsonify({"ok": True, **result})
        except Exception as e:
            log.error(f"同步服务器数据失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/fetch-all", methods=["POST"])
    def api_fetch_all():
        data = request.json or {}
        year = int(data.get("year", datetime.now().year))
        month = int(data.get("month", datetime.now().month))
        date_from = data.get("date_from", "")
        date_to = data.get("date_to", "")
        task_id = f"fetch_{int(time.time())}"
        _order_log_queues[task_id] = Queue()
        _task_manager.create(task_id, "fetch_all_orders", f"{year}-{month:02d}")
        t = threading.Thread(target=_run_fetch_all, args=(year, month, task_id, date_from, date_to), daemon=True)
        t.start()
        return jsonify({"task_id": task_id})

    @app.route("/api/fetch-one", methods=["POST"])
    def api_fetch_one():
        data = request.json or {}
        ip = data.get("ip", "")
        server_id = data.get("server_id")
        year = int(data.get("year", datetime.now().year))
        month = int(data.get("month", datetime.now().month))
        date_from = data.get("date_from", "")
        date_to = data.get("date_to", "")
        if not ip and server_id:
            order_db = _get_order_db()
            if order_db:
                server = order_db.servers_col.find_one({"id": server_id}, {"_id": 0, "ip": 1})
                if server:
                    ip = server.get("ip", "")
        if not ip:
            return jsonify({"error": "缺少 ip"}), 400
        task_id = f"fetch_{int(time.time())}"
        _order_log_queues[task_id] = Queue()
        _task_manager.create(task_id, "fetch_orders_by_ip", ip)
        t = threading.Thread(target=_run_fetch_by_ip, args=(ip, year, month, task_id, date_from, date_to), daemon=True)
        t.start()
        return jsonify({"task_id": task_id})

    @app.route("/api/orders", methods=["GET"])
    def api_get_orders():
        order_db = _get_order_db()
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

    @app.route("/api/order-stats", methods=["GET"])
    def api_order_stats():
        order_db = _get_order_db()
        if not order_db:
            return jsonify([])
        ip = request.args.get("ip", "")
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        return jsonify(order_db.get_order_stats(ip=ip, year=year, month=month, date_from=date_from, date_to=date_to))

    @app.route("/api/order-status-stats", methods=["GET"])
    def api_order_status_stats():
        order_db = _get_order_db()
        if not order_db:
            return jsonify([])
        ip = request.args.get("ip", "")
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        return jsonify(order_db.get_order_status_stats(ip=ip, year=year, month=month, date_from=date_from, date_to=date_to))

    # === 订单详情和去重路由 ===

    @app.route("/api/fetch-details", methods=["POST"])
    def api_fetch_details():
        """异步获取订单详情"""
        data = request.json or {}
        year = int(data.get("year", datetime.now().year))
        month = int(data.get("month", datetime.now().month))
        ip = data.get("ip", "")
        
        task_id = f"details_{int(time.time())}"
        _order_log_queues[task_id] = Queue()
        _task_manager.create(task_id, "fetch_order_details", f"{year}-{month:02d}")
        
        def run_task():
            order_db = _get_order_db()
            if not order_db:
                _order_log(task_id, "数据库连接失败", "error")
                _task_manager.update(task_id, status="failed", message="数据库连接失败")
                q = _order_log_queues.get(task_id)
                if q: q.put({"done": True})
                return
            
            # 获取服务器列表
            if ip:
                servers = list(order_db.servers_col.find({"ip": ip}))
            else:
                servers = order_db.get_servers()
            
            if not servers:
                _order_log(task_id, "没有配置服务器", "warn")
                _task_manager.update(task_id, status="completed", message="没有配置服务器")
                q = _order_log_queues.get(task_id)
                if q: q.put({"done": True})
                return
            
            # 读取 WordPress 密码
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
                _task_manager.update(task_id, status="failed", message="未配置 WordPress 密码")
                q = _order_log_queues.get(task_id)
                if q: q.put({"done": True})
                return
            
            _order_log(task_id, f"开始获取订单详情: {len(servers)} 台服务器")
            _task_manager.update(task_id, status="running", message=f"处理中: {len(servers)} 台服务器")
            
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
                    if _task_manager.is_stopped(task_id):
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
                    
                    _task_manager.update(task_id, progress=int(done / len(futures) * 100),
                                        message=f"处理中: {done}/{len(futures)} 台服务器")
            
            if not _task_manager.is_stopped(task_id):
                msg = f"完成: 成功 {total_success}, 失败 {total_failed}, 去重 {total_dedup}"
                _task_manager.update(task_id, status="completed", message=msg, progress=100)
                _order_log(task_id, f"\n{'='*50}")
                _order_log(task_id, msg)
                _order_log(task_id, f"{'='*50}")
            
            q = _order_log_queues.get(task_id)
            if q: q.put({"done": True})
        
        threading.Thread(target=run_task, daemon=True).start()
        return jsonify({"task_id": task_id})

    @app.route("/api/deduplicate", methods=["POST"])
    def api_deduplicate():
        """执行订单去重任务"""
        data = request.json or {}
        ip = data.get("ip", "")
        domain = data.get("domain", "")
        year = data.get("year", type=int)
        month = data.get("month", type=int)
        
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        
        try:
            # 先获取统计信息
            stats = order_db.get_dedup_stats(ip=ip, domain=domain, year=year, month=month)
            
            # 执行去重
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

    @app.route("/api/dedup-stats", methods=["GET"])
    def api_dedup_stats():
        """获取去重统计信息（不执行删除）"""
        ip = request.args.get("ip", "")
        domain = request.args.get("domain", "")
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        
        order_db = _get_order_db()
        if not order_db:
            return jsonify({"error": "数据库连接失败"}), 500
        
        try:
            stats = order_db.get_dedup_stats(ip=ip, domain=domain, year=year, month=month)
            return jsonify(stats)
        except Exception as e:
            log.error(f"获取去重统计失败: {e}")
            return jsonify({"error": str(e)}), 500

    # === 订单定时任务路由 ===

    @app.route("/api/scheduler/status", methods=["GET"])
    def api_scheduler_status():
        """获取订单定时任务状态"""
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        return jsonify(scheduler.get_status())

    @app.route("/api/scheduler/start", methods=["POST"])
    def api_scheduler_start():
        """启动订单定时任务"""
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        scheduler.start()
        return jsonify({"ok": True, "message": "定时任务已启动"})

    @app.route("/api/scheduler/stop", methods=["POST"])
    def api_scheduler_stop():
        """停止订单定时任务"""
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        scheduler.stop()
        return jsonify({"ok": True, "message": "定时任务已停止"})

    @app.route("/api/scheduler/run-now", methods=["POST"])
    def api_scheduler_run_now():
        """立即执行一次订单更新"""
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        task_id = scheduler.run_now()
        return jsonify({"ok": True, "task_id": task_id, "message": "任务已启动"})

    @app.route("/api/scheduler/set-time", methods=["POST"])
    def api_scheduler_set_time():
        """设置定时任务执行时间"""
        data = request.json or {}
        hour = int(data.get("hour", 8))
        minute = int(data.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return jsonify({"error": "无效的时间格式"}), 400
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        scheduler.set_schedule(hour, minute)
        return jsonify({"ok": True, "message": f"定时任务已设置为每天 {hour:02d}:{minute:02d}"})

    @app.route("/api/scheduler/log/<task_id>", methods=["GET"])
    def api_scheduler_log(task_id):
        """获取定时任务日志流"""
        from qmds.modules.order_checker.scheduler import get_order_scheduler
        scheduler = get_order_scheduler()
        
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
        return Response(generate(), mimetype="text/event-stream")

    # === 网站收录分析路由 ===

    @app.route("/seo-analysis", methods=["GET"])
    def seo_analysis():
        """网站收录分析主页"""
        try:
            # 从订单数据库获取域名列表（已缓存到本地）
            from qmds.db.order_db import OrderDBClient
            order_db = OrderDBClient()
            servers = order_db.get_servers()
            domains = sorted(set(s.get("domain", "") for s in servers if s.get("domain")))
            order_db.close()
            
            return render_template("seo_analysis.html", domains=domains, has_config=True)
        except Exception as e:
            log.error(f"网站收录分析页面错误: {e}")
            flash(f"获取域名列表失败: {e}", "error")
            return render_template("seo_analysis.html", domains=[], has_config=True)

    @app.route("/seo-analysis/query", methods=["POST"])
    def seo_analysis_query():
        """启动收录查询任务"""
        domains_text = request.form.get("domains", "").strip()
        interval = float(request.form.get("interval", 1.0))
        
        if not domains_text:
            flash("请输入要查询的域名", "error")
            return redirect(url_for("seo_analysis"))
        
        # 解析域名列表
        domains = [d.strip() for d in domains_text.split("\n") if d.strip()]
        if not domains:
            flash("未找到有效的域名", "error")
            return redirect(url_for("seo_analysis"))
        
        task_id = f"seo_{int(time.time())}"
        _task_manager.create(task_id, "seo_analysis", f"{len(domains)} 个域名")
        
        def run_task():
            from qmds.utils.seo_checker import SEOChecker
            checker = SEOChecker()
            try:
                total = len(domains)
                success_count = 0
                failed_count = 0

                _task_manager.add_log(task_id, f"任务启动: 网站收录分析", "info")
                _task_manager.add_log(task_id, f"待查询域名: {total}", "info")
                _task_manager.add_log(task_id, f"查询间隔: {interval}秒", "info")

                _task_manager.update(task_id, status="running", message=f"开始查询 {total} 个域名的收录情况")

                for idx, domain in enumerate(domains, 1):
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped",
                            message=f"任务已停止: 已查询 {idx-1}/{total} 个域名，成功 {success_count} 个，失败 {failed_count} 个")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return

                    try:
                        _task_manager.update(task_id,
                            progress=int(idx / total * 100),
                            message=f"正在查询第 {idx}/{total} 个域名: {domain}")

                        result = checker.check_google_index(domain)
                        if result["success"]:
                            success_count += 1
                            _task_manager.add_log(task_id, f"[{idx}/{total}] ✓ {domain} - 收录 {result['count']} 条", "info")
                        else:
                            failed_count += 1
                            _task_manager.add_log(task_id, f"[{idx}/{total}] ✗ {domain} - {result.get('error', '未知错误')}", "warning")

                        time.sleep(interval)
                    except Exception as e:
                        failed_count += 1
                        _task_manager.add_log(task_id, f"[{idx}/{total}] ✗ {domain} - {e}", "error")

                summary = f"查询完成: 共 {total} 个域名，成功 {success_count} 个，失败 {failed_count} 个"
                _task_manager.update(task_id,
                    status="completed",
                    message=summary,
                    result={"total": total, "success": success_count, "failed": failed_count},
                    progress=100)
                _task_manager.add_log(task_id, summary, "info")
            except Exception as e:
                log.error(f"收录查询任务失败: {e}")
                _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                _task_manager.add_log(task_id, f"任务失败: {e}", "error")
            finally:
                checker.close()
        
        threading.Thread(target=run_task, daemon=True).start()
        flash(f"收录查询任务已启动: {len(domains)} 个域名，可在任务页面查看进度", "info")
        return redirect(url_for("seo_analysis"))

    @app.route("/api/seo/domains", methods=["GET"])
    def api_seo_domains():
        """API: 获取域名列表"""
        site_db = SiteDBClient()
        try:
            settings = site_db.get_all_settings()
            username = settings.get("report_username", "")
            password = settings.get("report_password", "")
            
            if not username or not password:
                return jsonify({"error": "请先在配置页面设置上报账号和密码"}), 400
            
            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
            all_domains = reporter.fetch_all_domains()
            domains = [d.get("name", "") for d in all_domains if d.get("name")]
            
            return jsonify({"ok": True, "domains": domains, "total": len(domains)})
        except Exception as e:
            log.error(f"获取域名列表失败: {e}")
            return jsonify({"error": str(e)}), 500
        finally:
            site_db.close()

    # === 工具路由 ===

    @app.route("/tools", methods=["GET"])
    def tools():
        """工具箱主页"""
        return render_template("tools.html")

    @app.route("/tools/id-distribute", methods=["GET"])
    def id_distribute():
        """商品ID分配工具"""
        return render_template("id_distribute.html")

    @app.route("/tools/data-clean", methods=["GET", "POST"])
    def data_clean():
        """数据二次清洗：对文件夹中的表格数据进行清洗"""
        if request.method == "GET":
            return render_template("data_clean.html")
        
        try:
            folder_path = request.form.get("folder_path", "").strip()
            output_folder = request.form.get("output_folder", "").strip() or None
            price_threshold = float(request.form.get("price_threshold", 2500))
            
            if not folder_path:
                return render_template("data_clean.html", error="请输入文件夹路径")

            task_id = f"data_clean_{int(time.time())}"
            _task_manager.create(task_id, "data_clean", f"数据二次清洗: {os.path.basename(folder_path)}")

            def run_task():
                from qmds.utils.data_cleaner import clean_folder
                try:
                    _task_manager.add_log(task_id, f"任务启动: 数据二次清洗", "info")
                    _task_manager.add_log(task_id, f"输入目录: {folder_path}", "info")
                    _task_manager.add_log(task_id, f"价格阈值: {price_threshold}", "info")

                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.update(task_id, message="正在扫描文件...")
                    _task_manager.add_log(task_id, "正在扫描文件夹...", "info")

                    result = clean_folder(
                        input_folder=folder_path,
                        output_folder=output_folder,
                        price_threshold=price_threshold
                    )

                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        _task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return

                    # 计算输出目录
                    if output_folder:
                        result["output_folder"] = output_folder
                    else:
                        from pathlib import Path
                        result["output_folder"] = str(Path(folder_path) / "cleaned")

                    _task_manager.add_log(task_id, f"扫描文件数: {result['total_files']}", "info")
                    _task_manager.add_log(task_id, f"成功处理: {result['processed']}", "info")
                    _task_manager.add_log(task_id, f"处理失败: {result['failed']}", "info")
                    _task_manager.add_log(task_id, f"输出目录: {result['output_folder']}", "info")

                    # 记录每个文件的处理详情
                    for detail in result.get("details", []):
                        if detail.get("status") == "success":
                            orig = detail.get("original_count", 0)
                            cleaned = detail.get("cleaned_count", 0)
                            removed = orig - cleaned if orig and cleaned else 0
                            _task_manager.add_log(task_id, f"✓ {detail['file']}: {orig} → {cleaned} (删除 {removed})", "info")
                        else:
                            _task_manager.add_log(task_id, f"✗ {detail['file']}: {detail.get('reason', '未知错误')}", "error")

                    summary = f"完成: 处理 {result['processed']}/{result['total_files']} 个文件，失败 {result['failed']}"
                    _task_manager.update(task_id, status="completed", message=summary, progress=100)
                    _task_manager.add_log(task_id, summary, "info")

                except FileNotFoundError as e:
                    _task_manager.update(task_id, status="failed", message=str(e))
                    _task_manager.add_log(task_id, str(e), "error")
                except Exception as e:
                    log.error(f"数据二次清洗失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"处理失败: {e}")
                    _task_manager.add_log(task_id, f"处理失败: {e}", "error")

            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据二次清洗任务已启动，可在任务页面查看进度", "success")
            return redirect(url_for("tasks"))
            
        except Exception as e:
            log.error(f"数据二次清洗失败: {e}")
            return render_template("data_clean.html", error=f"处理失败: {str(e)}")

    @app.route("/tools/category-merge", methods=["GET", "POST"])
    def category_merge():
        """分类数据处理：将数量过少的分类合并为公共类"""
        if request.method == "GET":
            return render_template("category_merge.html")
        
        try:
            import re
            
            file_path = request.form.get("file_path", "").strip()
            threshold = int(request.form.get("threshold", 10))
            category_field = request.form.get("category_field", "分类").strip()
            common_category_input = request.form.get("common_category", "Other").strip()
            common_categories = [cat.strip() for cat in re.split(r'[,;\n]+', common_category_input) if cat.strip()]
            if not common_categories:
                common_categories = ["Other"]
            
            if not file_path:
                return render_template("category_merge.html", error="请输入表格文件路径")
            
            if not file_path.endswith(('.xlsx', '.xls', '.csv')):
                return render_template("category_merge.html", error="不支持的文件格式，请使用 .xlsx 或 .csv 文件")

            task_id = f"category_merge_{int(time.time())}"
            _task_manager.create(task_id, "category_merge", f"分类数据处理: {os.path.basename(file_path)}")

            def run_task():
                import pandas as pd
                from collections import Counter
                import random as rng
                
                try:
                    _task_manager.add_log(task_id, f"任务启动: 分类数据处理", "info")
                    _task_manager.add_log(task_id, f"文件: {file_path}", "info")
                    _task_manager.add_log(task_id, f"分类字段: {category_field}, 阈值: {threshold}", "info")
                    _task_manager.add_log(task_id, f"公共类: {', '.join(common_categories)}", "info")

                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.update(task_id, message="正在读取文件...")
                    _task_manager.add_log(task_id, "正在读取表格文件...", "info")

                    if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                        df = pd.read_excel(file_path)
                    else:
                        df = pd.read_csv(file_path)

                    if category_field not in df.columns:
                        _task_manager.update(task_id, status="failed", message=f"未找到 '{category_field}' 列")
                        _task_manager.add_log(task_id, f"表格中未找到 '{category_field}' 列，可用列: {', '.join(df.columns.tolist())}", "error")
                        return

                    total_rows = len(df)
                    _task_manager.add_log(task_id, f"读取完成，共 {total_rows} 行数据", "info")

                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.update(task_id, message="正在统计分类...")
                    _task_manager.add_log(task_id, "正在统计各分类数量...", "info")

                    category_counts = Counter(df[category_field].fillna('').astype(str))

                    merged_categories = []
                    for cat, count in category_counts.items():
                        if cat and count < threshold:
                            merged_categories.append((cat, count))
                    merged_categories.sort(key=lambda x: x[1])

                    before_count = len([c for c in category_counts.keys() if c])
                    _task_manager.add_log(task_id, f"修改前分类数: {before_count}, 待合并分类数: {len(merged_categories)}", "info")

                    if not merged_categories:
                        _task_manager.update(task_id, status="completed", message="无需合并，所有分类数量均大于阈值", progress=100)
                        _task_manager.add_log(task_id, "无需合并，所有分类数量均大于阈值", "info")
                        return

                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.update(task_id, message=f"正在合并 {len(merged_categories)} 个分类...")
                    _task_manager.add_log(task_id, "开始执行合并操作...", "info")

                    modified_rows = 0
                    for i, (cat, count) in enumerate(merged_categories):
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message=f"任务已停止: 已处理 {i}/{len(merged_categories)}")
                            _task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return

                        mask = df[category_field].fillna('').astype(str) == cat
                        row_count = mask.sum()
                        modified_rows += row_count
                        selected_category = rng.choice(common_categories)
                        df.loc[mask, category_field] = selected_category

                        if (i + 1) % 50 == 0 or i + 1 == len(merged_categories):
                            progress = int((i + 1) / len(merged_categories) * 100)
                            _task_manager.update(task_id, progress=progress, current=i + 1, total=len(merged_categories),
                                                message=f"合并中: {i + 1}/{len(merged_categories)}")

                    new_category_counts = Counter(df[category_field].fillna('').astype(str))
                    after_count = len([c for c in new_category_counts.keys() if c])

                    remaining_categories = [(cat, count) for cat, count in new_category_counts.items()
                                           if cat and cat not in common_categories]
                    remaining_categories.sort(key=lambda x: x[1], reverse=True)

                    _task_manager.add_log(task_id, f"修改前分类数: {before_count} → 修改后: {after_count}", "info")
                    _task_manager.add_log(task_id, f"合并分类数: {len(merged_categories)}, 修改行数: {modified_rows}", "info")

                    # 记录被合并的分类详情
                    for cat, count in merged_categories[:20]:
                        _task_manager.add_log(task_id, f"  合并: {cat} ({count} 条)", "info")
                    if len(merged_categories) > 20:
                        _task_manager.add_log(task_id, f"  ... 还有 {len(merged_categories) - 20} 个分类", "info")

                    summary = f"分类合并: {len(merged_categories)} 个分类, 修改 {modified_rows} 行, {before_count} → {after_count}"
                    _task_manager.add_log(task_id, summary, "info")

                    # ── 清理无效分类名称 ──
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.add_log(task_id, "─── 清理无效分类名称 ───", "info")
                    _task_manager.update(task_id, message="正在清理无效分类名称...")

                    import re as _re
                    
                    def _is_invalid_category(cat_name: str) -> bool:
                        """判断分类名称是否无效：simple、包含Undefined、纯数字符号"""
                        if not cat_name:
                            return False
                        cat_lower = cat_name.strip().lower()
                        # 分类为 simple
                        if cat_lower == "simple":
                            return True
                        # 包含 undefined
                        if "undefined" in cat_lower:
                            return True
                        # 纯数字符号（去掉空格和常见符号后只剩数字）
                        cleaned = _re.sub(r'[\s\-_.,/\\|:;]+', '', cat_name.strip())
                        if cleaned.isdigit():
                            return True
                        return False
                    
                    # 统计无效分类
                    invalid_categories = []
                    current_counts = Counter(df[category_field].fillna('').astype(str))
                    for cat, count in current_counts.items():
                        if cat and _is_invalid_category(cat):
                            invalid_categories.append((cat, count))
                    
                    if invalid_categories:
                        _task_manager.add_log(task_id, f"发现 {len(invalid_categories)} 个无效分类名称", "info")
                        
                        invalid_modified_rows = 0
                        for i, (cat, count) in enumerate(invalid_categories):
                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", message="任务已停止")
                                return
                            
                            mask = df[category_field].fillna('').astype(str) == cat
                            row_count = mask.sum()
                            invalid_modified_rows += row_count
                            selected_category = rng.choice(common_categories)
                            df.loc[mask, category_field] = selected_category
                            _task_manager.add_log(task_id, f"  替换: {cat} ({count} 条) → {selected_category}", "info")
                        
                        _task_manager.add_log(task_id, f"无效分类清理完成: 替换 {len(invalid_categories)} 个分类, 修改 {invalid_modified_rows} 行", "info")
                        summary += f"；无效分类替换 {len(invalid_categories)} 个, 修改 {invalid_modified_rows} 行"
                    else:
                        _task_manager.add_log(task_id, "未发现无效分类名称", "info")

                    # ── 自动执行数据二次清洗（原地） ──
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    _task_manager.add_log(task_id, "─── 开始数据二次清洗 ───", "info")
                    _task_manager.update(task_id, message="正在执行数据二次清洗...")

                    from qmds.utils.data_cleaner import clean_dataframe
                    before_clean = len(df)
                    df = clean_dataframe(df, price_threshold=2500.0)
                    after_clean = len(df)
                    removed_clean = before_clean - after_clean

                    _task_manager.add_log(task_id, f"清洗前行数: {before_clean}", "info")
                    _task_manager.add_log(task_id, f"清洗后行数: {after_clean} (删除 {removed_clean})", "info")

                    # 保存最终结果
                    if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                        df.to_excel(file_path, index=False)
                    else:
                        df.to_csv(file_path, index=False)

                    _task_manager.add_log(task_id, f"最终文件已保存: {file_path}", "info")

                    # 最终汇总
                    final_summary = f"{summary}；二次清洗删除 {removed_clean} 行，最终 {after_clean} 行"
                    _task_manager.update(task_id, status="completed", message=final_summary, progress=100)

                except Exception as e:
                    log.error(f"分类数据处理失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"处理失败: {e}")
                    _task_manager.add_log(task_id, f"处理失败: {e}", "error")

            threading.Thread(target=run_task, daemon=True).start()
            flash(f"分类数据处理任务已启动，可在任务页面查看进度", "success")
            return redirect(url_for("tasks"))
            
        except Exception as e:
            log.error(f"分类数据处理失败: {e}")
            return render_template("category_merge.html", error=f"处理失败: {str(e)}")

    # ── HTML网站分类器 ──────────────────────────────────────

    @app.route("/tools/html-classifier", methods=["GET", "POST"])
    def html_classifier():
        """基于HTML内容的Shopify网站分类器"""
        if request.method == "GET":
            task_id = request.args.get("task_id")
            task_result = None
            if task_id:
                task_result = _task_manager.get(task_id)
            return render_template("html_classifier.html", task_result=task_result)

        action = request.form.get("action", "single")

        if action == "single":
            url = request.form.get("url", "").strip()
            if not url:
                return render_template("html_classifier.html", error="请输入网站 URL")

            try:
                from qmds.utils.html_site_classifier import HTMLSiteClassifier
                classifier = HTMLSiteClassifier(use_proxy=True)
                result = classifier.classify(url)
                return render_template("html_classifier.html", single_result=result)
            except Exception as e:
                log.error(f"HTML网站分类失败: {e}")
                return render_template("html_classifier.html", error=f"分类失败: {str(e)}")

        elif action == "batch":
            file_path = request.form.get("file_path", "").strip()
            if not file_path:
                return render_template("html_classifier.html", error="请输入 Excel 文件路径")

            # 清理路径中的不可见字符
            import re
            file_path = re.sub(r'[\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069]', '', file_path)
            file_path = file_path.strip().strip('\u200b').strip('\ufeff')

            if not Path(file_path).exists():
                return render_template("html_classifier.html", error=f"文件不存在: {file_path}")

            if not file_path.endswith(('.xlsx', '.xls')):
                return render_template("html_classifier.html", error="请使用 .xlsx 文件")

            # 创建后台任务
            task_id = f"html_classifier_{int(time.time())}"
            _task_manager.create(task_id, "html_classifier", f"HTML批量分类: {Path(file_path).name}")

            def run_task():
                try:
                    from qmds.utils.html_site_classifier import HTMLSiteClassifier
                    classifier = HTMLSiteClassifier(use_proxy=True)

                    _task_manager.add_log(task_id, f"开始处理: {file_path}", "info")
                    stats = classifier.classify_from_excel(file_path)

                    final_msg = f"完成! 总计 {stats['total']}: 成功 {stats['success']}, 失败 {stats['error']}, Shopify {stats['shopify']}"
                    _task_manager.update(task_id, status="completed", message=final_msg, result=stats, progress=100)
                    _task_manager.add_log(task_id, final_msg, "success")

                except Exception as e:
                    log.error(f"HTML批量分类失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    _task_manager.add_log(task_id, f"失败: {e}", "error")

            threading.Thread(target=run_task, daemon=True).start()
            flash(f"HTML批量分类任务已启动，可在任务页面查看进度", "success")
            return redirect(url_for("html_classifier", task_id=task_id))

    # ── 网站分类器 ──────────────────────────────────────

    @app.route("/tools/site-classifier", methods=["GET", "POST"])
    def site_classifier():
        """Shopify 网站分类器：判断专一站/综合站"""
        if request.method == "GET":
            task_id = request.args.get("task_id")
            task_result = None
            if task_id:
                task_result = _task_manager.get(task_id)
            return render_template("site_classifier.html", task_result=task_result)

        action = request.form.get("action", "single")

        if action == "single":
            url = request.form.get("url", "").strip()
            if not url:
                return render_template("site_classifier.html", error="请输入网站 URL")

            try:
                from qmds.utils.site_classifier import SiteClassifier
                classifier = SiteClassifier(http_client=http)
                result = classifier.classify(url)
                return render_template("site_classifier.html", single_result=result)
            except Exception as e:
                log.error(f"网站分类失败: {e}")
                return render_template("site_classifier.html", error=f"分类失败: {str(e)}")

        elif action == "batch":
            file_path = request.form.get("file_path", "").strip()
            if not file_path:
                return render_template("site_classifier.html", error="请输入 Excel 文件路径")

            # 清理路径中的不可见字符（如 Unicode LTR/RTL 标记）
            import re
            file_path = re.sub(r'[\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069]', '', file_path)
            file_path = file_path.strip().strip('\u200b').strip('\ufeff')

            if not Path(file_path).exists():
                return render_template("site_classifier.html", error=f"文件不存在: {file_path}")

            if not file_path.endswith(('.xlsx', '.xls')):
                return render_template("site_classifier.html", error="请使用 .xlsx 文件")

            # 创建后台任务
            task_id = f"classifier_{int(time.time())}"
            _task_manager.create(task_id, "site_classifier", f"批量分类: {Path(file_path).name}")

            def run_task():
                try:
                    from qmds.utils.site_classifier import SiteClassifier
                    classifier = SiteClassifier(http_client=http)

                    _task_manager.add_log(task_id, f"开始处理: {file_path}", "info")
                    stats = classifier.classify_from_excel(file_path)

                    final_msg = f"完成! 总计 {stats['total']}: 专一 {stats['niche']}, 综合 {stats['general']}, 未知 {stats['unknown']}, 非英文 {stats['non_english']}"
                    _task_manager.update(task_id, status="completed", message=final_msg, result=stats, progress=100)
                    _task_manager.add_log(task_id, final_msg, "success")

                except Exception as e:
                    log.error(f"批量分类失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    _task_manager.add_log(task_id, f"失败: {e}", "error")

            threading.Thread(target=run_task, daemon=True).start()
            flash(f"批量分类任务已启动，可在任务页面查看进度", "success")
            return redirect(url_for("site_classifier", task_id=task_id))

    return app


class WebModule:
    def __init__(self, http_client: Optional[HttpClient] = None, host: str = "127.0.0.1", port: int = 5001, debug: bool = False):
        self.http = http_client or HttpClient()
        self.host = host
        self.port = port
        self.debug = debug
        self.app = create_app(http_client=self.http)
        self._server: Optional[threading.Thread] = None

    def run(self):
        log.info(f"Starting web console on http://{self.host}:{self.port}")
        from waitress import serve
        serve(self.app, host=self.host, port=self.port)

    def run_dev(self):
        log.info(f"Starting dev web console on http://127.0.0.1:{self.port}")
        self.app.run(host="127.0.0.1", port=self.port, debug=self.debug)

    def start_background(self):
        self._server = threading.Thread(target=self.run, daemon=True)
        self._server.start()
        return self._server
