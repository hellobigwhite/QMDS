import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for

from qmds.config import settings
from qmds.config.categories import SHOPIFY_CATEGORIES
from qmds.db.mongodb import MongoDBClient
from qmds.modules.data_scraper import DataScraperModule
from qmds.modules.data_scraper.category_matcher import match_title
from qmds.modules.data_scraper.collections_fetcher import fetch_collections
from qmds.utils.http_client import HttpClient
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
    http = http_client or HttpClient()
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
                return redirect(url_for("shopify_fetch_urls"))
        return render_template("shopify_urls.html", result=None, categories=SHOPIFY_CATEGORIES, db_name=settings.mongo_db_url, api_status=api_status)

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
        return render_template("product_data.html",
                               total_collections=0,
                               non_empty_collections=0,
                               total_rows=0,
                               collections=[])

    @app.route("/product-data/clean", methods=["POST"])
    def product_data_clean():
        collection = request.form.get("collection", "__all__")
        flash(f"数据清洗任务已启动: {collection}")
        return redirect(url_for("product_data"))

    @app.route("/site-management", methods=["GET"])
    def site_management():
        q = request.args.get("q", "")
        return render_template("site_management.html",
                               total_sites=0,
                               built_sites=0,
                               reported_sites=0,
                               sites=[],
                               q=q)

    @app.route("/site-management/<int:site_id>", methods=["GET"])
    def site_detail(site_id):
        flash(f"站点详情页面开发中 (ID: {site_id})")
        return redirect(url_for("site_management"))

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
