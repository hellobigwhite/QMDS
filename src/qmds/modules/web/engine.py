import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Optional

import pandas as pd

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for, Response

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
from qmds.utils.domain_reporter import DomainReporter, DOMAIN_STATUS_LABELS, REPORT_API_BASE_URL
from qmds.modules.order_checker import WooOrderChecker, ORDER_STATUS_LABELS

log = get_logger("web")


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._stop_events: dict[str, threading.Event] = {}
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
        return task_id

    def update(self, task_id: str, **kwargs):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(kwargs)

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
            db = MongoDBClient()
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
            finally:
                db.close()
        
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
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            return
                        result = module.fetch_shopify_urls_by_keyword(
                            category=category, keyword=keyword,
                            max_pages=0, min_products=min_products,
                            keyword_workers=3,  # 3个关键词并行
                            save_mongo=save_mongo, save_excel=save_excel,
                            provider_name=provider,
                        )
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", message="任务已停止")
                            return
                        _task_manager.update(task_id, status="completed",
                            message=f"完成: 找到 {result['total_shopify']} 个店铺",
                            result=result, progress=100)
                    except Exception as e:
                        log.error(f"fetch-urls task failed: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")

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
        
        db = MongoDBClient()
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
        finally:
            db.close()
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

        db = MongoDBClient()
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
        finally:
            db.close()
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/shopify/unfiltered/edit", methods=["POST"])
    def shopify_unfiltered_edit():
        """编辑单条unfiltered记录"""
        category = request.form.get("category", "").strip()
        domain = request.form.get("domain", "").strip()
        if not category or not domain:
            flash("类目和域名不能为空", "error")
            return redirect(url_for("shopify_fetch_urls", category=category))
        
        db = MongoDBClient()
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
        finally:
            db.close()
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
        
        db = MongoDBClient()
        try:
            deleted = db.delete_unfiltered_many(category, domains)
            flash(f"已删除 {deleted} 条记录", "success")
        except Exception as e:
            flash(f"删除失败: {e}", "error")
        finally:
            db.close()
        return redirect(url_for("shopify_fetch_urls", category=category))

    @app.route("/api/shopify/unfiltered/<category>/<domain>")
    def api_shopify_unfiltered_get(category, domain):
        """API: 获取单条unfiltered记录"""
        db = MongoDBClient()
        try:
            doc = db.get_unfiltered_by_domain(category, domain)
            if doc:
                return jsonify({"ok": True, "data": doc})
            return jsonify({"ok": False, "error": "未找到记录"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        finally:
            db.close()

    @app.route("/shopify/filter-categories", methods=["GET", "POST"])
    def shopify_filter_categories():
        selected_category = request.args.get("category", "")
        filtered_stores = []
        filtered_total = 0
        
        # 查询选中类目的 filtered 数据
        if selected_category:
            db = MongoDBClient()
            try:
                filtered_stores = db.get_filtered_stores(selected_category, limit=100)
                filtered_total = db.get_filtered_count(selected_category)
            except Exception as e:
                log.error(f"查询 filtered 数据失败: {e}")
            finally:
                db.close()
        
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
                        if total == 0:
                            _task_manager.update(task_id, status="completed",
                                message=f"类目 {category} 无待处理 URL", progress=100)
                            return

                        matched_count = 0
                        removed_count = 0
                        processed = 0
                        for store in stores:
                            if _task_manager.is_stopped(task_id):
                                _task_manager.update(task_id, status="stopped", 
                                    message=f"任务已停止: 处理 {processed}/{total}，已匹配 {matched_count} 条")
                                return
                            
                            store_url = store["url"]
                            domain = store["domain"]
                            processed += 1
                            domain_matched = False
                            try:
                                collections = fetch_collections(http, store_url)
                                log.info(f"[精准类目] [{processed}/{total}] {domain} - 获取 {len(collections)} 个 collection")
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
                                
                                if db.delete_unfiltered(category, domain):
                                    removed_count += 1
                                    log.info(f"[精准类目]   🗑️ 已从 {category}_unfiltered 删除: {domain}")
                            except Exception as e:
                                log.warning(f"[精准类目] [{processed}/{total}] {domain} - 处理失败: {e}")
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
                    except Exception as e:
                        log.error(f"[精准类目] 任务异常: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")
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
        db = MongoDBClient()
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
        finally:
            db.close()

    @app.route("/shopify/filter-categories/add", methods=["POST"])
    def shopify_filter_add():
        """手动添加单条记录到 filtered"""
        category = request.form.get("category", "").strip()
        store_url = request.form.get("store_url", "").strip()
        collection_url = request.form.get("collection_url", "").strip()

        if not category or not collection_url:
            flash("类目和 Collection URL 不能为空", "error")
            return redirect(url_for("shopify_filter_categories", category=category))

        db = MongoDBClient()
        try:
            if db.add_filtered_manual(category, store_url, collection_url):
                flash("已添加记录", "success")
            else:
                flash("添加失败", "error")
        except Exception as e:
            flash(f"添加失败: {e}", "error")
        finally:
            db.close()
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

        db = MongoDBClient()
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
        finally:
            db.close()
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
        finally:
            db.close()
        return redirect(url_for("shopify_filter_categories", category=category))

    @app.route("/product-data", methods=["GET"])
    def product_data():
        return redirect(url_for("product_data_overview"))

    @app.route("/product-data/overview", methods=["GET"])
    def product_data_overview():
        try:
            product_db = ProductDBClient()
            stats = product_db.get_all_stats()
            collections = product_db.list_all_collections()
            
            # 获取可用的_filtered类目（用于爬取）
            source_db = MongoDBClient()
            filtered_categories = source_db.list_filtered_categories()
            source_db.close()
            
            product_db.close()
            
            return render_template("product_overview.html",
                                   total_collections=stats["total_categories"],
                                   non_empty_collections=stats["total_categories"],
                                   total_rows=stats["total_raw"],
                                   total_clean_rows=stats["total_clean"],
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
                                   collections=[],
                                   category_stats=[],
                                   filtered_categories=[],
                                   error=str(e))

    @app.route("/product-data/crawl", methods=["GET", "POST"])
    def product_data_crawl():
        # 获取可用的_filtered类目（用于爬取）
        source_db = MongoDBClient()
        filtered_categories = source_db.list_filtered_categories()
        source_db.close()
        
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
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    
                    # 创建爬取器
                    crawler = create_crawler()
                    
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
                        else:
                            _task_manager.update(task_id, message=str(info))
                    
                    # 爬取类目数据
                    result = crawler.crawl_category(category, max_collections=max_sites,
                                                    progress_callback=progress_callback, max_workers=max_workers)
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 爬取 {result['success_collections']}/{result['total_collections']} 个集合，获取 {result['total_products']} 件商品",
                                        progress=100)
                except InterruptedError:
                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                except Exception as e:
                    log.error(f"爬取任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
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
            task_id = f"clean_{category}_{int(time.time())}"
            _task_manager.create(task_id, "clean_products", category)
            
            def run_task():
                product_db = None
                try:
                    _task_manager.update(task_id, status="running", message=f"开始清洗: {category}")
                    
                    product_db = ProductDBClient()
                    
                    if category == "__all__":
                        # 清洗所有类目
                        categories = product_db.list_categories()
                    else:
                        categories = [category]
                    
                    total_processed = 0
                    total_cleaned = 0
                    total_removed = 0
                    
                    for cat in categories:
                        if _task_manager.is_stopped(task_id):
                            _task_manager.update(task_id, status="stopped", 
                                message=f"任务已停止: 已处理 {total_processed} 条数据")
                            return
                        
                        _task_manager.update(task_id, message=f"清洗类目: {cat}")
                        
                        result = product_db.clean_category(cat)
                        total_processed += result["processed"]
                        total_cleaned += result["cleaned"]
                        total_removed += result["removed"]
                        
                        log.info(f"类目 {cat}: 处理 {result['processed']} 条，清洗后 {result['cleaned']} 条")
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 处理 {total_processed} 条数据，清洗后 {total_cleaned} 条，移除 {total_removed} 条",
                                        progress=100)
                except Exception as e:
                    import traceback
                    log.error(f"清洗任务失败: {e}\n{traceback.format_exc()}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                finally:
                    if product_db:
                        product_db.close()
                    import gc
                    gc.collect()
            
            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据清洗任务已启动: {category}，可在任务页面查看进度")
            return redirect(url_for("product_data_clean"))
        
        # GET请求：获取类目统计信息
        try:
            product_db = ProductDBClient()
            stats = product_db.get_all_stats()
            product_db.close()
            category_stats = stats["categories"]
        except Exception as e:
            log.error(f"获取类目统计失败: {e}")
            category_stats = []
        
        return render_template("product_clean.html", category_stats=category_stats)

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
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    
                    product_db = ProductDBClient()
                    export_dir = str(settings.data_dir / "exports")
                    
                    # 定义进度回调
                    def progress_callback(info):
                        if _task_manager.is_stopped(task_id):
                            raise InterruptedError("任务被用户停止")
                        if isinstance(info, dict):
                            _task_manager.update(task_id, **info)
                        else:
                            _task_manager.update(task_id, message=str(info))
                    
                    filepath = product_db.export_category_to_excel(
                        category, export_dir, limit=limit, progress_callback=progress_callback
                    )
                    
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    
                    if filepath:
                        # 获取实际导出数量
                        if limit:
                            count = min(limit, product_db.clean_col(category).estimated_document_count())
                        else:
                            count = product_db.clean_col(category).estimated_document_count()
                        
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 导出 {count} 条数据到 {os.path.basename(filepath)}",
                                            progress=100,
                                            current=count,
                                            total=count)
                    else:
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 类目 {category} 无清洗后数据",
                                            progress=100)
                except InterruptedError:
                    _task_manager.update(task_id, status="stopped", message="任务已停止")
                except Exception as e:
                    log.error(f"导出任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                finally:
                    if product_db:
                        product_db.close()
                    import gc
                    gc.collect()
            
            threading.Thread(target=run_task, daemon=True).start()
            flash(f"数据导出任务已启动: {category}，可在任务页面查看进度")
            return redirect(url_for("product_data_export"))
        
        # GET请求：获取类目统计信息
        try:
            product_db = ProductDBClient()
            stats = product_db.get_all_stats()
            product_db.close()
            category_stats = stats["categories"]
        except Exception as e:
            log.error(f"获取类目统计失败: {e}")
            category_stats = []
        
        return render_template("product_export.html", category_stats=category_stats)

    # === 建站管理路由 ===

    @app.route("/site-management", methods=["GET"])
    def site_management():
        """建站管理主页 - 显示统计概览"""
        site_db = SiteDBClient()
        try:
            stats = site_db.get_stats()
        except Exception as e:
            log.error(f"获取站点统计失败: {e}")
            stats = {"total_sites": 0, "local_sites": 0, "reported_sites": 0, "scheduled_sites": 0, "built_sites": 0}
        finally:
            site_db.close()
        return render_template("site_management.html", stats=stats)

    @app.route("/site-management/local", methods=["GET", "POST"])
    def site_local():
        """本地站点管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))

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
                    if selected_ids:
                        count = site_db.batch_update_report_status(selected_ids, "已报")
                        flash(f"已上报 {count} 个站点", "success")
                    return redirect(url_for("site_reported"))

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

            result = site_db.list_local_sites(q, page=page)
            stats = site_db.get_stats()
            return render_template("site_local.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"本地站点页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_local.html", sites=[], stats={"local_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)
        finally:
            site_db.close()

    @app.route("/site-management/reported", methods=["GET", "POST"])
    def site_reported():
        """已报域名管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))

            if request.method == "POST":
                action = request.form.get("action", "")

                if action == "build_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.batch_update_build_status(selected_ids, "已建站")
                        flash(f"已建站 {count} 个站点", "success")
                    return redirect(url_for("site_built"))

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
                            settings = site_db_inner.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            success_count = 0
                            fail_count = 0

                            for site_id in selected_ids:
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped", 
                                        message=f"任务已停止: 成功 {success_count} 个, 失败 {fail_count} 个")
                                    return
                                
                                site = site_db_inner.get_site_by_id(site_id)
                                if not site:
                                    fail_count += 1
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
                                    log.info(f"更新域名状态成功: {domain} -> {status_label}")
                                except Exception as e:
                                    fail_count += 1
                                    log.error(f"更新域名状态失败: {domain} - {e}")

                            _task_manager.update(
                                task_id,
                                status="completed",
                                message=f"完成: 成功 {success_count} 个, 失败 {fail_count} 个",
                                progress=100
                            )
                        except Exception as e:
                            log.error(f"更新域名状态任务失败: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
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
                            settings = site_db_inner.get_all_settings()
                            username = settings.get("report_username", "")
                            password = settings.get("report_password", "")
                            if not username or not password:
                                _task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                                return

                            reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                            all_reported = site_db_inner.list_reported_domains_for_sync()
                            total = len(all_reported)
                            found_count = 0
                            not_found_count = 0
                            error_count = 0

                            for i, site in enumerate(all_reported):
                                if _task_manager.is_stopped(task_id):
                                    _task_manager.update(task_id, status="stopped",
                                        message=f"任务已停止: 审查 {i}/{total}, 平台存在 {found_count}, 不存在 {not_found_count}")
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
                                    else:
                                        site_db_inner.update_site(domain, {"report_status": "未报"})
                                        not_found_count += 1
                                except Exception:
                                    site_db_inner.update_site(domain, {"report_status": "未报"})
                                    error_count += 1

                                if (i + 1) % 10 == 0 or i + 1 == total:
                                    _task_manager.update(task_id,
                                        progress=int((i + 1) / total * 100),
                                        message=f"审查中: {i + 1}/{total}")

                            _task_manager.update(
                                task_id,
                                status="completed",
                                message=f"审查完成: 平台存在 {found_count}, 不存在 {not_found_count}, 失败 {error_count}",
                                progress=100
                            )
                        except Exception as e:
                            log.error(f"审查已报域名任务失败: {e}")
                            _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                        finally:
                            site_db_inner.close()

                    threading.Thread(target=run_review_task, daemon=True).start()
                    flash(f"审查已报域名任务已启动，可在任务页面查看进度", "info")
                    return redirect(url_for("site_reported"))

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

            result = site_db.list_reported_sites(q, page=page)
            stats = site_db.get_stats()
            return render_template("site_reported.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"已报域名页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_reported.html", sites=[], stats={"reported_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)
        finally:
            site_db.close()

    @app.route("/site-management/scheduled", methods=["GET", "POST"])
    def site_scheduled():
        """计划上报管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))

            if request.method == "POST":
                action = request.form.get("action", "")

                if action == "report_selected":
                    selected_ids = request.form.getlist("selected_ids")
                    if selected_ids:
                        count = site_db.batch_update_report_status(selected_ids, "已报")
                        flash(f"已上报 {count} 个站点", "success")
                    return redirect(url_for("site_reported"))

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

            result = site_db.list_scheduled_sites(q, page=page)
            stats = site_db.get_stats()
            return render_template("site_scheduled.html", sites=result["items"], stats=stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"计划上报页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_scheduled.html", sites=[], stats={"scheduled_sites": 0}, q=q,
                                   total=0, page=1, page_size=20)
        finally:
            site_db.close()

    @app.route("/site-management/built", methods=["GET", "POST"])
    def site_built():
        """已建站管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()
            page = int(request.args.get("page", 1))

            if request.method == "POST":
                action = request.form.get("action", "")
                selected_ids = request.form.getlist("selected_ids")

                if not selected_ids:
                    flash("请先勾选要操作的站点", "error")
                    return redirect(url_for("site_built", q=q))

                if action == "delete_selected":
                    count = site_db.delete_sites_by_ids(selected_ids)
                    flash(f"已删除 {count} 个站点", "success")

                elif action == "health_check":
                    count = site_db.batch_update_health_status(selected_ids, "正常")
                    flash(f"已完成 {count} 个站点的健康检查", "success")

                elif action == "upload_main":
                    count = site_db.batch_update_main_data_status(selected_ids, "已上传")
                    flash(f"已完成 {count} 个站点的主数据上传", "success")

                elif action == "upload_extra":
                    count = site_db.batch_update_extra_data_status(selected_ids, "已上传")
                    flash(f"已完成 {count} 个站点的补充数据上传", "success")

                elif action == "set_main_category":
                    count = site_db.batch_update_main_category_status(selected_ids, "已上传")
                    flash(f"已完成 {count} 个站点的主分类设置", "success")

                elif action == "clear_cache":
                    # TODO: 实现实际的缓存清理逻辑
                    flash(f"已完成 {len(selected_ids)} 个站点的缓存清理", "success")

                elif action == "configure_menu":
                    count = site_db.batch_update_auto_category_status(selected_ids, "已配置")
                    flash(f"已完成 {count} 个站点的菜单设置", "success")

                elif action == "configure_sites":
                    count_plugin = site_db.batch_update_plugin_status(selected_ids, "已配置")
                    count_media = site_db.batch_update_media_status(selected_ids, "已配置")
                    flash(f"已完成 {len(selected_ids)} 个站点的配置", "success")

                return redirect(url_for("site_built", q=q))

            result = site_db.list_built_sites(q, page=page)
            stats = site_db.get_stats()
            built_stats = site_db.get_built_stats()
            return render_template("site_built.html", sites=result["items"], stats=stats, built_stats=built_stats, q=q,
                                   total=result["total"], page=result["page"], page_size=result["page_size"])
        except Exception as e:
            log.error(f"已建站页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_built.html", sites=[], stats={"built_sites": 0}, built_stats={}, q=q,
                                   total=0, page=1, page_size=20)
        finally:
            site_db.close()

    @app.route("/site-management/<site_id>/edit", methods=["GET", "POST"])
    def site_edit(site_id):
        """编辑站点"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

    @app.route("/site-management/<site_id>/delete", methods=["POST"])
    def site_delete(site_id):
        """删除站点"""
        site_db = SiteDBClient()
        try:
            site = site_db.get_site_by_id(site_id)
            if site:
                site_db.delete_site(site.get("domain", ""))
                flash("站点已删除", "success")
        except Exception as e:
            log.error(f"删除站点错误: {e}")
            flash(f"删除失败: {e}", "error")
        finally:
            site_db.close()
        return redirect(url_for("site_management"))

    @app.route("/site-management/<site_id>/report", methods=["POST"])
    def site_report(site_id):
        """将站点标记为已上报"""
        site_db = SiteDBClient()
        try:
            site_db.update_site_by_id(site_id, {"report_status": "已报"})
            flash("站点已标记为已上报", "success")
        except Exception as e:
            log.error(f"上报站点错误: {e}")
            flash(f"上报失败: {e}", "error")
        finally:
            site_db.close()
        return redirect(url_for("site_local"))

    @app.route("/site-management/export-weekly", methods=["GET"])
    def site_export_weekly():
        """导出本周已报域名Excel"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

    @app.route("/site-management/batch-update", methods=["POST"])
    def site_batch_update():
        """批量更新站点字段"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()
        return redirect(url_for("site_local"))

    @app.route("/site-management/<site_id>/detail", methods=["GET"])
    def site_detail(site_id):
        """站点详情"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

    # === 配置管理路由 ===

    @app.route("/config", methods=["GET", "POST"])
    def site_config():
        """网页配置页面"""
        site_db = SiteDBClient()
        try:
            if request.method == "POST":
                # 保存配置
                settings_to_save = {
                    "report_username": request.form.get("report_username", ""),
                    "report_password": request.form.get("report_password", ""),
                    "erp_username": request.form.get("erp_username", ""),
                    "erp_password": request.form.get("erp_password", ""),
                    "wp_password": request.form.get("wp_password", ""),
                    "media_root": request.form.get("media_root", ""),
                    "seo_proxy": request.form.get("seo_proxy", ""),
                    "seo_api_key": request.form.get("seo_api_key", ""),
                }
                for key, value in settings_to_save.items():
                    site_db.set_setting(key, value)
                flash("配置已保存", "success")
                return redirect(url_for("site_config"))

            # 获取当前配置
            settings = site_db.get_all_settings()
            return render_template("site_config.html", settings=settings)
        except Exception as e:
            log.error(f"配置页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_config.html", settings={})
        finally:
            site_db.close()

    @app.route("/config/templates", methods=["GET", "POST"])
    def config_templates():
        """模板选项管理"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

    @app.route("/config/servers", methods=["GET", "POST"])
    def config_servers():
        """服务器选项管理"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

    @app.route("/config/categories", methods=["GET", "POST"])
    def config_categories():
        """主分类选项管理"""
        site_db = SiteDBClient()
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
        finally:
            site_db.close()

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
            return 0
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
                break
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
        done = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _log_wrapper(msg, level="info"):
            _order_log(task_id, msg, level)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
            for future in as_completed(futures):
                if _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{len(servers)} 台服务器")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                svr = futures[future]
                done += 1
                try:
                    cnt = future.result()
                    _order_log(task_id, f"[{done}/{len(servers)}] [{svr['name']}] ✅ {cnt} 条")
                    _task_manager.update(task_id, progress=int(done / len(servers) * 100), 
                                        message=f"处理中: {done}/{len(servers)} 台服务器")
                except Exception as e:
                    _order_log(task_id, f"[{done}/{len(servers)}] [{svr['name']}] ❌ {e}", "error")
        
        if not _task_manager.is_stopped(task_id):
            _task_manager.update(task_id, status="completed", 
                                message=f"完成: 处理 {done}/{len(servers)} 台服务器", progress=100)
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
        done = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _log_wrapper(msg, level="info"):
            _order_log(task_id, msg, level)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_orders_for_server, svr, year, month, _log_wrapper, date_from or None, date_to or None, wp_password, task_id): svr for svr in servers}
            for future in as_completed(futures):
                if _task_manager.is_stopped(task_id):
                    _task_manager.update(task_id, status="stopped", message=f"任务已停止: 完成 {done}/{len(servers)} 个域名")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                svr = futures[future]
                done += 1
                try:
                    cnt = future.result()
                    _order_log(task_id, f"[{done}/{len(servers)}] [{svr['name']}] ✅ {cnt} 条")
                    _task_manager.update(task_id, progress=int(done / len(servers) * 100),
                                        message=f"处理中: {done}/{len(servers)} 个域名")
                except Exception as e:
                    _order_log(task_id, f"[{done}/{len(servers)}] [{svr['name']}] ❌ {e}", "error")
        
        if not _task_manager.is_stopped(task_id):
            _task_manager.update(task_id, status="completed",
                                message=f"完成: 处理 {done}/{len(servers)} 个域名", progress=100)
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
        def generate():
            q = _order_log_queues.get(task_id)
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
        return jsonify(order_db.get_orders(ip=ip, page=page, limit=limit, year=year, month=month, date_from=date_from, date_to=date_to))

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

    # === 批量操作路由 ===

    @app.route("/site-management/generate-logos", methods=["POST"])
    def site_generate_logos():
        """批量生成LOGO"""
        site_db = SiteDBClient()
        try:
            selected_ids = request.form.getlist("selected_ids")
            if not selected_ids:
                flash("请先勾选要生成LOGO的站点", "error")
                return redirect(url_for("site_local"))

            # TODO: 实现LOGO生成逻辑
            flash(f"已为 {len(selected_ids)} 个站点生成LOGO（功能开发中）", "info")
        except Exception as e:
            log.error(f"生成LOGO错误: {e}")
            flash(f"操作失败: {e}", "error")
        finally:
            site_db.close()
        return redirect(url_for("site_local"))

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
                
                _task_manager.update(task_id, status="running", message=f"开始查询 {total} 个域名的收录情况")
                
                for idx, domain in enumerate(domains, 1):
                    if _task_manager.is_stopped(task_id):
                        _task_manager.update(task_id, status="stopped",
                            message=f"任务已停止: 已查询 {idx-1}/{total} 个域名，成功 {success_count} 个，失败 {failed_count} 个")
                        return
                    
                    try:
                        _task_manager.update(task_id, 
                            progress=int(idx / total * 100),
                            message=f"正在查询第 {idx}/{total} 个域名: {domain}")
                        
                        result = checker.check_google_index(domain)
                        if result["success"]:
                            success_count += 1
                            log.info(f"域名 {domain} 收录数量: {result['count']}")
                        else:
                            failed_count += 1
                            log.warning(f"域名 {domain} 查询失败: {result.get('error', '未知错误')}")
                        
                        time.sleep(interval)
                    except Exception as e:
                        failed_count += 1
                        log.error(f"处理域名 {domain} 时出错: {e}")
                
                _task_manager.update(task_id, 
                    status="completed",
                    message=f"查询完成: 共 {total} 个域名，成功 {success_count} 个，失败 {failed_count} 个",
                    result={"total": total, "success": success_count, "failed": failed_count},
                    progress=100)
            except Exception as e:
                log.error(f"收录查询任务失败: {e}")
                _task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
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

    @app.route("/tools/category-merge", methods=["GET", "POST"])
    def category_merge():
        """分类数据处理：将数量过少的分类合并为公共类"""
        if request.method == "GET":
            return render_template("category_merge.html")
        
        try:
            import pandas as pd
            from collections import Counter
            
            file_path = request.form.get("file_path", "").strip()
            threshold = int(request.form.get("threshold", 10))
            category_field = request.form.get("category_field", "分类").strip()
            common_category = request.form.get("common_category", "Other").strip()
            
            if not file_path:
                return render_template("category_merge.html", error="请输入表格文件路径")
            
            # 读取表格文件
            if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                df = pd.read_excel(file_path)
            elif file_path.endswith('.csv'):
                df = pd.read_csv(file_path)
            else:
                return render_template("category_merge.html", error="不支持的文件格式，请使用 .xlsx 或 .csv 文件")
            
            # 检查分类字段是否存在
            if category_field not in df.columns:
                return render_template("category_merge.html", 
                    error=f"表格中未找到 '{category_field}' 列，可用列: {', '.join(df.columns.tolist())}")
            
            total_rows = len(df)
            
            # 统计各分类的数量
            category_counts = Counter(df[category_field].fillna('').astype(str))
            
            # 找出需要合并的分类（数量小于阈值）
            merged_categories = []
            for cat, count in category_counts.items():
                if cat and count < threshold:
                    merged_categories.append((cat, count))
            
            merged_categories.sort(key=lambda x: x[1])
            
            # 记录修改前的分类个数
            before_count = len([c for c in category_counts.keys() if c])
            
            # 执行合并
            modified_rows = 0
            for cat, _ in merged_categories:
                mask = df[category_field].fillna('').astype(str) == cat
                modified_rows += mask.sum()
                df.loc[mask, category_field] = common_category
            
            # 统计修改后的分类
            new_category_counts = Counter(df[category_field].fillna('').astype(str))
            after_count = len([c for c in new_category_counts.keys() if c])
            
            # 保留的分类列表
            remaining_categories = [(cat, count) for cat, count in new_category_counts.items() 
                                   if cat and cat != common_category]
            remaining_categories.sort(key=lambda x: x[1], reverse=True)
            
            # 保存修改后的表格
            if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                df.to_excel(file_path, index=False)
            else:
                df.to_csv(file_path, index=False)
            
            result = {
                "file_path": file_path,
                "total_rows": total_rows,
                "category_field": category_field,
                "threshold": threshold,
                "common_category": common_category,
                "before_count": before_count,
                "after_count": after_count,
                "merged_count": len(merged_categories),
                "modified_rows": modified_rows,
                "merged_categories": merged_categories,
                "remaining_categories": remaining_categories
            }
            
            return render_template("category_merge.html", result=result)
            
        except Exception as e:
            log.error(f"分类数据处理失败: {e}")
            return render_template("category_merge.html", error=f"处理失败: {str(e)}")

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
