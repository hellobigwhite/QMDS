"""QMDS Web 控制台 — Flask 应用工厂 + Blueprint 注册"""

import os
import sys
import threading
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask

from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger

log = get_logger("web")


def create_app(http_client: Optional[HttpClient] = None) -> Flask:
    load_dotenv()

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
        static_url_path="/static",
    )
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

    # 将 http_client 存储到 app 上，供 Blueprint 使用
    app.config["HTTP_CLIENT"] = http_client

    # 注册 teardown
    from qmds.modules.web.db_helpers import close_db_connections
    app.teardown_appcontext(close_db_connections)

    # 注册全局模板上下文
    from datetime import datetime

    @app.context_processor
    def inject_globals():
        return {"now": datetime.now(), "module_name": "QMDS 管理控制台"}

    # 启动任务清理调度器
    from qmds.modules.web.task_manager import (
        start_cleanup_scheduler,
        start_counter_calibration_scheduler,
    )
    start_cleanup_scheduler()

    # 启动计数器定时校准调度器（间隔小时数，默认 6；<=0 禁用）
    calibrate_hours = float(os.environ.get("COUNTER_CALIBRATE_HOURS", "6"))
    if calibrate_hours > 0:
        start_counter_calibration_scheduler(interval_hours=calibrate_hours)

    # 启动域名状态自动更新调度器
    from qmds.modules.web.services.domain_status_scheduler import start_domain_status_scheduler
    start_domain_status_scheduler()

    # 注册所有 Blueprint
    from qmds.modules.web.routes.core import bp as core_bp
    from qmds.modules.web.routes.shopify import bp as shopify_bp
    from qmds.modules.web.routes.product_data import bp as product_data_bp
    from qmds.modules.web.routes.site_management import bp as site_management_bp
    from qmds.modules.web.routes.config_routes import bp as config_bp
    from qmds.modules.web.routes.orders import bp as orders_bp
    from qmds.modules.web.routes.scheduler import bp as scheduler_bp
    from qmds.modules.web.routes.seo import bp as seo_bp
    from qmds.modules.web.routes.tools import bp as tools_bp

    app.register_blueprint(core_bp)
    app.register_blueprint(shopify_bp)
    app.register_blueprint(product_data_bp)
    app.register_blueprint(site_management_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(scheduler_bp)
    app.register_blueprint(seo_bp)
    app.register_blueprint(tools_bp)

    log.info("Web 应用初始化完成，已注册 9 个 Blueprint")
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
        serve(
            self.app,
            host=self.host,
            port=self.port,
            threads=8,
            channel_timeout=30,
            recv_bytes=1048576,
            send_bytes=47104,
        )

    def run_dev(self):
        log.info(f"Starting dev web console on http://127.0.0.1:{self.port}")
        self.app.run(host="127.0.0.1", port=self.port, debug=self.debug)

    def start_background(self):
        self._server = threading.Thread(target=self.run, daemon=True)
        self._server.start()
        return self._server
