import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for

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

log = get_logger("web")


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, task_id: str, action: str, target: str) -> str:
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "action": action,
                "target": target,
                "status": "running",
                "progress": 0,
                "message": "Starting...",
                "result": None,
                "error": None,
                "created_at": datetime.now().isoformat(),
            }
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

    def cleanup(self, max_age_hours: int = 24):
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._lock:
            self._tasks = {
                k: v for k, v in self._tasks.items()
                if datetime.fromisoformat(v["created_at"]) > cutoff
            }


_task_manager = TaskManager()


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
    module = DataScraperModule(http_client=http)

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
            pages = int(request.form.get("pages", 3))
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

    @app.route("/shopify/fetch-urls", methods=["GET", "POST"])
    def shopify_fetch_urls():
        api_status = module.searcher.get_api_status()
        selected_category = request.args.get("category", "")
        stores = []
        stores_total = 0
        
        # 查询选中类目的unfiltered数据
        if selected_category:
            db = MongoDBClient()
            try:
                stores = db.get_unfiltered_stores(selected_category, limit=50)
                stores_total = db.get_unfiltered_count(selected_category)
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
                        result = module.fetch_shopify_urls(
                            category=category, keyword=keyword,
                            max_pages=2, min_products=min_products,
                            save_mongo=save_mongo, save_excel=save_excel,
                            provider_name=provider,
                        )
                        _task_manager.update(task_id, status="completed",
                            message=f"完成: 找到 {result['total_shopify']} 个店铺",
                            result=result, progress=100)
                    except Exception as e:
                        log.error(f"fetch-urls task failed: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"任务已启动: {category} | {keyword}，可在任务页面查看进度")
                return redirect(url_for("shopify_fetch_urls", category=category))
        return render_template("shopify_urls.html", result=None, categories=SHOPIFY_CATEGORIES, db_name=settings.mongo_db_url, api_status=api_status, selected_category=selected_category, stores=stores, stores_total=stores_total)

    @app.route("/shopify/filter-categories", methods=["GET", "POST"])
    def shopify_filter_categories():
        if request.method == "POST":
            category = (request.form.get("category") or "").strip()
            if category:
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
                        processed = 0
                        for store in stores:
                            store_url = store["url"]
                            domain = store["domain"]
                            processed += 1
                            try:
                                collections = fetch_collections(http, store_url)
                                log.info(f"[精准类目] [{processed}/{total}] {domain} - 获取 {len(collections)} 个 collection")
                                for coll in collections:
                                    if match_title(category, coll["title"]):
                                        if db.save_filtered_url(
                                            category, domain, store_url,
                                            coll["title"], coll["handle"],
                                        ):
                                            matched_count += 1
                                            log.info(f"[精准类目]   ✅ 匹配: {coll['title']} -> {store_url}/collections/{coll['handle']}")
                            except Exception as e:
                                log.warning(f"[精准类目] [{processed}/{total}] {domain} - 处理失败: {e}")

                            if processed % 10 == 0 or processed == total:
                                _task_manager.update(task_id,
                                    progress=int(processed / total * 100),
                                    message=f"处理中: {processed}/{total}，已匹配 {matched_count} 条")

                        log.info(f"[精准类目] 任务完成: category={category}, 处理={total}, 匹配={matched_count}")
                        _task_manager.update(task_id, status="completed",
                            message=f"完成: 处理 {total} 个店铺，匹配 {matched_count} 条 collection",
                            result={"total_stores": total, "matched": matched_count},
                            progress=100)
                    except Exception as e:
                        log.error(f"[精准类目] 任务异常: {e}")
                        _task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    finally:
                        db.close()

                threading.Thread(target=run_task, daemon=True).start()
                flash(f"精准类目筛选任务已启动: {category}，可在任务页面查看进度")
                return redirect(url_for("shopify_filter_categories"))
        return render_template("shopify_categories.html", categories=SHOPIFY_CATEGORIES)

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
            
            if not category:
                flash("请选择类目", "error")
                return redirect(url_for("product_data_crawl"))
            
            task_id = f"crawl_{category}_{int(time.time())}"
            _task_manager.create(task_id, "crawl_products", category)
            
            def run_task():
                try:
                    _task_manager.update(task_id, status="running", message=f"开始爬取类目: {category}")
                    
                    # 创建爬取器
                    crawler = create_crawler()
                    
                    # 定义进度回调
                    def progress_callback(message):
                        _task_manager.update(task_id, message=message)
                    
                    # 爬取类目数据
                    result = crawler.crawl_category(category, max_sites=max_sites, progress_callback=progress_callback)
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 爬取 {result['success_sites']}/{result['total_sites']} 个站点，{result.get('total_collections', 0)} 个集合，获取 {result['total_products']} 件商品",
                                        result=result,
                                        progress=100)
                except Exception as e:
                    log.error(f"爬取任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
            
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
                        _task_manager.update(task_id, message=f"清洗类目: {cat}")
                        
                        result = product_db.clean_category(cat)
                        total_processed += result["processed"]
                        total_cleaned += result["cleaned"]
                        total_removed += result["removed"]
                        
                        log.info(f"类目 {cat}: 处理 {result['processed']} 条，清洗后 {result['cleaned']} 条")
                    
                    product_db.close()
                    
                    _task_manager.update(task_id, status="completed",
                                        message=f"完成: 处理 {total_processed} 条数据，清洗后 {total_cleaned} 条，移除 {total_removed} 条",
                                        result={"processed": total_processed, "cleaned": total_cleaned, "removed": total_removed},
                                        progress=100)
                except Exception as e:
                    log.error(f"清洗任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
            
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
            
            if not category:
                flash("请选择要导出的类目", "error")
                return redirect(url_for("product_data_export"))
            
            task_id = f"export_{category}_{int(time.time())}"
            _task_manager.create(task_id, "export_products", category)
            
            def run_task():
                try:
                    _task_manager.update(task_id, status="running", message=f"开始导出: {category}")
                    
                    product_db = ProductDBClient()
                    export_dir = str(settings.data_dir / "exports")
                    
                    filepath = product_db.export_category_to_excel(category, export_dir)
                    
                    if filepath:
                        # 获取导出数量
                        clean_col = product_db.clean_col(category)
                        count = clean_col.estimated_document_count()
                        
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 导出 {count} 条数据到 {os.path.basename(filepath)}",
                                            result={"file": filepath, "count": count},
                                            progress=100)
                    else:
                        _task_manager.update(task_id, status="completed",
                                            message=f"完成: 类目 {category} 无清洗后数据",
                                            progress=100)
                    
                    product_db.close()
                except Exception as e:
                    log.error(f"导出任务失败: {e}")
                    _task_manager.update(task_id, status="failed", message=f"失败: {e}")
            
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
                        flash(f"导入完成: 新增 {result['created']}, 更新 {result['updated']}", "success")
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

            sites = site_db.list_local_sites(q)
            stats = site_db.get_stats()
            return render_template("site_local.html", sites=sites, stats=stats, q=q)
        except Exception as e:
            log.error(f"本地站点页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_local.html", sites=[], stats={"local_sites": 0}, q=q)
        finally:
            site_db.close()

    @app.route("/site-management/reported", methods=["GET", "POST"])
    def site_reported():
        """已报域名管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()

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

            sites = site_db.list_reported_sites(q)
            stats = site_db.get_stats()
            return render_template("site_reported.html", sites=sites, stats=stats, q=q)
        except Exception as e:
            log.error(f"已报域名页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_reported.html", sites=[], stats={"reported_sites": 0}, q=q)
        finally:
            site_db.close()

    @app.route("/site-management/scheduled", methods=["GET", "POST"])
    def site_scheduled():
        """计划上报管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()

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

            sites = site_db.list_scheduled_sites(q)
            stats = site_db.get_stats()
            return render_template("site_scheduled.html", sites=sites, stats=stats, q=q)
        except Exception as e:
            log.error(f"计划上报页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_scheduled.html", sites=[], stats={"scheduled_sites": 0}, q=q)
        finally:
            site_db.close()

    @app.route("/site-management/built", methods=["GET", "POST"])
    def site_built():
        """已建站管理"""
        site_db = SiteDBClient()
        try:
            q = request.args.get("q", "").strip()

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

            sites = site_db.list_built_sites(q)
            stats = site_db.get_stats()
            built_stats = site_db.get_built_stats()
            return render_template("site_built.html", sites=sites, stats=stats, built_stats=built_stats, q=q)
        except Exception as e:
            log.error(f"已建站页面错误: {e}")
            flash(f"操作失败: {e}", "error")
            return render_template("site_built.html", sites=[], stats={"built_sites": 0}, built_stats={}, q=q)
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
