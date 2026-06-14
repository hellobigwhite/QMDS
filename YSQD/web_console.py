import base64
import os
import random
import re
import json
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from io import BytesIO
import threading
import traceback
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests
from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from pymongo import MongoClient

from constants import (
    AUTO_CATEGORY_STATUS_COL,
    AUTO_CATEGORY_TIME_COL,
    BUILD_STATUS_COL,
    BUILD_TIME_COL,
    CATEGORY_ID_MAP,
    COLUMNS,
    DB_PATH,
    DOMAIN_RESOLVED_TIME_COL,
    DOMAIN_STATUS_LABELS,
    DOMAIN_NUMBER_COL,
    EXTRA_COLUMNS,
    EXTRA_DATA_STATUS_COL,
    EXTRA_DATA_TIME_COL,
    HEALTH_STATUS_COL,
    HEALTH_TIME_COL,
    MAIN_CATEGORY_STATUS_COL,
    MAIN_CATEGORY_TIME_COL,
    MAIN_DATA_STATUS_COL,
    MAIN_DATA_TIME_COL,
    MEDIA_STATUS_COL,
    MEDIA_TIME_COL,
    PLUGIN_STATUS_COL,
    PLUGIN_TIME_COL,
    REPORT_STATUS_COL,
    REPORT_TIME_COL,
    SCHEDULE_ENABLED_COL,
    SCHEDULE_TIME_COL,
    TABLE_NAME,
)
from datastore import DataStore
from domain_reporter_client import DomainReporter
from erp_builder import ERPBuilder
from health_checker import healthcheck_domain
from main_category_uploader_v2 import MainCategoryUploader
from main_data_uploader import MainDataUploader
from wp_cache_purger import WpCachePurger
from wp_media_config import DEFAULT_MEDIA_ROOT, WPMediaConfigurator
from wp_plugin_button_clicker import WpPluginButtonClicker
from woo_order_checker import ORDER_STATUS_LABELS, WooOrderChecker


app = Flask(__name__, template_folder="templates_console", static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("YSQD_WEB_SECRET", "ysqd-web-console")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

UPLOAD_FOLDER = "uploads_console"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(_error):
    flash("上传文件过大，当前上限为 200MB，请压缩后重试。", "error")
    return redirect(request.referrer or url_for("product_processing_page"))

TASKS = {}
TASK_LOCK = threading.Lock()
UPLOAD_MAX_WORKERS = 10
AUTO_SCHEDULE_INTERVAL_SECONDS = 30
AUTO_SCHEDULE_LOCK = threading.Lock()
AUTO_SCHEDULE_ACTIVE_IDS = set()
AUTO_SCHEDULE_FIRED = {}
AUTO_SCHEDULE_THREAD_STARTED = False

EXCEL_FIELD_ALIASES = {
    "classification": ["分类", "classification"],
    "build_flag": ["是否建站", "build_flag"],
    "domain": ["域名", "domain"],
    "template": ["底板", "模板", "template"],
    "main_data_source_id": ["主分类数据码", "主数据源ID", "主数据源id", "main_data_source_id"],
    "extra_data_source_id": ["站群数据码", "补充数据源ID", "补充数据源id", "extra_data_source_id"],
    "main_category": ["主分类", "主打类目", "main_category"],
    "category": ["大类", "category"],
    "schedule_time": ["时间", "schedule_time"],
    "title": ["SEO Title（最大58字符）", "SEO Title(最大58字符)", "SEO Title", "title"],
    "title_translation": ["中文标题翻译", "title_translation"],
    "description": ["Meta Description", "描述", "description"],
    "description_translation": ["中文描述翻译", "description_translation"],
    "main_keyword": ["主关键词", "main_keyword"],
    "long_tail_keywords": ["长尾关键词", "long_tail_keywords"],
    "address": ["地址", "address"],
    "server": ["服务器", "server"],
}

EDIT_SECTIONS = [
    (
        "基础信息",
        [
            ("domain", "域名"),
            ("template", "模板"),
            ("server", "服务器"),
            ("category", "大类"),
            ("main_category", "主分类"),
            ("main_data_source_id", "主数据源ID"),
            ("extra_data_source_id", "补充数据源ID"),
            ("title", "标题"),
            ("description", "描述"),
            ("address", "地址"),
            ("store_pf", "盘符"),
        ],
    ),
    (
        "上报与计划",
        [
            ("report_status", "上报状态"),
            ("report_time", "上报时间"),
            ("report_id", "上报ID"),
            ("domain_status", "域名状态值"),
            ("domain_resolved_time", "解析时间"),
            ("schedule_enabled", "计划开关"),
            ("schedule_time", "计划时间"),
        ],
    ),
    (
        "建站与上传状态",
        [
            ("build_status", "建站状态"),
            ("build_time", "建站时间"),
            ("health_status", "健康状态"),
            ("health_time", "健康时间"),
            ("main_data_status", "主数据状态"),
            ("main_data_time", "主数据时间"),
            ("extra_data_status", "补充数据状态"),
            ("extra_data_time", "补充数据时间"),
            ("main_category_status", "主分类状态"),
            ("main_category_time", "主分类时间"),
            ("plugin_status", "插件状态"),
            ("plugin_time", "插件时间"),
            ("media_status", "媒体状态"),
            ("media_time", "媒体时间"),
        ],
    ),
    (
        "登录检查",
        [
            ("login_path", "登录路径（如 /bbwllogin）"),
        ],
    ),
]

EDIT_SELECT_OPTIONS = {
    "report_status": ["", "未报", "已报"],
    "domain_status": ["", "1", "2", "3", "4"],
    "schedule_enabled": ["0", "1"],
    "build_status": ["", "已建站"],
    "health_status": ["", "正常", "异常"],
    "main_data_status": ["", "未上传", "已上传"],
    "extra_data_status": ["", "未上传", "已上传"],
    "main_category_status": ["", "未设置", "已上传"],
    "plugin_status": ["", "未配置", "已配置"],
    "media_status": ["", "未配置", "已配置"],
    "store_pf": ["", "/www/wwwroot/", "/home/www/"],
}


FIELD_LABELS = {
    field_name: field_label
    for _section_title, fields in EDIT_SECTIONS
    for field_name, field_label in fields
}

BATCH_EDIT_FIELD_KEYS = [
    "template",
    "server",
    "store_pf",
    "category",
    "main_category",
    "main_data_source_id",
    "extra_data_source_id",
    "title",
    "description",
    "address",
    "report_status",
    "domain_status",
    "schedule_enabled",
    "schedule_time",
    "build_status",
    "health_status",
    "main_data_status",
    "extra_data_status",
    "main_category_status",
    "plugin_status",
    "media_status",
]

BATCH_EDIT_FIELDS = [(field, FIELD_LABELS.get(field, field)) for field in BATCH_EDIT_FIELD_KEYS]
BATCH_EDIT_FIELD_SET = {field for field, _label in BATCH_EDIT_FIELDS}


def build_batch_edit_option_items():
    option_items = {}
    for field, _label in BATCH_EDIT_FIELDS:
        if field not in EDIT_SELECT_OPTIONS:
            continue
        items = []
        for option in EDIT_SELECT_OPTIONS[field]:
            if field == "domain_status":
                label = "清空" if option == "" else DOMAIN_STATUS_LABELS.get(option, DOMAIN_STATUS_LABELS.get(str(option), str(option)))
            elif field == "schedule_enabled":
                label = "清空" if option == "" else ("开启" if option == "1" else "关闭")
            else:
                label = "清空" if option == "" else option
            items.append({"value": option, "label": label})
        option_items[field] = items
    return option_items


BATCH_EDIT_OPTION_ITEMS = build_batch_edit_option_items()


def get_store():
    return DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)


@app.context_processor
def inject_batch_edit_context():
    return {
        "batch_edit_fields": BATCH_EDIT_FIELDS,
        "batch_edit_option_items": BATCH_EDIT_OPTION_ITEMS,
    }


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_text(row, key):
    value = row[key] if key in row.keys() else ""
    return (value or "").strip()


def normalize_ids(values):
    ids = []
    for value in values:
        value = str(value).strip()
        if value:
            ids.append(value)
    return ids


def filter_rows_by_domain(rows, keyword):
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return list(rows)
    return [row for row in rows if keyword in row_text(row, "domain").lower()]


def sort_rows_by_time_desc(rows, time_col):
    def sort_key(row):
        parsed = parse_datetime_text(row_text(row, time_col))
        if parsed is None:
            return datetime.min
        return parsed

    return sorted(rows, key=sort_key, reverse=True)


def domain_status_label(value):
    return DOMAIN_STATUS_LABELS.get(value, DOMAIN_STATUS_LABELS.get(str(value), "未知"))


def get_settings_snapshot():
    store = get_store()
    try:
        return {
            "report_username": store.get_setting("report_username", "").strip(),
            "report_password": store.get_setting("report_password", "").strip(),
            "erp_username": store.get_setting("erp_username", "").strip(),
            "erp_password": store.get_setting("erp_password", "").strip(),
            "wp_password": store.get_setting("wp_password", "").strip(),
            "media_root": store.get_setting("media_root", DEFAULT_MEDIA_ROOT).strip() or DEFAULT_MEDIA_ROOT,
        }
    finally:
        store.close()


def list_all_sites():
    store = get_store()
    try:
        return store.query_rows("")
    finally:
        store.close()


def create_task(title, action, site_ids):
    task_id = uuid.uuid4().hex[:10]
    task = {
        "id": task_id,
        "title": title,
        "action": action,
        "site_ids": list(site_ids),
        "site_count": len(site_ids),
        "status": "queued",
        "created_at": now_str(),
        "updated_at": now_str(),
        "success_count": 0,
        "failed_count": 0,
        "logs": [],
        "errors": [],
        "stop_requested": False,
    }
    with TASK_LOCK:
        TASKS[task_id] = task
    return task


def list_tasks():
    with TASK_LOCK:
        # Keep task positions stable in the task center.
        # Dict insertion order matches task creation order, so cards no longer jump
        # around whenever logs or status updates change `updated_at`.
        return list(reversed(list(TASKS.values())))


def append_task_log(task, message):
    with TASK_LOCK:
        task["logs"].append(f"[{now_str()}] {message}")
        task["logs"] = task["logs"][-2000:]
        task["updated_at"] = now_str()


def set_task_status(task, status):
    with TASK_LOCK:
        task["status"] = status
        task["updated_at"] = now_str()


def get_task(task_id):
    with TASK_LOCK:
        return TASKS.get(task_id)


def is_task_stop_requested(task):
    with TASK_LOCK:
        return bool(task.get("stop_requested"))


def request_task_stop(task):
    with TASK_LOCK:
        task["stop_requested"] = True
        if task.get("status") in {"queued", "running"}:
            task["status"] = "stopping"
        task["updated_at"] = now_str()
        task["logs"].append(f"[{now_str()}] 已收到停止请求，等待任务安全退出")
        task["logs"] = task["logs"][-400:]


def increment_task_counts(task, success=0, failed=0, error=None):
    with TASK_LOCK:
        task["success_count"] += int(success or 0)
        task["failed_count"] += int(failed or 0)
        if error:
            task["errors"].append(str(error))
        task["updated_at"] = now_str()


def run_background_task(task, runner):
    def target():
        set_task_status(task, "running")
        append_task_log(task, f"任务开始: {task['title']}")
        try:
            if is_task_stop_requested(task):
                set_task_status(task, "stopped")
                append_task_log(task, "任务已停止")
                append_task_log(task, f"任务结束: 成功 {task['success_count']}，失败 {task['failed_count']}")
                return
            runner(task)
            if is_task_stop_requested(task):
                set_task_status(task, "stopped")
                append_task_log(task, "任务已停止")
            elif task["status"] == "running":
                set_task_status(task, "completed")
            append_task_log(task, f"任务结束: 成功 {task['success_count']}，失败 {task['failed_count']}")
        except Exception as exc:
            task["errors"].append(str(exc))
            append_task_log(task, f"任务异常: {exc}")
            append_task_log(task, traceback.format_exc())
            set_task_status(task, "failed")

    threading.Thread(target=target, daemon=True).start()


def start_task(title, action, site_ids, runner):
    task = create_task(title, action, site_ids)
    run_background_task(task, runner)
    return task["id"]


def task_site_log(task, domain, message):
    append_task_log(task, f"[{domain}] {message}")


def task_progress_callback(task, domain):
    def callback(message):
        task_site_log(task, domain, message)

    return callback


def ensure_required_setting(settings, key, label):
    value = (settings.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"请先在网页配置页填写 {label}")
    return value


def get_rows_for_ids(store, site_ids):
    rows = []
    for site_id in site_ids:
        row = store.get_row(site_id)
        if row:
            rows.append(row)
    return rows


def row_to_dict(row):
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def update_site_fields(row_id, values):
    store = get_store()
    try:
        store.update_fields(row_id, values)
    finally:
        store.close()


def run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label=""):
    if not rows:
        return

    worker_count = max(1, min(int(max_workers or 1), len(rows)))
    if label:
        append_task_log(task, f"{label}启用 {worker_count} 个线程")

    row_iter = iter(rows)
    futures = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        def submit_next():
            if is_task_stop_requested(task):
                return False
            try:
                next_row = next(row_iter)
            except StopIteration:
                return False
            future = executor.submit(worker, next_row)
            futures[future] = next_row
            return True

        for _ in range(worker_count):
            if not submit_next():
                break

        while futures:
            done, _pending = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                row = futures.pop(future, None)
                try:
                    future.result()
                except Exception as exc:
                    domain = row_text(row or {}, "domain") or "鏈煡鍩熷悕"
                    increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
                    task_site_log(task, domain, f"并发执行异常: {exc}")
                submit_next()


def build_uploaded_sync_updates(row, current_time):
    updates = {
        BUILD_STATUS_COL: "已建站",
        MAIN_DATA_STATUS_COL: "已上传",
        EXTRA_DATA_STATUS_COL: "已上传",
    }
    if not row_text(row, BUILD_TIME_COL):
        updates[BUILD_TIME_COL] = current_time
    if not row_text(row, MAIN_DATA_TIME_COL):
        updates[MAIN_DATA_TIME_COL] = current_time
    if not row_text(row, EXTRA_DATA_TIME_COL):
        updates[EXTRA_DATA_TIME_COL] = current_time
    return updates


def clean_excel_value(value):
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def normalize_header(value):
    return "".join(ch.lower() for ch in str(value) if ch not in " \t\r\n_-()（）")


def get_excel_cell(row, field_name, default_index=None):
    aliases = EXCEL_FIELD_ALIASES[field_name]
    normalized_map = {normalize_header(col): col for col in row.index}
    for alias in aliases:
        col_name = normalized_map.get(normalize_header(alias))
        if col_name is not None:
            return clean_excel_value(row[col_name])
    if default_index is not None and default_index < len(row.index):
        return clean_excel_value(row.iloc[default_index])
    return ""


def get_selected_ids():
    return normalize_ids(request.form.getlist("selected_ids"))


def parse_domains_text(raw_text):
    seen = set()
    domains = []
    for chunk in re.split(r"[\s,;，；]+", str(raw_text or "").strip()):
        value = chunk.strip()
        if not value:
            continue
        value = value.replace("https://", "").replace("http://", "").strip().strip("/")
        if value.startswith("www."):
            value = value[4:]
        value = value.lower()
        if not value or value in seen:
            continue
        seen.add(value)
        domains.append(value)
    return domains


def redirect_to_task(task_id, message):
    flash(message, "success")
    return redirect(url_for("tasks_list", task_id=task_id))


def is_reported(row):
    return row_text(row, REPORT_STATUS_COL) == "已报"


def is_schedule_enabled(row):
    return row_text(row, SCHEDULE_ENABLED_COL).lower() in {"1", "true", "yes"}


def format_schedule_text(row):
    if not is_schedule_enabled(row):
        return "无计划"
    return row_text(row, SCHEDULE_TIME_COL) or "已开启"


def parse_schedule_input(text):
    value = (text or "").strip()
    if not value:
        raise ValueError("请先选择计划时间")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    raise ValueError("计划时间格式不正确")


def redirect_back(default_endpoint="sites_list"):
    endpoint = (request.form.get("next_endpoint") or default_endpoint).strip()
    return redirect(url_for(endpoint if endpoint else default_endpoint))


def normalize_form_value(field, value):
    text = (value or "").strip()
    if field == "schedule_enabled":
        return "1" if text in {"1", "true", "True", "yes", "on"} else "0"
    if field.endswith("_time") and "T" in text:
        return text.replace("T", " ")
    return text


def format_batch_value_display(field, value):
    text = (value or "").strip()
    if text == "":
        return "清空"
    if field == "domain_status":
        return domain_status_label(text)
    if field == "schedule_enabled":
        return "开启" if text == "1" else "关闭"
    return text


def parse_datetime_text(text):
    value = (text or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def current_week_range():
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def collect_due_scheduled_rows():
    now = datetime.now()
    store = get_store()
    try:
        rows = store.query_rows("")
    finally:
        store.close()

    due_rows = []
    with AUTO_SCHEDULE_LOCK:
        active_ids = set(AUTO_SCHEDULE_ACTIVE_IDS)
        fired = dict(AUTO_SCHEDULE_FIRED)

    for row in rows:
        if not is_schedule_enabled(row) or is_reported(row):
            continue
        schedule_time = row_text(row, SCHEDULE_TIME_COL)
        schedule_dt = parse_datetime_text(schedule_time)
        if schedule_dt is None or schedule_dt > now:
            continue
        site_id = str(row["id"])
        if site_id in active_ids:
            continue
        if fired.get(site_id) == schedule_time:
            continue
        due_rows.append(
            {
                "id": site_id,
                "domain": row_text(row, "domain") or "未知域名",
                "schedule_time": schedule_time,
            }
        )
    return due_rows


def start_auto_schedule_task(due_rows):
    if not due_rows:
        return None

    site_ids = [item["id"] for item in due_rows]
    due_snapshot = list(due_rows)
    with AUTO_SCHEDULE_LOCK:
        for item in due_snapshot:
            AUTO_SCHEDULE_ACTIVE_IDS.add(item["id"])
            AUTO_SCHEDULE_FIRED[item["id"]] = item["schedule_time"]

    title = f"自动计划上报 ({len(site_ids)} 个站点)"
    task = create_task(title, "scheduled_report", site_ids)
    append_task_log(task, "由计划上报自动触发")
    append_task_log(task, "触发站点: " + ", ".join(item["domain"] for item in due_snapshot))

    def runner(inner_task):
        try:
            run_report_task(inner_task)
        finally:
            with AUTO_SCHEDULE_LOCK:
                for item in due_snapshot:
                    AUTO_SCHEDULE_ACTIVE_IDS.discard(item["id"])

    run_background_task(task, runner)
    return task["id"]


def auto_schedule_loop():
    while True:
        try:
            due_rows = collect_due_scheduled_rows()
            if due_rows:
                start_auto_schedule_task(due_rows)
        except Exception as exc:
            print(f"[auto-schedule] {exc}")
        time.sleep(AUTO_SCHEDULE_INTERVAL_SECONDS)


def start_auto_schedule_worker():
    global AUTO_SCHEDULE_THREAD_STARTED
    with AUTO_SCHEDULE_LOCK:
        if AUTO_SCHEDULE_THREAD_STARTED:
            return
        AUTO_SCHEDULE_THREAD_STARTED = True
    threading.Thread(target=auto_schedule_loop, name="ysqd-auto-schedule", daemon=True).start()


def list_scraper_files():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data")
    items = []
    if not os.path.isdir(data_dir):
        return items
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            if name.lower().endswith(".xlsx"):
                full_path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(full_path)
                except OSError:
                    continue
                items.append(
                    {
                        "name": name,
                        "path": full_path,
                        "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def list_mongo_categories():
    try:
        from url_import_export import get_category_list

        return get_category_list()
    except Exception:
        return []


def list_url_source_collections():
    try:
        from url_auto_classifier import list_source_collections

        return list_source_collections()
    except Exception:
        return []


def list_product_source_collections():
    try:
        from pymongo import MongoClient
        client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)
        db = client["shopify_url"]
        result = []
        for coll_name in db.list_collection_names():
            if coll_name.endswith("_Unfiltered_URLs") or coll_name.endswith("_Filtered_URLs"):
                count = db[coll_name].count_documents({})
                result.append({"name": coll_name, "count": count})
        client.close()
        result.sort(key=lambda x: x["name"])
        return result
    except Exception:
        return []


def list_product_data_collections():
    try:
        from product_processing_v2 import list_product_collections

        return list_product_collections()
    except Exception as e:
        app.logger.exception("Failed to list product data collections: %s", e)
        return []


def list_clean_product_data_collections():
    return [item for item in list_product_data_collections() if item.get("name", "").endswith("_clean")]


def list_product_export_files():
    try:
        from product_exporter import list_export_files

        return list_export_files()
    except Exception:
        return []


def list_product_mismatch_reports():
    try:
        from product_mismatch_scanner import list_mismatch_reports

        return list_mismatch_reports()
    except Exception:
        return []


def get_reuse_queue_db():
    client = MongoClient("mongodb://localhost:27017/")
    return client, client["shopify_url"]


def _extract_domain(url):
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def reuse_datetime_text(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def is_reuse_queue_reusable(doc, now=None):
    now = now or datetime.now()
    status = str(doc.get("Status") or "").strip().lower()
    if status in {"disabled", "deleted"}:
        return False
    value = doc.get("NextReusableAt")
    if isinstance(value, datetime):
        next_time = value
    else:
        next_time = parse_datetime_text(str(value or ""))
    if next_time and next_time > now:
        return False
    return True


def get_reuse_queue_summary():
    client, db = get_reuse_queue_db()
    try:
        rows = []
        now = datetime.now()
        for collection_name in sorted(name for name in db.list_collection_names() if name.endswith("_Reuse_Queue")):
            collection = db[collection_name]
            total = collection.count_documents({})
            queued = collection.count_documents({"Status": "queued"})
            cooldown = collection.count_documents({"Status": "cooldown"})
            new_count = collection.count_documents({"Status": "new"})
            matched = collection.count_documents({"LastMatched": True})
            unmatched = collection.count_documents({"LastMatched": False})
            reusable = 0
            for doc in collection.find({}, {"Status": 1, "NextReusableAt": 1, "_id": 0}):
                if is_reuse_queue_reusable(doc, now):
                    reusable += 1
            rows.append(
                {
                    "collection": collection_name,
                    "category": collection_name[: -len("_Reuse_Queue")],
                    "total": total,
                    "queued": queued,
                    "cooldown": cooldown,
                    "new": new_count,
                    "matched": matched,
                    "unmatched": unmatched,
                    "reusable": reusable,
                }
            )
        return rows
    finally:
        client.close()


def get_reuse_queue_details(category, status_filter="", keyword="", limit=300):
    client, db = get_reuse_queue_db()
    try:
        collection_name = f"{category}_Reuse_Queue"
        if collection_name not in db.list_collection_names():
            return {"category": category, "rows": [], "collection_name": collection_name, "total": 0}

        collection = db[collection_name]
        query = {}
        if status_filter:
            query["Status"] = status_filter
        if keyword:
            query["URL"] = {"$regex": keyword, "$options": "i"}

        rows = []
        now = datetime.now()
        cursor = collection.find(query).sort("UpdatedAt", -1).limit(max(1, min(int(limit), 1000)))
        for doc in cursor:
            url = str(doc.get("URL") or "")
            doc["Domain"] = _extract_domain(url)
            doc["reusable_now"] = is_reuse_queue_reusable(doc, now)
            doc["CreatedAtText"] = reuse_datetime_text(doc.get("CreatedAt"))
            doc["FirstUsedAtText"] = reuse_datetime_text(doc.get("FirstUsedAt"))
            doc["LastUsedAtText"] = reuse_datetime_text(doc.get("LastUsedAt"))
            doc["NextReusableAtText"] = reuse_datetime_text(doc.get("NextReusableAt"))
            doc["UpdatedAtText"] = reuse_datetime_text(doc.get("UpdatedAt"))
            rows.append(doc)

        total = collection.count_documents(query)
        return {
            "category": category,
            "collection_name": collection_name,
            "rows": rows,
            "total": total,
        }
    finally:
        client.close()


def run_scraper_task(task, options):
    from data_scraper import run_scraper_job

    def progress(message):
        append_task_log(task, message)

    keywords = [part.strip() for part in (options.get("keywords") or "").replace(";", "\n").replace(",", "\n").splitlines() if part.strip()]
    if not keywords:
        raise RuntimeError("请至少填写一个关键词")

    result = run_scraper_job(
        keywords=keywords,
        max_results=int(options.get("max_results") or 100),
        min_product_count=int(options.get("min_product_count") or 200),
        api_mode=options.get("api_mode") or "1",
        api_key=(options.get("api_key") or "").strip() or None,
        bestproxy_auth=(options.get("bestproxy_auth") or "").strip() or None,
        save_mode=options.get("save_mode") or "excel",
        category=(options.get("category") or "default").strip() or "default",
        mongo_collection=(options.get("mongo_collection") or "").strip() or None,
        progress_callback=progress,
        stop_callback=lambda: is_task_stop_requested(task),
    )

    task["success_count"] = result.get("count", 0)
    if result.get("stopped"):
        append_task_log(task, "数据爬取已按停止请求提前结束")
    append_task_log(task, f"数据爬取完成，结果数: {result.get('count', 0)}")
    append_task_log(task, f"保存位置: {result.get('saved_to', '')}")


def run_url_import_task(task, options):
    from url_import_export import import_urls_from_txt_file

    file_path = options.get("file_path") or ""
    category = (options.get("category") or "").strip()
    if not file_path or not os.path.exists(file_path):
        raise RuntimeError("导入文件不存在")
    if not category:
        raise RuntimeError("分类名称不能为空")

    append_task_log(task, f"开始导入文件: {file_path}")
    append_task_log(task, f"目标分类: {category}")
    try:
        result = import_urls_from_txt_file(file_path, category)
        task["success_count"] = int(result.get("new_count", 0))
        append_task_log(task, f"读取 URL 总数: {result.get('total_urls', 0)}")
        append_task_log(task, f"新增 URL 数量: {result.get('new_count', 0)}")
        append_task_log(task, f"跳过已存在数量: {result.get('skipped_count', 0)}")
        append_task_log(task, f"目标集合: {result.get('collection_name', '')}")
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


def run_url_auto_classify_task(task, options):
    from url_auto_classifier import run_auto_classify_job

    source_collection = (options.get("source_collection") or "").strip()
    if not source_collection:
        raise RuntimeError("璇峰厛閫夋嫨 URL 鏉ユ簮闆嗗悎")

    append_task_log(task, f"URL 鑷姩鍒嗙被鏉ユ簮: {source_collection}")
    append_task_log(task, f"鏈€澶у鐞嗘暟: {options.get('limit') or '100'}")
    append_task_log(task, f"鏈€浣庡垎鏁伴槇鍊? {options.get('min_score') or '10'}")
    append_task_log(task, f"鏈€灏忓垎宸? {options.get('min_margin') or '3'}")

    result = run_auto_classify_job(
        source_collection=source_collection,
        limit=int(options.get("limit") or 100),
        min_score=int(options.get("min_score") or 10),
        min_margin=int(options.get("min_margin") or 3),
        delete_low_confidence=True,
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )

    task["success_count"] = int(result.get("classified", 0))
    task["failed_count"] = int(result.get("failed", 0))
    if result.get("stopped"):
        append_task_log(task, "URL 鑷姩鍒嗙被宸叉寜鍋滄璇锋眰鎻愬墠缁撴潫")
    append_task_log(task, f"澶勭悊鎬绘暟: {result.get('processed', 0)}")
    append_task_log(task, f"鎴愬姛绉诲姩: {result.get('classified', 0)}")
    append_task_log(task, f"鍒犻櫎鏁伴噺: {result.get('deleted', 0)}")
    append_task_log(task, f"鍏朵腑闈瀪hopify: {result.get('not_shopify', 0)}")
    append_task_log(task, f"鍏朵腑浣庣舰淇″害: {result.get('low_confidence', 0)}")


def run_url_auto_classify_task_clean(task, options):
    from url_auto_classifier import run_auto_classify_job

    source_collection = (options.get("source_collection") or "").strip()
    if not source_collection:
        raise RuntimeError("请选择来源 URL 集合")

    append_task_log(task, f"来源集合: {source_collection}")
    append_task_log(task, f"最大处理数: {options.get('limit') or '100'}")
    append_task_log(task, f"最低命中分: {options.get('min_score') or '12'}")
    append_task_log(task, f"最小领先分差: {options.get('min_margin') or '4'}")

    result = run_auto_classify_job(
        source_collection=source_collection,
        limit=int(options.get("limit") or 100),
        min_score=int(options.get("min_score") or 12),
        min_margin=int(options.get("min_margin") or 4),
        delete_low_confidence=True,
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )

    task["success_count"] = int(result.get("classified", 0))
    task["failed_count"] = int(result.get("failed", 0))
    if result.get("stopped"):
        append_task_log(task, "已按停止请求中断自动分类")
    append_task_log(task, f"已处理: {result.get('processed', 0)}")
    append_task_log(task, f"成功移动: {result.get('classified', 0)}")
    append_task_log(task, f"删除数量: {result.get('deleted', 0)}")
    append_task_log(task, f"非 Shopify: {result.get('not_shopify', 0)}")
    append_task_log(task, f"低置信度: {result.get('low_confidence', 0)}")
    append_task_log(task, f"阿拉伯文删除: {result.get('arabic_deleted', 0)}")
    append_task_log(task, f"打不开删除: {result.get('fetch_failed_deleted', 0)}")


def run_product_crawler_task(task, options):
    from product_data_crawler import run_product_crawler_job

    currency_config_path = (options.get("currency_config_path") or "").strip()
    proxies_file = (options.get("proxies_file") or "").strip()

    if not currency_config_path or not os.path.exists(currency_config_path):
        raise RuntimeError("汇率文件不存在，请先在项目根目录放置 currency_config.json")
    if not proxies_file or not os.path.exists(proxies_file):
        raise RuntimeError("代理文件不存在，请先在项目根目录放置 proxies.txt")

    append_task_log(task, f"商品爬取来源模式: {'Filtered' if str(options.get('task_source_mode')) == '1' else 'Unfiltered'}")
    append_task_log(task, f"汇率文件: {currency_config_path}")
    append_task_log(task, f"代理文件: {proxies_file}")

    result = run_product_crawler_job(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=int(options.get("task_source_mode") or 2),
        max_workers=int(options.get("max_workers") or 10),
        max_retry_per_site=int(options.get("max_retry_per_site") or 4),
        min_price=float(options.get("min_price") or 1),
        reuse_requeue_before_crawl=bool(options.get("reuse_requeue_before_crawl")),
        reuse_per_category_limit=int(options.get("reuse_per_category_limit") or 200),
        reuse_max_use_count=int(options.get("reuse_max_use_count") or 0),
        reuse_max_mode2_count=int(options.get("reuse_max_mode2_count") or 0),
        clear_source_after_crawl=bool(options.get("clear_source_after_crawl")),
        clear_only_consumed_urls=bool(options.get("clear_only_consumed_urls")),
        skip_already_crawled_source_url=bool(options.get("skip_already_crawled_source_url")),
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )
    task["success_count"] = int(result.get("success_sites", 0))
    task["failed_count"] = int(result.get("failed_sites", 0))
    append_task_log(task, f"复用池回投新增: {result.get('reuse_released', 0)}")
    append_task_log(task, f"复用池已在任务池: {result.get('reuse_already_in_queue', 0)}")
    append_task_log(task, f"Reuse_Queue 新增: {result.get('reuse_queue_inserted', 0)}")
    append_task_log(task, f"Reuse_Queue 已存在: {result.get('reuse_queue_existing', 0)}")
    append_task_log(task, f"新增商品数: {result.get('inserted_products', 0)}")
    if result.get("stopped"):
        append_task_log(task, "商品爬取已按停止请求安全退出")
    append_task_log(task, f"日志文件: {result.get('log_file', '')}")


def run_single_collection_crawler_task(task, options):
    from product_data_crawler import run_product_crawler_job

    currency_config_path = (options.get("currency_config_path") or "").strip()
    proxies_file = (options.get("proxies_file") or "").strip()

    if not currency_config_path or not os.path.exists(currency_config_path):
        raise RuntimeError("汇率文件不存在，请先在项目根目录放置 currency_config.json")
    if not proxies_file or not os.path.exists(proxies_file):
        raise RuntimeError("代理文件不存在，请先在项目根目录放置 proxies.txt")

    single_collection = (options.get("single_collection") or "").strip()
    append_task_log(task, f"单类目集合爬取: {single_collection}")
    append_task_log(task, f"汇率文件: {currency_config_path}")
    append_task_log(task, f"代理文件: {proxies_file}")

    result = run_product_crawler_job(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=int(options.get("task_source_mode") or 2),
        max_workers=int(options.get("max_workers") or 10),
        max_retry_per_site=int(options.get("max_retry_per_site") or 4),
        min_price=float(options.get("min_price") or 1),
        reuse_requeue_before_crawl=bool(options.get("reuse_requeue_before_crawl")),
        reuse_per_category_limit=int(options.get("reuse_per_category_limit") or 200),
        reuse_max_use_count=int(options.get("reuse_max_use_count") or 0),
        reuse_max_mode2_count=int(options.get("reuse_max_mode2_count") or 0),
        clear_source_after_crawl=bool(options.get("clear_source_after_crawl")),
        clear_only_consumed_urls=bool(options.get("clear_only_consumed_urls")),
        skip_already_crawled_source_url=bool(options.get("skip_already_crawled_source_url")),
        single_collection=single_collection,
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )
    task["success_count"] = int(result.get("success_sites", 0))
    task["failed_count"] = int(result.get("failed_sites", 0))
    append_task_log(task, f"复用池回投新增: {result.get('reuse_released', 0)}")
    append_task_log(task, f"复用池已在任务池: {result.get('reuse_already_in_queue', 0)}")
    append_task_log(task, f"Reuse_Queue 新增: {result.get('reuse_queue_inserted', 0)}")
    append_task_log(task, f"Reuse_Queue 已存在: {result.get('reuse_queue_existing', 0)}")
    append_task_log(task, f"新增商品数: {result.get('inserted_products', 0)}")
    if result.get("stopped"):
        append_task_log(task, "商品爬取已按停止请求安全退出")
    append_task_log(task, f"日志文件: {result.get('log_file', '')}")


def run_nav_category_crawler_task(task, options):
    from product_data_crawler import run_nav_category_crawler_job

    currency_config_path = (options.get("currency_config_path") or "").strip()
    proxies_file = (options.get("proxies_file") or "").strip()

    if not currency_config_path or not os.path.exists(currency_config_path):
        raise RuntimeError("汇率文件不存在，请先在项目根目录放置 currency_config.json")
    if not proxies_file or not os.path.exists(proxies_file):
        raise RuntimeError("代理文件不存在，请先在项目根目录放置 proxies.txt")

    append_task_log(task, "导航分类爬取模式: 解析店铺导航栏，按集合逐类爬取，使用导航两级分类")
    append_task_log(task, f"来源模式: {'Filtered' if str(options.get('task_source_mode')) == '1' else 'Unfiltered'}")
    append_task_log(task, f"汇率文件: {currency_config_path}")
    append_task_log(task, f"代理文件: {proxies_file}")

    result = run_nav_category_crawler_job(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=int(options.get("task_source_mode") or 2),
        max_workers=int(options.get("max_workers") or 10),
        max_retry_per_site=int(options.get("max_retry_per_site") or 4),
        min_price=float(options.get("min_price") or 1),
        reuse_requeue_before_crawl=bool(options.get("reuse_requeue_before_crawl")),
        reuse_per_category_limit=int(options.get("reuse_per_category_limit") or 200),
        reuse_max_use_count=int(options.get("reuse_max_use_count") or 0),
        reuse_max_mode2_count=int(options.get("reuse_max_mode2_count") or 0),
        clear_source_after_crawl=bool(options.get("clear_source_after_crawl")),
        clear_only_consumed_urls=bool(options.get("clear_only_consumed_urls")),
        skip_already_crawled_source_url=bool(options.get("skip_already_crawled_source_url")),
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )
    task["success_count"] = int(result.get("success_sites", 0))
    task["failed_count"] = int(result.get("failed_sites", 0))
    append_task_log(task, f"复用池回投新增: {result.get('reuse_released', 0)}")
    append_task_log(task, f"复用池已在任务池: {result.get('reuse_already_in_queue', 0)}")
    append_task_log(task, f"Reuse_Queue 新增: {result.get('reuse_queue_inserted', 0)}")
    append_task_log(task, f"Reuse_Queue 已存在: {result.get('reuse_queue_existing', 0)}")
    append_task_log(task, f"新增商品数: {result.get('inserted_products', 0)}")
    if result.get("stopped"):
        append_task_log(task, "导航分类爬取已按停止请求安全退出")
    append_task_log(task, f"日志文件: {result.get('log_file', '')}")


def run_reuse_queue_task(task, options):
    from product_data_crawler import run_reuse_queue_requeue_job

    result = run_reuse_queue_requeue_job(
        task_source_mode=int(options.get("task_source_mode") or 2),
        reuse_per_category_limit=int(options.get("reuse_per_category_limit") or 200),
        reuse_max_use_count=int(options.get("reuse_max_use_count") or 0),
        reuse_max_mode2_count=int(options.get("reuse_max_mode2_count") or 0),
        progress_callback=lambda message: append_task_log(task, message),
    )
    task["success_count"] = int(result.get("released", 0))
    append_task_log(task, f"本次回投批次: {result.get('batch_id', '')}")
    append_task_log(task, f"新投放到任务池: {result.get('released', 0)}")
    append_task_log(task, f"已在任务池中: {result.get('already_in_queue', 0)}")


def run_product_processing_task(task, options):
    from product_processing_v2 import (
        CATEGORY_SMALL_THRESHOLD,
        process_excel_category_merge,
        run_basic_cleanup,
        run_category_cleanup,
        run_clean_forbidden_cleanup,
        run_english_cleanup,
        run_extract_clean,
        run_forbidden_cleanup,
        run_image_cleanup,
    )

    action = (options.get("action") or "").strip()
    collection = (options.get("collection") or "__all__").strip()
    progress = lambda message: append_task_log(task, message)
    stop_callback = lambda: is_task_stop_requested(task)

    if action == "basic":
        result = run_basic_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted", 0))
        if result.get("stopped"):
            append_task_log(task, "基础数据清洗已按停止请求提前结束")
            return
        append_task_log(task, f"基础数据清洗完成，删除 {result.get('deleted', 0)} 条")
        return

    if action == "all":
        append_task_log(task, "开始提取清洗: 基础数据清洗 -> 异常图片处理 -> 英文数据过滤 -> 分类清洗 -> 写入 {类目}_clean")
        result = run_extract_clean(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        total_inserted = sum(r.get("inserted", 0) for r in result.get("collections", []))
        total_existing = sum(r.get("existing", 0) for r in result.get("collections", []))
        total_failed = sum(r.get("failed", 0) for r in result.get("collections", []))
        total_merged = sum(r.get("merged", 0) for r in result.get("collections", []))
        write_failed_count = sum(1 for r in result.get("collections", []) if r.get("write_failed"))
        task["success_count"] = total_inserted
        if result.get("stopped"):
            append_task_log(task, "提取清洗已按停止请求提前结束")
            return
        append_task_log(
            task,
            "提取清洗完成"
            f" | 写入 _clean {total_inserted}"
            f" | 已存在跳过 {total_existing}"
            f" | 未通过检查 {total_failed}"
            f" | 分类合并 {total_merged}"
            f" | 写入失败集合 {write_failed_count}"
        )
        return



    if action == "image":
        result = run_image_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted", 0))
        if result.get("stopped"):
            append_task_log(task, "异常图片清洗已按停止请求提前结束")
            return
        append_task_log(task, f"异常图片清洗完成，删除 {result.get('deleted', 0)} 条")
        return

    if action == "english":
        result = run_english_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted", 0))
        if result.get("stopped"):
            append_task_log(task, "英文数据过滤已按停止请求提前结束")
            return
        append_task_log(task, f"英文数据过滤完成，删除 {result.get('deleted', 0)} 条")
        return

    if action == "forbidden":
        result = run_forbidden_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("moved", 0))
        if result.get("stopped"):
            append_task_log(task, "违禁词过滤已按停止请求提前结束")
            return
        append_task_log(task, f"违禁词过滤完成，移入回收站 {result.get('moved', 0)} 条")
        return

    if action == "clean_forbidden":
        result = run_clean_forbidden_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted", 0))
        if result.get("stopped"):
            append_task_log(task, "clean 集合违禁词过滤已按停止请求提前结束")
            return
        append_task_log(
            task,
            f"clean 集合违禁词过滤完成，处理集合 {result.get('collections', 0)} 个，删除 {result.get('deleted', 0)} 条"
        )
        return

    if action == "excel_category_merge":
        file_path = (options.get("file_path") or "").strip()
        threshold = int(options.get("threshold", CATEGORY_SMALL_THRESHOLD) or CATEGORY_SMALL_THRESHOLD)
        append_task_log(task, f"开始处理 Excel 分类数据: {file_path}")
        try:
            result = process_excel_category_merge(
                file_path=file_path,
                threshold=threshold,
                progress_callback=progress,
                stop_callback=stop_callback,
            )
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    append_task_log(task, f"临时文件删除失败: {file_path}")
        task["success_count"] = int(result.get("updated", 0))
        if result.get("stopped"):
            append_task_log(task, "Excel 分类处理已按停止请求提前结束")
            return
        append_task_log(
            task,
            f"Excel 分类处理完成 | 更新分类 {result.get('updated', 0)}"
            f" | 小分类种类 {result.get('small_categories', 0)}"
        )
        if result.get("file_path"):
            append_task_log(task, f"导出文件: {result.get('file_path')}")
        return

    if action == "category":
        result = run_category_cleanup(selected_collection=collection, progress_callback=progress, stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted", 0)) + int(result.get("normalized", 0)) + int(result.get("merged", 0))
        if result.get("stopped"):
            append_task_log(task, "分类清洗已按停止请求提前结束")
            return
        append_task_log(
            task,
            f"分类清洗完成 | 中文分类删除 {result.get('deleted', 0)}"
            f" | 分类标准化 {result.get('normalized', 0)}"
            f" | 小分类合并 {result.get('merged', 0)}"
        )
        return

    if action == "mismatch":
        from product_mismatch_scanner import scan_collection_mismatches

        append_task_log(task, "开始执行快速分类扫描，仅导出快速判定不匹配结果，不修改数据库")
        result = scan_collection_mismatches(collection, logger=lambda message: append_task_log(task, message), stop_callback=stop_callback)
        task["success_count"] = int(result.get("mismatch_count", 0))
        if result.get("stopped"):
            append_task_log(task, "快速分类扫描已按停止请求提前结束")
            return
        append_task_log(
            task,
            f"快速分类扫描完成，扫描 {result.get('scanned_count', 0)} 条，"
            f"发现快速判定不匹配 {result.get('mismatch_count', 0)} 条",
        )
        if result.get("file_path"):
            append_task_log(task, f"错类扫描结果：{result.get('file_path')}")
        return

    if action == "mismatch_move":
        from product_mismatch_scanner import delete_collection_mismatches

        append_task_log(task, "开始执行快速分类直删，不通过快速分类的商品会先备份后删除")
        result = delete_collection_mismatches(collection, logger=lambda message: append_task_log(task, message), stop_callback=stop_callback)
        task["success_count"] = int(result.get("deleted_count", 0))
        if result.get("stopped"):
            append_task_log(task, "快速分类直删已按停止请求提前结束")
            return
        append_task_log(
            task,
            f"快速分类直删完成，扫描 {result.get('scanned_count', 0)} 条，"
            f"发现快速判定不匹配 {result.get('mismatch_count', 0)} 条，"
            f"实际删除 {result.get('deleted_count', 0)} 条",
        )
        if result.get("file_path"):
            append_task_log(task, f"删除前快照：{result.get('file_path')}")
        return

    if action == "domain_match":
        from product_processing_v2 import run_domain_category_match

        domain = (options.get("domain") or "").strip()
        category = (options.get("category") or "").strip()

        append_task_log(task, f"开始域名分类匹配: 域名={domain}, 分类={category}, 集合={collection}")

        result = run_domain_category_match(
            domain=domain,
            category_str=category,
            selected_collection=collection,
            progress_callback=lambda message: append_task_log(task, message),
            stop_callback=stop_callback,
        )
        task["success_count"] = int(result.get("total", 0))
        if result.get("stopped"):
            append_task_log(task, "域名分类匹配已按停止请求提前结束")
            return
        append_task_log(task, f"域名分类匹配完成，共匹配 {result.get('total', 0)} 条")
        for cat, count in sorted(result.get("category_counts", {}).items(), key=lambda x: -x[1]):
            append_task_log(task, f"  {cat}: {count}条")
            if count < 10:
                append_task_log(task, f"  ⚠ {cat}: 数量少于10条 ({count})")
        if result.get("file_path"):
            append_task_log(task, f"导出文件: {result.get('file_path')}")
        return

    raise RuntimeError("未知的商品处理动作")


def run_structured_crawl_task(task, options):
    from structured_site_crawler import run_structured_crawl

    domain = (options.get("domain") or "").strip()
    category_text = (options.get("category_text") or "").strip()
    api_mode = (options.get("api_mode") or "5").strip()

    append_task_log(task, f"开始结构网站数据获取: 域名={domain}, API模式={api_mode}")

    result = run_structured_crawl(
        domain=domain,
        category_text=category_text,
        api_mode=api_mode,
        progress_callback=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )
    task["success_count"] = int(result.get("total", 0))
    if result.get("stopped"):
        append_task_log(task, "结构网站数据获取已按停止请求提前结束")
        return
    append_task_log(task, f"结构网站数据获取完成，共匹配 {result.get('total', 0)} 条")
    if result.get("file_path"):
        append_task_log(task, f"导出文件: {result.get('file_path')}")


def run_product_export_task(task, options):
    from product_exporter import export_collection_by_category_rules

    collection_name = (options.get("collection_name") or "").strip()
    total_limit = int(options.get("total_limit") or 0)
    min_per_category = int(options.get("min_per_category") or 0)
    max_per_category = int(options.get("max_per_category") or 0)

    append_task_log(
        task,
        f"开始导出集合 {collection_name}，总数上限 {total_limit}，"
        f"小分类最小值 {min_per_category}，小分类最大值 {max_per_category}。",
    )

    result = export_collection_by_category_rules(
        collection_name=collection_name,
        total_limit=total_limit,
        min_per_category=min_per_category,
        max_per_category=max_per_category,
        logger=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )

    task["success_count"] = int(result.get("exported_count", 0))
    if result.get("stopped"):
        append_task_log(task, "分类导出已按停止请求提前结束")
        return
    append_task_log(
        task,
        f"导出完成：选中 {result.get('selected_category_count', 0)} 个小分类，"
        f"导出 {result.get('exported_count', 0)} 条，删除 {result.get('deleted_count', 0)} 条。",
    )
    append_task_log(task, f"导出文件：{result.get('file_path', '')}")


def run_direct_product_export_task(task, options):
    from product_exporter import export_clean_collection_direct

    collection_name = (options.get("collection_name") or "").strip()
    limit = int(options.get("limit") or 0)

    append_task_log(
        task,
        f"开始数据库直接导出商品: shopify_data_new.{collection_name}，"
        f"导出数量 {'全部' if limit <= 0 else limit}。"
    )

    result = export_clean_collection_direct(
        collection_name=collection_name,
        limit=limit,
        logger=lambda message: append_task_log(task, message),
        stop_callback=lambda: is_task_stop_requested(task),
    )

    task["success_count"] = int(result.get("exported_count", 0))
    if result.get("stopped"):
        append_task_log(task, "数据库直接导出已按停止请求提前结束")
        return
    append_task_log(
        task,
        f"数据库直接导出完成：导出 {result.get('exported_count', 0)} 条，"
        f"删除 {result.get('deleted_count', 0)} 条。",
    )
    append_task_log(task, f"导出文件：{result.get('file_path', '')}")


def run_report_task(task):
    settings = get_settings_snapshot()
    username = ensure_required_setting(settings, "report_username", "上报账号")
    password = ensure_required_setting(settings, "report_password", "上报密码")
    reporter = DomainReporter("http://123.60.135.93:8099", username, password)
    store = get_store()
    try:
        cur = store._db.execute("SELECT MAX(domain_number) FROM sites WHERE report_status = '已报'")
        max_number = cur.fetchone()[0] or 0
        try:
            current_number = int(max_number) + 1
        except Exception:
            current_number = 1

        for row in get_rows_for_ids(store, task["site_ids"]):
            domain = (row["domain"] or "").strip()
            if not domain:
                task["failed_count"] += 1
                task_site_log(task, "未知域名", "缺少域名，已跳过")
                continue

            missing = []
            for field in ("server", "template", "category"):
                if not (row[field] or "").strip():
                    missing.append(field)
            if missing:
                task["failed_count"] += 1
                task_site_log(task, domain, f"缺少必要字段: {', '.join(missing)}")
                continue

            category_id = CATEGORY_ID_MAP.get((row["category"] or "").strip())
            if not category_id:
                task["failed_count"] += 1
                task_site_log(task, domain, f"大类无效: {(row['category'] or '').strip()}")
                continue

            payload = {
                "name": domain,
                "serverip": (row["server"] or "").strip(),
                "template": (row["template"] or "").strip(),
                "category": category_id,
                "categoryTag": None,
                "language": None,
            }
            task_site_log(task, domain, "开始上报域名")
            try:
                reporter.submit_domain(payload)
                current_time = now_str()
                update_values = {
                    DOMAIN_NUMBER_COL: str(current_number),
                    REPORT_STATUS_COL: "已报",
                    REPORT_TIME_COL: current_time,
                    SCHEDULE_ENABLED_COL: "0",
                }
                current_number += 1
                try:
                    info = reporter.fetch_domain_info(domain)
                    status_val = info.get("status")
                    update_values["report_id"] = str(info.get("id") or "")
                    update_values["domain_status"] = str(status_val) if status_val is not None else ""
                    if status_val in {3, "3"}:
                        update_values[DOMAIN_RESOLVED_TIME_COL] = current_time
                    if status_val in {4, "4"}:
                        update_values.update(build_uploaded_sync_updates(row, current_time))
                except Exception as exc:
                    task_site_log(task, domain, f"获取上报详情失败，已跳过详情更新: {exc}")
                    update_values["report_id"] = ""
                    update_values["domain_status"] = ""

                store.update_fields(row["id"], update_values)
                task["success_count"] += 1
                task_site_log(task, domain, "上报成功")
            except Exception as exc:
                task["failed_count"] += 1
                task["errors"].append(f"{domain}: {exc}")
                task_site_log(task, domain, f"上报失败: {exc}")
    finally:
        store.close()


def run_refresh_reported_task(task):
    settings = get_settings_snapshot()
    username = ensure_required_setting(settings, "report_username", "上报账号")
    password = ensure_required_setting(settings, "report_password", "上报密码")
    reporter = DomainReporter("http://123.60.135.93:8099", username, password)
    store = get_store()
    try:
        rows = get_rows_for_ids(store, task["site_ids"]) if task["site_ids"] else store.query_rows("")
        rows = [row for row in rows if (row[REPORT_STATUS_COL] or "") == "已报"]
        for row in rows:
            domain = (row["domain"] or "").strip()
            if not domain:
                continue
            try:
                info = reporter.fetch_domain_info(domain)
            except Exception as exc:
                task["failed_count"] += 1
                task_site_log(task, domain, f"刷新失败: {exc}")
                continue

            current_time = now_str()
            status_val = info.get("status")
            current_status = row["domain_status"] if "domain_status" in row.keys() else ""
            update_values = {
                "report_id": str(info.get("id") or ""),
                "domain_status": str(status_val) if status_val is not None else "",
            }
            if current_status not in {"3", 3} and status_val in {"3", 3}:
                update_values[DOMAIN_RESOLVED_TIME_COL] = current_time
            if status_val in {"4", 4}:
                update_values.update(build_uploaded_sync_updates(row, current_time))
            store.update_fields(row["id"], update_values)
            task["success_count"] += 1
            task_site_log(task, domain, f"刷新完成，状态: {DOMAIN_STATUS_LABELS.get(status_val, '其他')}")
    finally:
        store.close()


def run_delete_reported_task(task):
    settings = get_settings_snapshot()
    username = ensure_required_setting(settings, "report_username", "上报账号")
    password = ensure_required_setting(settings, "report_password", "上报密码")
    reporter = DomainReporter("http://123.60.135.93:8099", username, password)
    store = get_store()
    try:
        for row in get_rows_for_ids(store, task["site_ids"]):
            domain = (row["domain"] or "").strip()
            report_id = (row["report_id"] or "").strip()
            if not report_id:
                task["failed_count"] += 1
                task_site_log(task, domain or "未知域名", "缺少 report_id，无法删除上报")
                continue
            try:
                reporter.delete_domain(report_id)
                store.update_fields(
                    row["id"],
                    {
                        REPORT_STATUS_COL: "未报",
                        "report_id": "",
                        "domain_status": "",
                        REPORT_TIME_COL: "",
                        DOMAIN_RESOLVED_TIME_COL: "",
                        BUILD_STATUS_COL: "",
                        BUILD_TIME_COL: "",
                    },
                )
                task["success_count"] += 1
                task_site_log(task, domain, "已删除上报并重置状态")
            except Exception as exc:
                task["failed_count"] += 1
                task_site_log(task, domain, f"删除上报失败: {exc}")
    finally:
        store.close()


def run_build_task(task):
    settings = get_settings_snapshot()
    username = ensure_required_setting(settings, "erp_username", "ERP 账号")
    password = ensure_required_setting(settings, "erp_password", "ERP 密码")
    media_root = settings["media_root"]
    builder = ERPBuilder(username, password, image_root=media_root)
    builder.login()

    store = get_store()
    try:
        for row in get_rows_for_ids(store, task["site_ids"]):
            domain = (row["domain"] or "").strip()
            if not domain:
                task["failed_count"] += 1
                task_site_log(task, "未知域名", "缺少域名，已跳过")
                continue

            domain_folder = os.path.join(media_root, domain)
            if not os.path.isdir(domain_folder):
                task["failed_count"] += 1
                task_site_log(task, domain, f"资源目录不存在: {domain_folder}")
                continue

            missing = []
            for field in ("server", "template", "title", "description", "address", "category"):
                if not (row[field] or "").strip():
                    missing.append(field)
            if missing:
                task["failed_count"] += 1
                task_site_log(task, domain, f"缺少建站字段: {', '.join(missing)}")
                continue

            try:
                ok, body = builder.build_site(
                    domain,
                    (row["server"] or "").strip(),
                    (row["template"] or "").strip(),
                    (row["title"] or "").strip(),
                    (row["description"] or "").strip(),
                    (row["address"] or "").strip(),
                    (row["category"] or "").strip(),
                )
                if ok:
                    store.update_fields(
                        row["id"],
                        {
                            BUILD_STATUS_COL: "已建站",
                            BUILD_TIME_COL: now_str(),
                            SCHEDULE_ENABLED_COL: "0",
                        },
                    )
                    task["success_count"] += 1
                    task_site_log(task, domain, "建站成功")
                else:
                    task["failed_count"] += 1
                    task_site_log(task, domain, f"建站失败: {body}")
            except Exception as exc:
                task["failed_count"] += 1
                task_site_log(task, domain, f"建站失败: {exc}")
    finally:
        store.close()


def run_health_task(task):
    store = get_store()
    try:
        for row in get_rows_for_ids(store, task["site_ids"]):
            domain = row_text(row, "domain") or "未知域名"
            task_site_log(task, domain, "开始健康检查")
            try:
                ok, code, details = healthcheck_domain(row_text(row, "domain"))
                status_text = "正常" if ok else f"异常:{code}"
                store.update_fields(
                    row["id"],
                    {
                        HEALTH_STATUS_COL: status_text,
                        HEALTH_TIME_COL: now_str(),
                    },
                )
                task["success_count"] += 1
                parts = [f"检查完成: {status_text}"]
                if details.get("has_redirect"):
                    parts.append(f"跳转至 {details['final_domain']}")
                if details.get("page_title"):
                    parts.append(f"标题: {details['page_title'][:60]}")
                if not ok and details.get("final_url"):
                    parts.append(f"最终URL: {details['final_url']}")

                login_path = row_text(row, "login_path")
                if login_path:
                    login_ok, login_code, login_details = healthcheck_domain(
                        row_text(row, "domain"), check_path=login_path
                    )
                    if not login_ok:
                        parts.append(f"登录异常:{login_code}")
                        if login_details.get("final_url"):
                            parts.append(f"登录跳转至 {login_details['final_url']}")
                        if status_text == "正常":
                            status_text = f"正常(登录异常:{login_code})"
                            store.update_fields(
                                row["id"],
                                {HEALTH_STATUS_COL: status_text},
                            )

                task_site_log(task, domain, "，".join(parts))
            except Exception as exc:
                task["failed_count"] += 1
                task["errors"].append(f"{domain}: {exc}")
                task_site_log(task, domain, f"健康检查失败: {exc}")
    finally:
        store.close()


def run_upload_main_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")
    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        source_id = row_text(row, "main_data_source_id")
        if not domain or not source_id:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名或主数据源ID")
            return

        try:
            uploader = MainDataUploader(wp_password)
            result = uploader.upload_main_data(
                row_text(row, "domain"),
                source_id,
                "0",
                progress_callback=task_progress_callback(task, domain),
            )
            result = result or {}
            success_count = int(result.get("upload_success", 0) or 0)
            repeat_count = int(result.get("repeat_count", 0) or 0)
            completed = bool(result.get("completed"))
            if success_count > 0 or repeat_count > 0:
                update_site_fields(
                    row["id"],
                    {
                        MAIN_DATA_STATUS_COL: "已上传",
                        MAIN_DATA_TIME_COL: now_str(),
                    },
                )
                increment_task_counts(task, success=1)
                task_site_log(task, domain, f"主数据上传完成 新增{success_count}条 重复{repeat_count}条")
            elif completed:
                increment_task_counts(task, success=1)
                task_site_log(task, domain, f"警告：上传完成但 0 条写入，数据源 (lv={source_id}) 中可能没有关联商品")
            else:
                increment_task_counts(task, failed=1)
                task_site_log(task, domain, "主数据上传失败")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"主数据上传失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label="主数据上传")


def run_upload_extra_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 瀵嗙爜")
    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        source_id = row_text(row, "extra_data_source_id")
        if not domain or not source_id:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名或补充数据源ID")
            return

        try:
            from extra_data_uploader import ExtraDataUploader

            uploader = ExtraDataUploader(wp_password)
            result = uploader.upload_extra_data(
                row_text(row, "domain"),
                source_id,
                "0",
                progress_callback=task_progress_callback(task, domain),
            )
            result = result or {}
            success_count = int(result.get("upload_success", 0) or 0)
            repeat_count = int(result.get("repeat_count", 0) or 0)
            completed = bool(result.get("completed"))
            if completed or success_count > 0 or repeat_count > 0:
                update_site_fields(
                    row["id"],
                    {
                        EXTRA_DATA_STATUS_COL: "已上传",
                        EXTRA_DATA_TIME_COL: now_str(),
                    },
                )
                increment_task_counts(task, success=1)
                task_site_log(task, domain, f"补充数据上传完成，新增{success_count}条，重复{repeat_count}条")
            else:
                increment_task_counts(task, failed=1)
                task_site_log(task, domain, "补充数据上传失败")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"补充数据上传失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label="补充数据上传")
    return

    from extra_data_uploader import ExtraDataUploader

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        source_id = row_text(row, "extra_data_source_id")
        if not domain or not source_id:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名或补充数据源ID")
            return

        try:
            result = batchdealsite(
                row_text(row, "domain"),
                source_id,
                "0",
                0,
                progress_callback=task_progress_callback(task, domain),
            )
            if result == 1:
                update_site_fields(
                    row["id"],
                    {
                        EXTRA_DATA_STATUS_COL: "已上传",
                        EXTRA_DATA_TIME_COL: now_str(),
                    },
                )
                increment_task_counts(task, success=1)
                task_site_log(task, domain, "补充数据上传成功")
            else:
                increment_task_counts(task, failed=1)
                task_site_log(task, domain, "补充数据上传失败")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"补充数据上传失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label="补充数据上传")


def run_upload_main_category_task(task):
    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        main_category = row_text(row, "main_category")
        if not domain or not main_category:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名或主分类")
            return

        try:
            uploader = MainCategoryUploader(row_text(row, "domain"))
            result = uploader.upload_main_category(
                row_text(row, "domain"),
                main_category,
                task_progress_callback(task, domain),
            )
            if result == 1:
                update_site_fields(
                    row["id"],
                    {
                        MAIN_CATEGORY_STATUS_COL: "已上传",
                        MAIN_CATEGORY_TIME_COL: now_str(),
                    },
                )
                increment_task_counts(task, success=1)
                task_site_log(task, domain, "主分类设置成功")
            else:
                increment_task_counts(task, failed=1)
                task_site_log(task, domain, "主分类设置失败")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"主分类设置失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label="主分类上传")


def run_configure_sites_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")
    media_root = settings["media_root"]

    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        if not domain:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名")
            return

        try:
            task_site_log(task, domain, "开始执行媒体配置")
            media_configurator = WPMediaConfigurator(wp_password, media_root=media_root)
            media_configurator.configure(domain)
            update_site_fields(
                row["id"],
                {
                    MEDIA_STATUS_COL: "已配置",
                    MEDIA_TIME_COL: now_str(),
                },
            )
            task_site_log(task, domain, "媒体配置完成")

            if is_task_stop_requested(task):
                return

            task_site_log(task, domain, "开始执行 Yoast / WP Rocket 按钮配置")
            button_clicker = WpPluginButtonClicker(wp_password, headless=True)
            button_clicker.configure(domain)
            update_site_fields(
                row["id"],
                {
                    PLUGIN_STATUS_COL: "已配置",
                    PLUGIN_TIME_COL: now_str(),
                },
            )
            task_site_log(task, domain, "开始清理 WP Rocket 缓存")
            cache_purger = WpCachePurger(wp_password)
            cache_purger.purge(domain)
            task_site_log(task, domain, "缓存清理完成")
            increment_task_counts(task, success=1)
            task_site_log(task, domain, "站点配置完成")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"站点配置失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=1, label="站点配置")


def run_configure_menu_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")

    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    from wp_menu_config import WpMenuConfigurator

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        if not domain:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名")
            return

        try:
            task_site_log(task, domain, "开始菜单配置")
            configurator = WpMenuConfigurator(wp_password)
            configurator.configure(domain)
            update_site_fields(
                row["id"],
                {
                    AUTO_CATEGORY_STATUS_COL: "已配置",
                    AUTO_CATEGORY_TIME_COL: now_str(),
                },
            )
            increment_task_counts(task, success=1)
            task_site_log(task, domain, "菜单配置完成")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"菜单配置失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=1, label="菜单配置")


def run_clear_cache_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")

    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        if not domain:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名")
            return

        try:
            task_site_log(task, domain, "开始清理 WP Rocket 缓存")
            purger = WpCachePurger(wp_password)
            purger.purge(domain)
            increment_task_counts(task, success=1)
            task_site_log(task, domain, "缓存清理完成")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"缓存清理失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=UPLOAD_MAX_WORKERS, label="批量清理缓存")


def run_check_orders_task_v2(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")

    store = get_store()
    try:
        rows = [row_to_dict(row) for row in get_rows_for_ids(store, task["site_ids"])]
    finally:
        store.close()

    task["checked_sites"] = 0
    task["sites_with_orders"] = 0
    task["sites_without_orders"] = 0
    task["order_details"] = []
    order_month = task.get("order_month", "").strip()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row_text(row, "domain") or "未知域名"
        if not domain:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名")
            return

        try:
            checker = WooOrderChecker(wp_password)
            result = checker.check_orders(domain, month=order_month)
            with TASK_LOCK:
                task["checked_sites"] = int(task.get("checked_sites", 0)) + 1

            total_orders = int(result.get("total_orders", 0) or 0)
            real_orders = int(result.get("real_orders", 0) or 0)
            status_counts = result.get("status_counts") or {}
            status_text = "，".join(
                f"{ORDER_STATUS_LABELS.get(key, key)}:{int(status_counts.get(key, 0) or 0)}"
                for key in sorted(status_counts, key=lambda k: -int(status_counts.get(k, 0) or 0))
                if int(status_counts.get(key, 0) or 0) > 0
            ) or "无订单"

            if result.get("has_orders"):
                is_real = result.get("has_real_orders", False)
                with TASK_LOCK:
                    task["sites_with_orders"] = int(task.get("sites_with_orders", 0)) + 1
                    task["order_details"].append({
                        "domain": domain,
                        "total": total_orders,
                        "real": real_orders,
                        "status_counts": status_counts,
                        "monthly_breakdown": result.get("monthly_breakdown", {}),
                    })
                increment_task_counts(task, success=1)
                log_msg = f"检测到订单，总数 {total_orders}"
                if not is_real:
                    log_msg += " (仅测试订单)"
                monthly = result.get("monthly_breakdown", {})
                if monthly:
                    parts_m = []
                    for mk, mv in sorted(monthly.items()):
                        pm = f"{mk}:{mv['count']}笔"
                        if mv['valid_count'] > 0:
                            pm += f"(有效{mv['valid_count']}笔/${mv['valid_total']})"
                        parts_m.append(pm)
                    log_msg += " | 月度: " + "，".join(parts_m)
                log_msg += f"，状态统计：{status_text}"
                task_site_log(task, domain, log_msg)
                for item in (result.get("recent_orders") or [])[:5]:
                    order_id = item.get("order_id") or "-"
                    order_status = item.get("status_label") or item.get("status") or "未知"
                    order_date = item.get("date") or ""
                    order_total = item.get("total") or ""
                    parts = [f"订单 #{order_id}", f"状态 {order_status}"]
                    if order_date:
                        parts.append(f"日期 {order_date}")
                    if order_total:
                        parts.append(f"金额 {order_total}")
                    task_site_log(task, domain, " | ".join(parts))
            else:
                with TASK_LOCK:
                    task["sites_without_orders"] = int(task.get("sites_without_orders", 0)) + 1
                increment_task_counts(task, success=1)
                task_site_log(task, domain, "当前没有检测到订单")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"订单检查失败: {exc}")

    run_parallel_site_jobs(task, rows, worker, max_workers=30, label="批量检查订单")

    details = task.get("order_details") or []
    total_orders_all = sum(d.get("total", 0) for d in details)
    real_orders_all = sum(d.get("real", 0) for d in details)
    merged_status = {}
    for d in details:
        for k, v in (d.get("status_counts") or {}).items():
            if k != "all":
                merged_status[k] = merged_status.get(k, 0) + int(v or 0)
    status_summary = "，".join(
        f"{ORDER_STATUS_LABELS.get(key, key)}:{merged_status[key]}"
        for key in sorted(merged_status, key=lambda k: -merged_status[k])
        if merged_status[key] > 0
    )
    top_sites = sorted(details, key=lambda d: -d["total"])[:10]
    real_sites = [d for d in details if d.get("real", 0) > 0]
    test_only_sites = [d for d in details if d.get("real", 0) == 0]
    all_monthly = defaultdict(lambda: {"count": 0, "valid_count": 0, "valid_total": 0.0})
    for d in details:
        for mk, mv in (d.get("monthly_breakdown") or {}).items():
            all_monthly[mk]["count"] += mv["count"]
            all_monthly[mk]["valid_count"] += mv["valid_count"]
            all_monthly[mk]["valid_total"] += mv["valid_total"]
    monthly_parts = []
    for mk in sorted(all_monthly):
        mv = all_monthly[mk]
        pm = f"{mk}:{mv['count']}笔"
        if mv['valid_count'] > 0:
            pm += f"(有效{mv['valid_count']}笔/${round(mv['valid_total'], 2)})"
        monthly_parts.append(pm)
    summary_parts = [
        f"订单检查汇总：已检查 {task.get('checked_sites', 0)} 个站点",
        f"有订单 {task.get('sites_with_orders', 0)} 个",
        f"无订单 {task.get('sites_without_orders', 0)} 个",
        f"失败 {task.get('failed_count', 0)} 个",
        f"全部订单共 {total_orders_all} 笔",
    ]
    if real_orders_all > 0:
        summary_parts.append(f"有效订单 {real_orders_all} 笔 ({len(real_sites)} 个站)")
    if test_only_sites:
        summary_parts.append(f"仅测试订单 {len(test_only_sites)} 个站")
    if status_summary:
        summary_parts.append(f"状态分布：{status_summary}")
    if real_sites:
        site_list = "，".join(f"{s['domain']}({s['real']}笔)" for s in sorted(real_sites, key=lambda d: -d['real'])[:10])
        summary_parts.append(f"有效订单站点 Top10：{site_list}")
    if monthly_parts:
        summary_parts.append(f"月度分布：{'，'.join(monthly_parts)}")
    append_task_log(task, " | ".join(summary_parts))


def run_check_orders_domains_task(task):
    settings = get_settings_snapshot()
    wp_password = ensure_required_setting(settings, "wp_password", "WordPress 密码")
    domains = list(task.get("order_domains") or task.get("domains") or [])

    task["checked_sites"] = 0
    task["sites_with_orders"] = 0
    task["sites_without_orders"] = 0
    task["order_details"] = []
    order_month = task.get("order_month", "").strip()

    def worker(row):
        if is_task_stop_requested(task):
            return

        domain = row.get("domain") or "未知域名"
        if not domain:
            increment_task_counts(task, failed=1)
            task_site_log(task, domain, "缺少域名")
            return

        try:
            checker = WooOrderChecker(wp_password)
            result = checker.check_orders(domain, month=order_month)
            with TASK_LOCK:
                task["checked_sites"] = int(task.get("checked_sites", 0)) + 1

            total_orders = int(result.get("total_orders", 0) or 0)
            real_orders = int(result.get("real_orders", 0) or 0)
            status_counts = result.get("status_counts") or {}
            status_text = "，".join(
                f"{ORDER_STATUS_LABELS.get(key, key)}:{int(status_counts.get(key, 0) or 0)}"
                for key in sorted(status_counts, key=lambda k: -int(status_counts.get(k, 0) or 0))
                if int(status_counts.get(key, 0) or 0) > 0
            ) or "无订单"

            if result.get("has_orders"):
                is_real = result.get("has_real_orders", False)
                with TASK_LOCK:
                    task["sites_with_orders"] = int(task.get("sites_with_orders", 0)) + 1
                    task["order_details"].append({
                        "domain": domain,
                        "total": total_orders,
                        "real": real_orders,
                        "status_counts": status_counts,
                        "monthly_breakdown": result.get("monthly_breakdown", {}),
                    })
                increment_task_counts(task, success=1)
                log_msg = f"检测到订单，总数 {total_orders}"
                if not is_real:
                    log_msg += " (仅测试订单)"
                monthly = result.get("monthly_breakdown", {})
                if monthly:
                    parts_m = []
                    for mk, mv in sorted(monthly.items()):
                        pm = f"{mk}:{mv['count']}笔"
                        if mv['valid_count'] > 0:
                            pm += f"(有效{mv['valid_count']}笔/${mv['valid_total']})"
                        parts_m.append(pm)
                    log_msg += " | 月度: " + "，".join(parts_m)
                log_msg += f"，状态统计：{status_text}"
                task_site_log(task, domain, log_msg)
                for item in (result.get("recent_orders") or [])[:5]:
                    order_id = item.get("order_id") or "-"
                    order_status = item.get("status_label") or item.get("status") or "未知"
                    order_date = item.get("date") or ""
                    order_total = item.get("total") or ""
                    parts = [f"订单 #{order_id}", f"状态 {order_status}"]
                    if order_date:
                        parts.append(f"日期 {order_date}")
                    if order_total:
                        parts.append(f"金额 {order_total}")
                    task_site_log(task, domain, " | ".join(parts))
            else:
                with TASK_LOCK:
                    task["sites_without_orders"] = int(task.get("sites_without_orders", 0)) + 1
                increment_task_counts(task, success=1)
                task_site_log(task, domain, "当前没有检测到订单")
        except Exception as exc:
            increment_task_counts(task, failed=1, error=f"{domain}: {exc}")
            task_site_log(task, domain, f"订单检查失败: {exc}")

    rows = [{"domain": domain} for domain in domains]
    run_parallel_site_jobs(task, rows, worker, max_workers=30, label="批量检查订单")

    details = task.get("order_details") or []
    total_orders_all = sum(d.get("total", 0) for d in details)
    real_orders_all = sum(d.get("real", 0) for d in details)
    merged_status = {}
    for d in details:
        for k, v in (d.get("status_counts") or {}).items():
            if k != "all":
                merged_status[k] = merged_status.get(k, 0) + int(v or 0)
    status_summary = "，".join(
        f"{ORDER_STATUS_LABELS.get(key, key)}:{merged_status[key]}"
        for key in sorted(merged_status, key=lambda k: -merged_status[k])
        if merged_status[key] > 0
    )
    top_sites = sorted(details, key=lambda d: -d["total"])[:10]
    real_sites = [d for d in details if d.get("real", 0) > 0]
    test_only_sites = [d for d in details if d.get("real", 0) == 0]
    all_monthly = defaultdict(lambda: {"count": 0, "valid_count": 0, "valid_total": 0.0})
    for d in details:
        for mk, mv in (d.get("monthly_breakdown") or {}).items():
            all_monthly[mk]["count"] += mv["count"]
            all_monthly[mk]["valid_count"] += mv["valid_count"]
            all_monthly[mk]["valid_total"] += mv["valid_total"]
    monthly_parts = []
    for mk in sorted(all_monthly):
        mv = all_monthly[mk]
        pm = f"{mk}:{mv['count']}笔"
        if mv['valid_count'] > 0:
            pm += f"(有效{mv['valid_count']}笔/${round(mv['valid_total'], 2)})"
        monthly_parts.append(pm)
    summary_parts = [
        f"订单检查汇总：已检查 {task.get('checked_sites', 0)} 个域名",
        f"有订单 {task.get('sites_with_orders', 0)} 个",
        f"无订单 {task.get('sites_without_orders', 0)} 个",
        f"失败 {task.get('failed_count', 0)} 个",
        f"全部订单共 {total_orders_all} 笔",
    ]
    if real_orders_all > 0:
        summary_parts.append(f"有效订单 {real_orders_all} 笔 ({len(real_sites)} 个站)")
    if test_only_sites:
        summary_parts.append(f"仅测试订单 {len(test_only_sites)} 个站")
    if status_summary:
        summary_parts.append(f"状态分布：{status_summary}")
    if real_sites:
        site_list = "，".join(f"{s['domain']}({s['real']}笔)" for s in sorted(real_sites, key=lambda d: -d['real'])[:10])
        summary_parts.append(f"有效订单站点 Top10：{site_list}")
    if monthly_parts:
        summary_parts.append(f"月度分布：{'，'.join(monthly_parts)}")
    append_task_log(task, " | ".join(summary_parts))


@app.route("/tasks/check-orders-selected", methods=["POST"])
def task_check_orders_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要检查订单的站点", "error")
        return redirect(url_for("built_list"))
    month = request.form.get("order_month", "").strip()
    task = create_task("批量检查订单", "check_orders", selected_ids)
    task["order_month"] = month
    if month:
        task["title"] = f"批量检查订单 ({month})"
    run_background_task(task, run_check_orders_task_v2)
    return redirect_to_task(task["id"], "订单检查任务已启动")


@app.route("/tasks/check-orders-domains", methods=["POST"])
def task_check_orders_domains():
    domains = parse_domains_text(request.form.get("domains_text", ""))
    if not domains:
        flash("请先粘贴要检查的域名", "error")
        return redirect(url_for("built_list"))
    month = request.form.get("order_month", "").strip()

    task = create_task("粘贴域名检查订单", "check_orders_domains", [])
    task["domains"] = domains
    task["site_count"] = len(domains)
    task["order_month"] = month
    if month:
        task["title"] = f"粘贴域名检查订单 ({month})"
    run_background_task(task, run_check_orders_domains_task)
    return redirect_to_task(task["id"], f"已启动 {len(domains)} 个域名的订单检查任务")


start_auto_schedule_worker()

# === 订单分析域名列表存储 ===

ORDER_LISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "order_lists.json")
ORDER_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "order_history.json")


def _load_order_lists():
    if not os.path.isfile(ORDER_LISTS_FILE):
        return {}
    try:
        with open(ORDER_LISTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_order_lists(data):
    with open(ORDER_LISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_order_history():
    if not os.path.isfile(ORDER_HISTORY_FILE):
        return []
    try:
        with open(ORDER_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_order_history(data):
    with open(ORDER_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_domain_text(text):
    parts = re.split(r"[\s,;\n\r]+", str(text or "").strip())
    return [p.strip().lower() for p in parts if p.strip()]


# === 页面路由 ===

@app.route("/")
@app.route("/sites")
def sites_list():
    q = request.args.get("q", "").strip()
    store = get_store()
    try:
        if q:
            rows = store.query_rows(q)
        else:
            rows = store.query_rows("")
    finally:
        store.close()
    rows = [row for row in rows if not is_reported(row)]
    rows = filter_rows_by_domain(rows, q)
    return render_template("sites.html", sites=rows, q=q, format_schedule_text=format_schedule_text)


@app.route("/reported")
def reported_list():
    q = request.args.get("q", "").strip()
    rows = [row for row in list_all_sites() if is_reported(row)]
    rows = sort_rows_by_time_desc(rows, "report_time")
    return render_template("reported.html", reported_sites=rows, q=q, 
        domain_status_label=domain_status_label, format_schedule_text=format_schedule_text)


@app.route("/scheduled")
def scheduled_list():
    q = request.args.get("q", "").strip()
    rows = [row for row in list_all_sites() if is_schedule_enabled(row) and not is_reported(row)]
    rows = filter_rows_by_domain(rows, q)
    return render_template("scheduled.html", scheduled_sites=rows, q=q, format_schedule_text=format_schedule_text)


@app.route("/built")
def built_list():
    q = request.args.get("q", "").strip()
    store = get_store()
    try:
        rows = store.query_rows(q)
    finally:
        store.close()
    rows = [row for row in rows if row_text(row, "build_status") == "已建站"]
    def sort_key(row):
        score = 0
        for col, done in [("health_status", "正常"), ("main_data_status", "已上传"),
                          ("extra_data_status", "已上传"), ("main_category_status", "已上传"),
                          ("auto_category_status", "已配置"),
                          ("plugin_status", "已配置"), ("media_status", "已配置")]:
            if row_text(row, col) == done:
                score += 1
        return score
    rows.sort(key=sort_key)
    return render_template("built.html", built_sites=rows, q=q)


@app.route("/scraper")
def scraper_page():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    default_currency_path = os.path.join(project_dir, "currency_config.json")
    default_proxies_path = os.path.join(project_dir, "proxies.txt")
    return render_template("scraper.html",
        defaults={
            "keywords": "",
            "api_mode": "1",
            "api_key": "",
            "bestproxy_auth": "",
            "max_results": "100",
            "min_product_count": "200",
            "save_mode": "excel",
            "category": "",
            "mongo_collection": "",
        },
        url_source_collections=list_url_source_collections(),
        auto_classify_defaults={"source_collection": "", "limit": "100", "min_score": "12", "min_margin": "4"},
        product_source_collections=list_product_source_collections(),
        single_collection_defaults={"single_collection": "", "task_source_mode": "2"},
        product_defaults={
            "task_source_mode": "2",
            "max_workers": "10",
            "max_retry_per_site": "4",
            "min_price": "1",
            "reuse_per_category_limit": "200",
            "reuse_max_use_count": "0",
            "reuse_max_mode2_count": "0",
            "reuse_requeue_before_crawl": False,
            "skip_already_crawled_source_url": False,
            "clear_only_consumed_urls": False,
            "clear_source_after_crawl": False,
        },
        default_currency_path=default_currency_path,
        default_currency_exists=os.path.isfile(default_currency_path),
        default_proxies_path=default_proxies_path,
        default_proxies_exists=os.path.isfile(default_proxies_path),
        product_collections=list_product_data_collections(),
        clean_product_collections=list_clean_product_data_collections(),
        export_defaults={"collection_name": "", "total_limit": "40000", "min_per_category": "31", "max_per_category": "299"},
        direct_export_defaults={"collection_name": "", "limit": "0"},
        mongo_categories=list_mongo_categories(),
        product_export_files=list_product_export_files(),
        scraper_files=list_scraper_files(),
    )


@app.route("/product-processing")
def product_processing_page():
    collections = list_product_data_collections()
    total = sum(item.get("count", 0) for item in collections)
    non_empty = sum(1 for item in collections if item.get("count", 0) > 0)
    return render_template("product_processing.html",
        total_collections=len(collections),
        non_empty_collections=non_empty,
        total_rows=total,
        selected_collection=request.args.get("collection", ""),
        collections=collections,
        clean_collections=list_clean_product_data_collections(),
        mismatch_reports=list_product_mismatch_reports(),
    )


@app.route("/reuse-queue")
def reuse_queue_page():
    q = request.args.get("q", "").strip()
    selected_status = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    summary_rows = get_reuse_queue_summary()
    selected = None
    if category:
        selected = get_reuse_queue_details(category, status_filter=selected_status, keyword=q)
    total_urls = sum(item.get("total", 0) for item in summary_rows)
    total_reusable = sum(item.get("reusable", 0) for item in summary_rows)
    total_queued = sum(item.get("queued", 0) for item in summary_rows)
    total_cooldown = sum(item.get("cooldown", 0) for item in summary_rows)
    return render_template("reuse_queue.html",
        total_collections=len(summary_rows),
        total_urls=total_urls,
        total_reusable=total_reusable,
        total_queued=total_queued,
        total_cooldown=total_cooldown,
        summary_rows=summary_rows,
        selected=selected,
        q=q,
        selected_status=selected_status,
        limit=300,
    )


@app.route("/import", methods=["GET", "POST"])
def import_excel():
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("请选择 Excel 文件", "error")
            return redirect(request.url)

        import uuid
        filename = f"{uuid.uuid4().hex}_{os.path.basename(upload.filename)}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        upload.save(filepath)

        store = get_store()
        try:
            df = pd.read_excel(filepath)
            created = 0
            updated = 0
            media_root = get_settings_snapshot()["media_root"]
            for _, row in df.iterrows():
                values = {
                    "classification": get_excel_cell(row, "classification"),
                    "build_flag": get_excel_cell(row, "build_flag"),
                    "domain": get_excel_cell(row, "domain"),
                    "template": get_excel_cell(row, "template"),
                    "main_data_source_id": get_excel_cell(row, "main_data_source_id"),
                    "extra_data_source_id": get_excel_cell(row, "extra_data_source_id").strip().strip(","),
                    "main_category": get_excel_cell(row, "main_category"),
                    "category": get_excel_cell(row, "category"),
                    "schedule_time": get_excel_cell(row, "schedule_time"),
                    "title": get_excel_cell(row, "title"),
                    "title_translation": get_excel_cell(row, "title_translation"),
                    "description": get_excel_cell(row, "description"),
                    "description_translation": get_excel_cell(row, "description_translation"),
                    "main_keyword": get_excel_cell(row, "main_keyword"),
                    "long_tail_keywords": get_excel_cell(row, "long_tail_keywords"),
                    "address": get_excel_cell(row, "address"),
                    "server": get_excel_cell(row, "server"),
                }
                domain = values["domain"]
                if not domain:
                    continue

                logo_path = os.path.join(media_root, domain, "logo.png")
                banner_path = os.path.join(media_root, domain, "banner.jpg")
                icon_path = os.path.join(media_root, domain, "icon.png")

                if os.path.exists(logo_path):
                    values["logo"] = logo_path
                if os.path.exists(banner_path):
                    values["banner"] = banner_path
                if os.path.exists(icon_path):
                    values["icon"] = icon_path

                existing_rows = store.get_rows_by_field("domain", domain)
                if existing_rows:
                    store.update_fields(existing_rows[0]["id"], values, commit=False)
                    duplicate_ids = [item["id"] for item in existing_rows[1:]]
                    if duplicate_ids:
                        store.delete_rows(duplicate_ids)
                    updated += 1
                else:
                    store.add_row(values, commit=False)
                    created += 1

            store.commit()
            flash(f"导入完成：新增 {created} 条，更新 {updated} 条", "success")
        except Exception as exc:
            flash(f"导入失败: {exc}", "error")
        finally:
            store.close()
            try:
                os.unlink(filepath)
            except Exception:
                pass
        return redirect(url_for("sites_list"))

    return render_template("import.html")


@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        store = get_store()
        try:
            for key in ("report_username", "report_password", "erp_username", "erp_password", "wp_password"):
                val = request.form.get(key, "").strip()
                store.set_setting(key, val)
            media_root = request.form.get("media_root", "").strip()
            if not media_root:
                drive = request.form.get("media_drive", "").strip()
                path = request.form.get("media_path", "").strip()
                if drive and path:
                    media_root = drive + path
            store.set_setting("media_root", media_root)
            flash("配置已保存", "success")
        except Exception as e:
            flash(f"保存失败: {e}", "error")
        finally:
            store.close()
        return redirect(url_for("config"))
    return render_template("config.html", settings=get_settings_snapshot())


@app.route("/tasks")
def tasks_list():
    task_id = request.args.get("task_id", "").strip()
    tasks = list_tasks()
    return render_template("tasks.html", tasks=tasks, current_task_id=task_id)


@app.route("/api/tasks")
def api_tasks():
    tasks = list_tasks()
    return {"tasks": tasks}


@app.route("/health/check-url", methods=["GET", "POST"])
def health_check_url():
    result = None
    if request.method == "POST":
        url_input = request.form.get("url") or request.form.get("check_url") or ""
        url_input = url_input.strip()
        if not url_input:
            flash("请输入要检查的 URL", "error")
        else:
            if not url_input.startswith("http://") and not url_input.startswith("https://"):
                url_input = "https://" + url_input
            from urllib.parse import urlparse
            from health_checker import healthcheck_domain
            parsed = urlparse(url_input)
            domain = parsed.netloc
            path = parsed.path
            ok, status, details = healthcheck_domain(domain, check_path=path)
            result = {
                "ok": ok,
                "status": status,
                "input_url": url_input,
                "domain": domain,
                "path": path,
                **details,
            }
    return render_template("check_url.html", result=result)


@app.route("/edit/<int:site_id>", methods=["GET", "POST"])
def edit_site(site_id):
    next_endpoint = request.args.get("next", "").strip() or request.form.get("next_endpoint", "").strip() or "sites_list"
    store = get_store()
    try:
        row = store.get_row(str(site_id))
        if not row:
            flash("站点不存在", "error")
            return redirect(url_for("sites_list"))

        if request.method == "POST":
            updates = {}
            for _title, fields in EDIT_SECTIONS:
                for field_name, _label in fields:
                    updates[field_name] = normalize_form_value(field_name, request.form.get(field_name, ""))
            store.update_fields(str(site_id), updates)
            flash("站点数据已更新", "success")
            return redirect(url_for("edit_site", site_id=site_id, next=next_endpoint))

        return render_template(
            "edit.html",
            site=row,
            next_endpoint=next_endpoint,
            edit_sections=EDIT_SECTIONS,
            edit_select_options=EDIT_SELECT_OPTIONS,
            domain_status_label=domain_status_label,
        )
    finally:
        store.close()


# === 批量任务路由 (POST) ===

@app.route("/tasks/report-selected", methods=["POST"])
def task_report_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要上报的站点", "error")
        return redirect(url_for("sites_list"))
    task_id = start_task("批量上报域名", "report", selected_ids, run_report_task)
    return redirect_to_task(task_id, "批量上报任务已启动")


@app.route("/tasks/refresh-reported", methods=["POST"])
def task_refresh_reported():
    selected_ids = get_selected_ids()
    title = "刷新已报域名状态" if selected_ids else "刷新全部已报域名状态"
    task_id = start_task(title, "refresh_reported", selected_ids or [], run_refresh_reported_task)
    return redirect_to_task(task_id, "刷新任务已启动")


@app.route("/tasks/delete-reported", methods=["POST"])
def task_delete_reported():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要删除上报的站点", "error")
        return redirect(url_for("reported_list"))
    task_id = start_task("删除已报域名", "delete_reported", selected_ids, run_delete_reported_task)
    return redirect_to_task(task_id, "删除上报任务已启动")


@app.route("/tasks/build-selected", methods=["POST"])
def task_build_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要建站的站点", "error")
        return redirect(url_for("reported_list"))
    task_id = start_task("批量建站", "build", selected_ids, run_build_task)
    return redirect_to_task(task_id, "建站任务已启动")


@app.route("/tasks/health-selected", methods=["POST"])
def task_health_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要检查的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量健康检查", "health", selected_ids, run_health_task)
    return redirect_to_task(task_id, "健康检查任务已启动")


@app.route("/tasks/upload-main", methods=["POST"])
def task_upload_main():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要上传主数据的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量上传主数据", "upload_main", selected_ids, run_upload_main_task)
    return redirect_to_task(task_id, "主数据上传任务已启动")


@app.route("/tasks/upload-extra", methods=["POST"])
def task_upload_extra():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要上传补充数据的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量上传补充数据", "upload_extra", selected_ids, run_upload_extra_task)
    return redirect_to_task(task_id, "补充数据上传任务已启动")


@app.route("/tasks/upload-main-category", methods=["POST"])
def task_upload_main_category():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要设置主分类的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量设置主分类", "upload_main_category", selected_ids, run_upload_main_category_task)
    return redirect_to_task(task_id, "主分类设置任务已启动")


@app.route("/tasks/configure-sites", methods=["POST"])
def task_configure_sites():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要配置的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量配置站点", "configure_sites", selected_ids, run_configure_sites_task)
    return redirect_to_task(task_id, "站点配置任务已启动")


@app.route("/tasks/clear-cache-selected", methods=["POST"])
def task_clear_cache_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要清理缓存的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量清理缓存", "clear_cache", selected_ids, run_clear_cache_task)
    return redirect_to_task(task_id, "缓存清理任务已启动")


@app.route("/tasks/configure-menu", methods=["POST"])
def task_configure_menu():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要设置菜单的站点", "error")
        return redirect(url_for("built_list"))
    task_id = start_task("批量设置菜单", "configure_menu", selected_ids, run_configure_menu_task)
    return redirect_to_task(task_id, "菜单设置任务已启动")


# === 单个站点操作路由 ===

@app.route("/site/<int:site_id>/report", methods=["POST"])
def report_domain(site_id):
    task_id = start_task("单站上报", "report", [str(site_id)], run_report_task)
    return redirect_to_task(task_id, "上报任务已启动")


@app.route("/site/<int:site_id>/build", methods=["POST"])
def build_site(site_id):
    task_id = start_task("单站建站", "build", [str(site_id)], run_build_task)
    return redirect_to_task(task_id, "建站任务已启动")


@app.route("/health/<int:site_id>")
def health_check(site_id):
    task_id = start_task("单站点健康检查", "health", [str(site_id)], run_health_task)
    return redirect_to_task(task_id, "健康检查任务已启动")


@app.route("/site/<int:site_id>/delete", methods=["POST"])
def delete_site(site_id):
    store = get_store()
    try:
        store.delete_rows([str(site_id)])
        flash("站点已删除", "success")
    finally:
        store.close()
    return redirect(url_for("sites_list"))


@app.route("/sites/delete-selected", methods=["POST"])
def delete_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要删除的站点", "error")
        return redirect(url_for("sites_list"))
    store = get_store()
    try:
        store.delete_rows(selected_ids)
        flash(f"已删除 {len(selected_ids)} 个站点", "success")
    finally:
        store.close()
    return redirect(url_for("sites_list"))


@app.route("/batch-update", methods=["POST"])
def batch_update_selected():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选站点", "error")
        return redirect(url_for("sites_list"))
    field = request.form.get("batch_field", "").strip()
    value = request.form.get("batch_value_select") or request.form.get("batch_value_text") or ""
    value = value.strip()
    store = get_store()
    try:
        for site_id in selected_ids:
            store.update_cell(site_id, field, value)
        flash(f"已更新 {len(selected_ids)} 个站点", "success")
    finally:
        store.close()
    return redirect(url_for("sites_list"))


@app.route("/schedule/set", methods=["POST"])
def schedule_set():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要设置的站点", "error")
        return redirect(url_for("sites_list"))
    time_str = request.form.get("schedule_time", "").strip()
    store = get_store()
    try:
        for site_id in selected_ids:
            store.update_fields(site_id, {SCHEDULE_ENABLED_COL: "1", SCHEDULE_TIME_COL: time_str})
        flash(f"已设置 {len(selected_ids)} 个站点的计划时间", "success")
    finally:
        store.close()
    return redirect(url_for("scheduled_list"))


@app.route("/schedule/clear", methods=["POST"])
def schedule_clear():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要清除的站点", "error")
        return redirect(url_for("sites_list"))
    store = get_store()
    try:
        for site_id in selected_ids:
            store.update_fields(site_id, {SCHEDULE_ENABLED_COL: "0", SCHEDULE_TIME_COL: ""})
        flash(f"已清除 {len(selected_ids)} 个站点的计划", "success")
    finally:
        store.close()
    return redirect(url_for("scheduled_list"))


@app.route("/reported/export-weekly")
def export_reported_weekly():
    keyword = request.args.get("q", "").strip()
    week_start, week_end = current_week_range()
    rows = [row for row in list_all_sites() if is_reported(row)]
    rows = filter_rows_by_domain(rows, keyword)
    export_rows = []
    for row in rows:
        report_time = row_text(row, REPORT_TIME_COL)
        report_dt = parse_datetime_text(report_time)
        if report_dt is None:
            continue
        if not (week_start <= report_dt < week_end):
            continue
        export_rows.append({
            "创建时间": report_time,
            "域名": row_text(row, "domain"),
            "模板": row_text(row, "template"),
            "服务器": row_text(row, "server"),
        })
    export_rows.sort(key=lambda item: item["创建时间"])
    if not export_rows:
        flash("本周没有可导出的已报数据", "error")
        return redirect(url_for("reported_list", q=keyword))
    import pandas as pd
    from io import BytesIO
    output = BytesIO()
    pd.DataFrame(export_rows, columns=["创建时间", "域名", "模板", "服务器"]).to_excel(output, index=False)
    output.seek(0)
    filename = f"weekly_report_{week_start.strftime('%Y%m%d')}_{(week_end - timedelta(days=1)).strftime('%Y%m%d')}.xlsx"
    from flask import send_file
    return send_file(output, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# === 爬取相关路由 ===

@app.route("/scraper/start", methods=["POST"])
def scraper_start():
    options = {
        "keywords": request.form.get("keywords", ""),
        "api_mode": request.form.get("api_mode", "1"),
        "api_key": request.form.get("api_key", ""),
        "bestproxy_auth": request.form.get("bestproxy_auth", ""),
        "max_results": request.form.get("max_results", "100"),
        "min_product_count": request.form.get("min_product_count", "200"),
        "save_mode": request.form.get("save_mode", "excel"),
        "category": request.form.get("category", ""),
        "mongo_collection": request.form.get("mongo_collection", ""),
    }
    task_id = start_task(f"数据爬取 - {options['category'] or 'default'}", "scraper", [], lambda t: run_scraper_task(t, options))
    return redirect_to_task(task_id, "数据爬取任务已启动")


@app.route("/scraper/crawl-domains", methods=["POST"])
def scraper_crawl_domains():
    domain_text = request.form.get("domains", "").strip()
    category = request.form.get("category", "").strip()
    if not domain_text:
        flash("请粘贴要爬取的域名", "error")
        return redirect(url_for("scraper_page"))
    if not category:
        flash("请填写分类名", "error")
        return redirect(url_for("scraper_page"))

    domains = parse_domains_text(domain_text)
    if not domains:
        flash("没有有效的域名", "error")
        return redirect(url_for("scraper_page"))

    options = {
        "domains": domains,
        "category": category,
        "mongo_collection": request.form.get("mongo_collection", "").strip(),
        "max_workers": request.form.get("max_workers", "10"),
        "max_retry_per_site": request.form.get("max_retry_per_site", "4"),
        "min_price": request.form.get("min_price", "1"),
        "reuse_requeue_before_crawl": request.form.get("reuse_requeue_before_crawl") == "1",
        "reuse_per_category_limit": request.form.get("reuse_per_category_limit", "200"),
        "skip_already_crawled_source_url": request.form.get("skip_already_crawled_source_url") == "1",
    }
    task_id = start_task(f"域名直爬 - {category} ({len(domains)} 个域名)", "domain_crawl", [],
                         lambda t: run_domain_crawl_task(t, options))
    return redirect_to_task(task_id, "域名直爬任务已启动")


def run_domain_crawl_task(task, options):
    from product_data_crawler import ProductCrawlerService
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    domains = list(options.get("domains") or [])
    # Remove duplicates
    seen = set()
    unique_domains = []
    for d in domains:
        d = d.strip().lower()
        if d and d not in seen:
            seen.add(d)
            unique_domains.append(d)
    domains = unique_domains

    category = (options.get("category") or "").strip()
    if not domains or not category:
        raise RuntimeError("缺少域名或分类名")

    project_dir = os.path.dirname(os.path.abspath(__file__))
    currency_config_path = os.path.join(project_dir, "currency_config.json")
    proxies_file = os.path.join(project_dir, "proxies.txt")

    if not os.path.exists(currency_config_path):
        raise RuntimeError("汇率文件不存在，请先在项目根目录放置 currency_config.json")
    if not os.path.exists(proxies_file):
        raise RuntimeError("代理文件不存在，请先在项目根目录放置 proxies.txt")

    append_task_log(task, f"域名直爬开始：{len(domains)} 个域名，分类 {category}，线程数 {options.get('max_workers', 10)}")

    service = ProductCrawlerService(
        currency_config_path=currency_config_path,
        proxies_file=proxies_file,
        task_source_mode=2,
        max_workers=int(options.get("max_workers") or 10),
        max_retry_per_site=int(options.get("max_retry_per_site") or 4),
        min_price=float(options.get("min_price") or 1),
        reuse_requeue_before_crawl=False,
        progress_callback=lambda message: append_task_log(task, message),
    )
    service.stop_callback = lambda: is_task_stop_requested(task)
    service.skip_non_english_check = True

    success_count = 0
    failed_count = 0
    total_inserted = 0
    lock = threading.Lock()

    def crawl_one(domain):
        nonlocal success_count, failed_count, total_inserted
        url = f"https://{domain}"
        # Quick check: test if this is a Shopify store
        try:
            import requests as _req
            quick_r = _req.get(f"{url}/products.json", timeout=8, verify=False)
            if quick_r.status_code != 200:
                with lock:
                    success_count += 1
                task_site_log(task, domain, "非 Shopify 站点")
                return
        except Exception:
            with lock:
                failed_count += 1
            task_site_log(task, domain, "连接失败")
            return
        # Full crawl (crawl_site will report progress via progress_callback)
        try:
            result = service.crawl_site((url, category, ""))
            inserted = int(result.get("inserted", 0))
            with lock:
                total_inserted += inserted
                if result.get("success"):
                    success_count += 1
                    if inserted > 0:
                        task_site_log(task, domain, f"爬取成功，新增 {inserted} 件商品")
                    else:
                        task_site_log(task, domain, "非 Shopify 或无商品")
                else:
                    failed_count += 1
                    if result.get("blacklisted"):
                        task_site_log(task, domain, "非英文站点已跳过")
                    elif result.get("stopped"):
                        task_site_log(task, domain, "已停止")
                    else:
                        task_site_log(task, domain, "爬取失败（汇率/连接）")
        except Exception as exc:
            with lock:
                failed_count += 1
            task_site_log(task, domain, f"爬取异常: {exc}")

    max_workers = min(int(options.get("max_workers") or 10), len(domains))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(crawl_one, d) for d in domains]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as exc:
                pass

    try:
        service.close()
    except Exception:
        pass

    task["success_count"] = success_count
    task["failed_count"] = failed_count
    append_task_log(task, f"域名直爬完成：成功 {success_count} 个，失败 {failed_count} 个，新增商品 {total_inserted} 件")


@app.route("/scraper/auto-classify", methods=["POST"])
def scraper_auto_classify():
    options = {
        "source_collection": request.form.get("source_collection", ""),
        "limit": request.form.get("limit", "100"),
        "min_score": request.form.get("min_score", "12"),
        "min_margin": request.form.get("min_margin", "4"),
    }
    task_id = start_task(f"URL 自动分类 - {options['source_collection'] or 'unknown'}", "url_auto_classify", [],
                         lambda t: run_url_auto_classify_task_clean(t, options))
    return redirect_to_task(task_id, "URL 自动分类任务已启动")


@app.route("/scraper/start-product-crawl", methods=["POST"])
def scraper_start_product_crawl():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    options = {
        "currency_config_path": os.path.join(project_dir, "currency_config.json"),
        "proxies_file": os.path.join(project_dir, "proxies.txt"),
        "task_source_mode": request.form.get("task_source_mode", "2"),
        "max_workers": request.form.get("max_workers", "10"),
        "max_retry_per_site": request.form.get("max_retry_per_site", "4"),
        "min_price": request.form.get("min_price", "1"),
        "reuse_requeue_before_crawl": request.form.get("reuse_requeue_before_crawl") == "1",
        "reuse_per_category_limit": request.form.get("reuse_per_category_limit", "200"),
        "reuse_max_use_count": request.form.get("reuse_max_use_count", "0"),
        "reuse_max_mode2_count": request.form.get("reuse_max_mode2_count", "0"),
        "clear_source_after_crawl": request.form.get("clear_source_after_crawl") == "1",
        "clear_only_consumed_urls": request.form.get("clear_only_consumed_urls") == "1",
        "skip_already_crawled_source_url": request.form.get("skip_already_crawled_source_url") == "1",
    }
    task_id = start_task("商品数据爬取", "product_crawler", [], lambda t: run_product_crawler_task(t, options))
    return redirect_to_task(task_id, "商品数据爬取任务已启动")


@app.route("/scraper/start-single-collection-crawl", methods=["POST"])
def scraper_start_single_collection_crawl():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    options = {
        "currency_config_path": os.path.join(project_dir, "currency_config.json"),
        "proxies_file": os.path.join(project_dir, "proxies.txt"),
        "single_collection": request.form.get("single_collection", ""),
        "task_source_mode": request.form.get("task_source_mode", "2"),
        "max_workers": request.form.get("max_workers", "10"),
        "max_retry_per_site": request.form.get("max_retry_per_site", "4"),
        "min_price": request.form.get("min_price", "1"),
        "reuse_requeue_before_crawl": request.form.get("reuse_requeue_before_crawl") == "1",
        "reuse_per_category_limit": request.form.get("reuse_per_category_limit", "200"),
        "reuse_max_use_count": request.form.get("reuse_max_use_count", "0"),
        "reuse_max_mode2_count": request.form.get("reuse_max_mode2_count", "0"),
        "clear_source_after_crawl": request.form.get("clear_source_after_crawl") == "1",
        "clear_only_consumed_urls": request.form.get("clear_only_consumed_urls") == "1",
        "skip_already_crawled_source_url": request.form.get("skip_already_crawled_source_url") == "1",
    }
    task_id = start_task("单类目集合商品爬取", "single_collection_crawler", [], lambda t: run_single_collection_crawler_task(t, options))
    return redirect_to_task(task_id, "单类目集合商品爬取任务已启动")


@app.route("/scraper/start-nav-category-crawl", methods=["POST"])
def scraper_start_nav_category_crawl():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    options = {
        "currency_config_path": os.path.join(project_dir, "currency_config.json"),
        "proxies_file": os.path.join(project_dir, "proxies.txt"),
        "task_source_mode": request.form.get("task_source_mode", "2"),
        "max_workers": request.form.get("max_workers", "10"),
        "max_retry_per_site": request.form.get("max_retry_per_site", "4"),
        "min_price": request.form.get("min_price", "1"),
        "reuse_requeue_before_crawl": request.form.get("reuse_requeue_before_crawl") == "1",
        "reuse_per_category_limit": request.form.get("reuse_per_category_limit", "200"),
        "reuse_max_use_count": request.form.get("reuse_max_use_count", "0"),
        "reuse_max_mode2_count": request.form.get("reuse_max_mode2_count", "0"),
        "clear_source_after_crawl": request.form.get("clear_source_after_crawl") == "1",
        "clear_only_consumed_urls": request.form.get("clear_only_consumed_urls") == "1",
        "skip_already_crawled_source_url": request.form.get("skip_already_crawled_source_url") == "1",
    }
    task_id = start_task("导航分类商品爬取", "nav_category_crawler", [], lambda t: run_nav_category_crawler_task(t, options))
    return redirect_to_task(task_id, "导航分类爬取任务已启动")


@app.route("/scraper/requeue-reuse", methods=["POST"])
def scraper_requeue_reuse():
    options = {
        "task_source_mode": request.form.get("task_source_mode", "2"),
        "reuse_per_category_limit": request.form.get("reuse_per_category_limit", "200"),
        "reuse_max_use_count": request.form.get("reuse_max_use_count", "0"),
        "reuse_max_mode2_count": request.form.get("reuse_max_mode2_count", "0"),
    }
    task_id = start_task("复用池回投到任务池", "reuse_requeue", [], lambda t: run_reuse_queue_task(t, options))
    return redirect_to_task(task_id, "复用池回投任务已启动")


@app.route("/product-processing/run", methods=["POST"])
def product_processing_run():
    action = request.form.get("action", "").strip()
    raw_collection = request.form.get("collection")
    collection = (raw_collection or "").strip()
    if action == "clean_forbidden":
        raw_collection = request.form.get("clean_collection")
        collection = (raw_collection or "").strip()
    title_map = {
        "all": "商品数据清洗",
        "basic": "商品基础数据清洗",
        "image": "商品异常图片清洗",
        "english": "商品英文数据过滤",
        "forbidden": "商品违禁词过滤",
        "clean_forbidden": "clean集合违禁词过滤",
        "excel_category_merge": "Excel分类处理",
        "category": "商品分类清洗",
        "mismatch": "商品快速分类扫描",
        "mismatch_move": "商品快速分类直删",
        "domain_match": "域名分类匹配",
    }
    if action not in title_map:
        flash("未知的处理动作", "error")
        return redirect(url_for("product_processing_page"))
    if action == "excel_category_merge":
        upload = request.files.get("excel_file")
        if not upload or not upload.filename:
            flash("请先上传 Excel 文件", "error")
            return redirect(url_for("product_processing_page"))
        filename = f"{uuid.uuid4().hex}_{os.path.basename(upload.filename)}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        upload.save(filepath)
        threshold = (request.form.get("excel_threshold") or "").strip() or "30"
        try:
            threshold_value = int(threshold)
        except ValueError:
            flash("分类阈值必须是整数", "error")
            try:
                os.remove(filepath)
            except OSError:
                pass
            return redirect(url_for("product_processing_page"))
        task_id = start_task(
            f"Excel分类处理 - {upload.filename}",
            "product_processing",
            [],
            lambda t, opts={
                "action": action,
                "collection": "__excel__",
                "file_path": filepath,
                "threshold": threshold_value,
            }: run_product_processing_task(t, opts),
        )
        return redirect_to_task(task_id, "Excel 分类处理任务已启动")
    if not collection:
        flash("Please select a processing scope; empty scope will not default to all collections.", "error")
        return redirect(url_for("product_processing_page"))
    if action == "domain_match":
        domain = request.form.get("domain", "").strip()
        category_raw = request.form.get("category", "").strip()
        if not domain:
            flash("请输入域名", "error")
            return redirect(url_for("product_processing_page"))
        lines = [line.strip() for line in category_raw.split("\n") if line.strip()]
        valid_lines = [line for line in lines if "|||" in line]
        if not valid_lines:
            flash("请至少输入一个有效分类，格式: 一级分类|||二级分类（每行一个）", "error")
            return redirect(url_for("product_processing_page"))
        category_text = "\n".join(valid_lines)
        task_id = start_task(f"域名分类匹配 - {domain}", "product_processing", [],
                             lambda t, opts={"action": action, "collection": collection, "domain": domain, "category": category_text}: run_product_processing_task(t, opts))
        return redirect_to_task(task_id, "域名分类匹配任务已启动")
    task_id = start_task(f"{title_map[action]} - {collection}", "product_processing", [],
                         lambda t, opts={"action": action, "collection": collection}: run_product_processing_task(t, opts))
    return redirect_to_task(task_id, "商品处理任务已启动")


@app.route("/product-processing/structured-crawl/run", methods=["POST"])
def structured_crawl_run():
    domain = request.form.get("domain", "").strip()
    category_raw = request.form.get("category", "").strip()
    api_mode = request.form.get("api_mode", "5").strip()

    if not domain:
        flash("请输入域名", "error")
        return redirect(url_for("product_processing_page"))
    lines = [line.strip() for line in category_raw.split("\n") if line.strip()]
    valid_lines = [line for line in lines if "|||" in line]
    if not valid_lines:
        flash("请至少输入一个有效分类，格式: 一级分类|||二级分类（每行一个）", "error")
        return redirect(url_for("product_processing_page"))
    category_text = "\n".join(valid_lines)

    from structured_site_crawler import run_structured_crawl

    task_id = start_task(f"结构网站数据获取 - {domain}", "structured_crawl", [],
                         lambda t, opts={"domain": domain, "category_text": category_text, "api_mode": api_mode}: run_structured_crawl_task(t, opts))
    return redirect_to_task(task_id, "结构网站数据获取任务已启动")


@app.route("/scraper/import-urls", methods=["POST"])
def scraper_import_urls():
    upload = request.files.get("txt_file")
    category = request.form.get("import_category", "").strip()
    if not upload or not upload.filename:
        flash("请选择要导入的 txt 文件", "error")
        return redirect(url_for("scraper_page"))
    if not category:
        flash("请填写导入分类名称", "error")
        return redirect(url_for("scraper_page"))
    import uuid
    filename = f"{uuid.uuid4().hex}_{os.path.basename(upload.filename)}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    upload.save(filepath)
    options = {"file_path": filepath, "category": category}
    task_id = start_task(f"URL 导入 - {category}", "url_import", [], lambda t: run_url_import_task(t, options))
    return redirect_to_task(task_id, "URL 导入任务已启动")


@app.route("/scraper/export-urls")
def scraper_export_urls():
    category = request.args.get("category", "").strip()
    if not category:
        flash("请先选择要导出的分类", "error")
        return redirect(url_for("scraper_page"))
    try:
        from url_import_export import export_urls_to_memory
        buffer, count, collection_name = export_urls_to_memory(category)
    except Exception as exc:
        flash(f"导出失败: {exc}", "error")
        return redirect(url_for("scraper_page"))
    filename = f"{category}_tasks.txt"
    from flask import send_file
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="text/plain; charset=utf-8")


@app.route("/scraper/export-products", methods=["POST"])
def scraper_export_products():
    collection_name = request.form.get("collection_name", "").strip()
    total_limit = request.form.get("total_limit", "40000").strip()
    min_per_category = request.form.get("min_per_category", "31").strip()
    max_per_category = request.form.get("max_per_category", "299").strip()
    if not collection_name:
        flash("请先选择要导出的集合", "error")
        return redirect(url_for("scraper_page"))
    try:
        total_limit_value = int(total_limit)
        min_value = int(min_per_category)
        max_value = int(max_per_category)
    except ValueError:
        flash("总导出数量和小分类限制必须填写整数", "error")
        return redirect(url_for("scraper_page"))
    options = {
        "collection_name": collection_name,
        "total_limit": total_limit_value,
        "min_per_category": min_value,
        "max_per_category": max_value,
    }
    task_id = start_task(f"商品导出 - {collection_name}", "product_export", [],
                         lambda t, opts=options: run_product_export_task(t, opts))
    return redirect_to_task(task_id, "商品导出任务已启动")


@app.route("/scraper/direct-export-products", methods=["POST"])
def scraper_direct_export_products():
    collection_name = request.form.get("collection_name", "").strip()
    limit = request.form.get("limit", "0").strip()
    if not collection_name:
        flash("请先选择要直接导出的 _clean 集合", "error")
        return redirect(url_for("scraper_page"))
    if not collection_name.endswith("_clean"):
        flash("数据库直接导出商品只允许选择 shopify_data_new 下的 _clean 集合", "error")
        return redirect(url_for("scraper_page"))
    try:
        limit_value = int(limit or 0)
    except ValueError:
        flash("导出数量必须填写整数，0 表示导出全部", "error")
        return redirect(url_for("scraper_page"))
    if limit_value < 0:
        flash("导出数量不能小于 0", "error")
        return redirect(url_for("scraper_page"))

    valid_names = {item.get("name") for item in list_clean_product_data_collections()}
    if collection_name not in valid_names:
        flash("只能导出当前 shopify_data_new 数据库下存在的 _clean 集合", "error")
        return redirect(url_for("scraper_page"))

    options = {
        "collection_name": collection_name,
        "limit": limit_value,
    }
    task_id = start_task(f"数据库直接导出商品 - {collection_name}", "direct_product_export", [],
                         lambda t, opts=options: run_direct_product_export_task(t, opts))
    return redirect_to_task(task_id, "数据库直接导出商品任务已启动")


@app.route("/reuse-queue/export-domains")
def reuse_queue_export_domains():
    category = request.args.get("category", "").strip()
    status_filter = request.args.get("status", "").strip()
    if not category:
        flash("请先选择分类", "error")
        return redirect(url_for("reuse_queue_page"))
    client, db = get_reuse_queue_db()
    try:
        collection_name = f"{category}_Reuse_Queue"
        if collection_name not in db.list_collection_names():
            flash(f"未找到集合: {collection_name}", "error")
            return redirect(url_for("reuse_queue_page"))
        collection = db[collection_name]
        query = {}
        if status_filter:
            query["Status"] = status_filter
        domains = set()
        for doc in collection.find(query, {"URL": 1, "_id": 0}):
            url = str(doc.get("URL") or "")
            domain = _extract_domain(url)
            if domain:
                domains.add(domain)
        if not domains:
            flash("没有可导出的域名", "error")
            return redirect(url_for("reuse_queue_page", category=category))
        content = "\n".join(sorted(domains)) + "\n"
        from io import BytesIO
        buffer = BytesIO(content.encode("utf-8"))
        buffer.seek(0)
        label = f"{category}_reuse_domains.txt"
        if status_filter:
            label = f"{category}_{status_filter}_reuse_domains.txt"
        from flask import send_file
        return send_file(buffer, as_attachment=True, download_name=label, mimetype="text/plain; charset=utf-8")
    finally:
        client.close()


@app.route("/tasks/<task_id>/stop", methods=["POST"])
def task_stop(task_id):
    task = get_task(task_id)
    if not task:
        return {"ok": False, "error": "任务不存在"}
    request_task_stop(task)
    return {"ok": True}


@app.route("/orders")
def orders_page():
    lists = _load_order_lists()
    history = _load_order_history()
    task_result = None
    loading = None
    task_id = request.args.get("task_id", "").strip()
    if task_id:
        task = get_task(task_id)
        if task:
            if task.get("status") in ("running", "queued"):
                loading = task.get("title", "订单分析中...")
            elif task.get("status") == "completed":
                details = task.get("order_details") or []
                all_monthly = defaultdict(lambda: {"c": 0, "v": 0, "r": 0.0, "cl": 0, "fd": 0})
                all_statuses = defaultdict(int)
                all_sites = defaultdict(int)
                cancel_sites = []
                for d in details:
                    total_cancelled = 0
                    total_orders_for_site = 0
                    for mk, mv in (d.get("monthly_breakdown") or {}).items():
                        all_monthly[mk]["c"] += mv["count"]
                        all_monthly[mk]["v"] += mv["valid_count"]
                        all_monthly[mk]["r"] += mv["valid_total"]
                        all_monthly[mk]["cl"] += mv.get("cancelled", 0)
                        all_monthly[mk]["fd"] += mv["count"] - mv["valid_count"] - mv.get("cancelled", 0)
                        total_cancelled += mv.get("cancelled", 0)
                        total_orders_for_site += mv["count"]
                    for sk, sv in (d.get("status_counts") or {}).items():
                        if sk != "all" and sv > 0:
                            all_statuses[sk] += sv
                    real = d.get("real", 0)
                    if real > 0:
                        all_sites[d["domain"]] += real
                    if total_orders_for_site > 0:
                        cancel_rate = total_cancelled / max(total_orders_for_site, 1) * 100
                        if cancel_rate > 30:
                            cancel_sites.append({
                                "domain": d["domain"],
                                "total": total_orders_for_site,
                                "cancelled": total_cancelled,
                                "rate": round(cancel_rate, 1),
                            })

                monthly_list = [{"m": mk, "c": mv["c"], "v": mv["v"], "r": round(mv["r"], 2), "cl": mv["cl"], "fd": mv["fd"]}
                               for mk, mv in sorted(all_monthly.items())]
                status_list = [{"l": ORDER_STATUS_LABELS.get(sk, sk), "c": sv}
                              for sk, sv in sorted(all_statuses.items(), key=lambda x: -x[1])]
                top_list = [{"d": sd, "c": sc} for sd, sc in sorted(all_sites.items(), key=lambda x: -x[1])[:10]]

                total_sites = task.get("checked_sites", 0)
                with_orders = task.get("sites_with_orders", 0)
                without = task.get("sites_without_orders", 0)
                total_orders_all = sum(d.get("total", 0) for d in details)

                task_result = {
                    "summary": f"已检查 {total_sites} 个站点，有订单 {with_orders} 个，无订单 {without} 个，共 {total_orders_all} 笔",
                    "monthly": monthly_list,
                    "statuses": status_list,
                    "top_sites": top_list,
                    "cancel_sites": sorted(cancel_sites, key=lambda x: -x["rate"])[:10],
                }

                # Save to history
                total_valid = sum(mv['v'] for mv in monthly_list)
                total_valid_r = sum(mv['r'] for mv in monthly_list)
                new_entry = {
                    "id": task_id,
                    "time": now_str(),
                    "title": task.get("title", ""),
                    "total_sites": total_sites,
                    "with_orders": with_orders,
                    "without": without,
                    "total_orders": total_orders_all,
                    "real_orders": sum(d.get("real", 0) for d in details),
                    "valid_orders": total_valid,
                    "valid_amount": round(total_valid_r, 2),
                    "monthly": monthly_list,
                }
                history = [new_entry]
                _save_order_history(history)
    # Check if any order_analysis task is running or recently completed
    if not task_id and not task_result:
        found_running = False
        for t in list_tasks():
            if t.get("action") == "order_analysis":
                if t.get("status") in ("running", "queued"):
                    loading = t.get("title", "订单分析中...")
                    found_running = True
                    break
                elif t.get("status") == "completed" and not found_running:
                    # Auto-redirect to the most recent completed order_analysis task
                    task_id = t["id"]
        if task_id and not loading:
            return redirect(url_for("orders_page", task_id=task_id))
    # Merge all history monthly data
    merged_history_monthly = defaultdict(lambda: {"c": 0, "v": 0, "r": 0.0, "cl": 0, "fd": 0})
    for entry in history:
        for m in entry.get("monthly") or []:
            merged_history_monthly[m["m"]]["c"] += m.get("c", 0)
            merged_history_monthly[m["m"]]["v"] += m["v"]
            merged_history_monthly[m["m"]]["r"] += m["r"]
            merged_history_monthly[m["m"]]["cl"] += m.get("cl", 0)
            merged_history_monthly[m["m"]]["fd"] += m.get("fd", 0)
    history_monthly = [{"m": mk, "c": mv["c"], "v": mv["v"], "r": round(mv["r"], 2), "cl": mv["cl"], "fd": mv["fd"]}
                       for mk, mv in sorted(merged_history_monthly.items())]
    return render_template("orders.html", lists=lists, task_result=task_result, loading=loading, exchange_rate=2.2, history=history, history_monthly=history_monthly)


@app.route("/orders/save-list", methods=["POST"])
def orders_save_list():
    list_id = (request.form.get("list_id") or "").strip()
    list_name = (request.form.get("list_name") or "").strip()
    domain_text = request.form.get("domains", "").strip()
    domains = _parse_domain_text(domain_text)
    if not list_name or not domains:
        flash("请填写列表名称和域名", "error")
        return redirect(url_for("orders_page"))
    data = _load_order_lists()
    if not list_id or list_id not in data:
        list_id = uuid.uuid4().hex[:8]
    data[list_id] = {"name": list_name, "domains": domains}
    _save_order_lists(data)
    flash(f"列表 '{list_name}' 已保存 ({len(domains)} 个域名)", "success")
    return redirect(url_for("orders_page"))


@app.route("/orders/delete-list", methods=["POST"])
def orders_delete_list():
    list_id = request.form.get("list_id", "").strip()
    data = _load_order_lists()
    if list_id in data:
        del data[list_id]
        _save_order_lists(data)
    return redirect(url_for("orders_page"))


@app.route("/orders/check", methods=["POST"])
def orders_check():
    list_id = request.form.get("list_id", "").strip()
    data = _load_order_lists()
    if list_id not in data:
        flash("列表不存在", "error")
        return redirect(url_for("orders_page"))
    domains = data[list_id]["domains"]
    month = request.form.get("order_month", "").strip()
    task = create_task(f"订单分析 - {data[list_id]['name']}", "order_analysis", [])
    task["order_domains"] = domains
    task["order_month"] = month
    task["site_count"] = len(domains)
    task["list_id"] = list_id
    run_background_task(task, run_check_orders_domains_task)
    flash(f"已启动 {len(domains)} 个域名的订单分析任务", "success")
    return redirect(url_for("orders_page"))


@app.route("/orders/export-cancelled", methods=["POST"])
def orders_export_cancelled():
    task_id = request.form.get("task_id", "").strip()
    month = request.form.get("month", "").strip()
    if not task_id or not month:
        flash("参数错误", "error")
        return redirect(url_for("orders_page"))
    task = get_task(task_id)
    if not task or task.get("status") != "completed":
        flash("任务数据已过期，请重新查询", "error")
        return redirect(url_for("orders_page"))
    details = task.get("order_details") or []
    lines = []
    for d in details:
        mb = d.get("monthly_breakdown") or {}
        mv = mb.get(month)
        if mv and int(mv.get("cancelled", 0)) > 0:
            lines.append(d["domain"])
    if not lines:
        flash(f"{month} 没有存在取消单的站点", "info")
        return redirect(url_for("orders_page", task_id=task_id))
    content = "\r\n".join(lines)
    safe_month = month.replace("-", "")
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment;filename=cancelled_sites_{safe_month}.txt"
    return resp


# === 批量生成 Logo ===

LOGO_FONTS = {
    "marseille-free-regular.png": ("MAXEB", "YjJhMWM5YThlNWVkNGM4OGE2MTQxOWIxMzI3MmE0NzYudHRm"),
    "border-wall.png": ("OG55o", "YjRjYzFiYTY5ZjcxNDJkYzljYWU5NzE3NGFiZmRiNGMub3Rm"),
    "remalos-regular.png": ("aYj1m", "ZGE0MjZkMzBjNzliNDllYmE0YTI3MjcwZTUwOWQxYTgudHRm"),
    "blush-asliring-regular.png": ("OGP66", "MmViNTViMmRjYWZiNDg1ZmI1NDljMmExNDIxYmRhMTIub3Rm"),
    "granika.png": ("MAm6r", "YjhhYmI2NDM1ZGI2NDQzOGIzMTk5ZDlkYTIyNjU3NmUub3Rm"),
    "billionery-regular.png": ("drXjg", "NWUyOThkN2E2MGJiNDA4N2FkZDk0OTA3Yjc4Y2VlZjkub3Rm"),
    "kingsman-demo.png": ("1GVgg", "OTI2YjVlNjExZGJlNDMyMzk3ZTA2YzUxNjIyOGIwYmMudHRm"),
    "shifty-notes-regular.png": ("BWZ6d", "N2NjMWFjYTM2M2M2NGYyMjhhZTg1NjliNWM4ZTJhMWMudHRm"),
    "guavine-demo-regular.png": ("1jGgL", "NzZkOGZhMzNkYTAwNGY1OGI0MDIyZGUzODQ4YjMzNTUub3Rm"),
    "boogie-boys-regular.png": ("L36y3", "MGE3YzA3NWYyY2NkNDI4MTgyZDk3OWFjY2JkMDE1MjMub3Rm"),
    "super-cedar.png": ("YqqeO", "ZDVhNGViODY5Mjg1NDQ1YmJkOTM1ZDVkOWQxZWZiYWEudHRm"),
    "mangabey-regular.png": ("rgqVO", "ZjIyNWI2ZDA2YTRiNDZiM2IzNzkwZjUxMjE0M2Q4ZGIub3Rm"),
    "goldleaf-bold-personal-use-bold.png": ("eZ4dO", "MjdjNTBiM2ZjOTkzNGIwOGFiMmNmN2I0YmViOTBjZmMudHRm"),
    "andalucia-regular.png": ("m22BP", "M2U2ZjI1NTA3ZTIwNDZlZjhiMzlhZjMxMWZlYWQ1YTgudHRm"),
    "flotha-display-bold.png": ("aYOr5", "YTI1ZDFkYWNhMGRlNGVkMDg3OTZhMTNlNjFhYTJlODMudHRm"),
}

LOGO_HEIGHT = 65
LOGO_WIDTH = 1000
LOGO_FG_COLOR = "000000"
LOGO_BG_COLOR = "FFFFFF"
LOGO_SIZE = 65
LOGO_TB = 1
LOGO_DELAY = 2


def run_generate_logos_task(task):
    store = get_store()
    try:
        rows = get_rows_for_ids(store, task["site_ids"])
        font_usage = {font_name: 0 for font_name in LOGO_FONTS}
        max_usage = max(len(rows) // len(LOGO_FONTS) + 1, 1)

        for row in rows:
            if is_task_stop_requested(task):
                append_task_log(task, "任务已被用户停止")
                set_task_status(task, "stopped")
                return

            domain = row_text(row, "domain") or "未知域名"
            text = domain.replace(".com", "")

            available_fonts = [f for f in LOGO_FONTS if font_usage[f] < max_usage]
            if not available_fonts:
                available_fonts = list(LOGO_FONTS.keys())
                font_usage = {f: 0 for f in LOGO_FONTS}

            font_name = random.choice(available_fonts)
            font_id, code = LOGO_FONTS[font_name]
            font_usage[font_name] += 1

            encoded_text = base64.urlsafe_b64encode(text.encode()).decode()

            url = (
                f"https://see.fontimg.com/api/rf5/{font_id}/{code}/{encoded_text}/{font_name}"
                f"?r=fs&h={LOGO_HEIGHT}&w={LOGO_WIDTH}&fg={LOGO_FG_COLOR}&bg={LOGO_BG_COLOR}"
                f"&tb={LOGO_TB}&s={LOGO_SIZE}"
            )

            task_site_log(task, domain, f"使用字体: {font_name}")

            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200:
                    output_dir = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "media", "setting", domain
                    )
                    os.makedirs(output_dir, exist_ok=True)
                    filepath = os.path.join(output_dir, "logo.png")
                    with open(filepath, "wb") as f:
                        f.write(resp.content)
                    task_site_log(task, domain, f"Logo 已保存: {filepath}")
                    task["success_count"] += 1
                else:
                    task_site_log(task, domain, f"请求失败，状态码: {resp.status_code}")
                    task["failed_count"] += 1
            except Exception as e:
                task_site_log(task, domain, f"异常: {e}")
                task["failed_count"] += 1

            time.sleep(LOGO_DELAY)

    except Exception as e:
        append_task_log(task, f"任务异常: {e}")
        append_task_log(task, traceback.format_exc())
        task["errors"].append(str(e))
    finally:
        store.close()


@app.route("/tasks/generate-logos", methods=["POST"])
def task_generate_logos():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要生成 Logo 的站点", "error")
        return redirect(url_for("sites_list"))
    task_id = start_task("批量生成 Logo", "generate_logos", selected_ids, run_generate_logos_task)
    return redirect_to_task(task_id, "批量生成 Logo 任务已启动")


@app.route("/tasks/generate-logos-reported", methods=["POST"])
def task_generate_logos_reported():
    selected_ids = get_selected_ids()
    if not selected_ids:
        flash("请先勾选要生成 Logo 的域名", "error")
        return redirect(url_for("reported_list"))
    task_id = start_task("批量生成 Logo", "generate_logos", selected_ids, run_generate_logos_task)
    return redirect_to_task(task_id, "批量生成 Logo 任务已启动")


if __name__ == "__main__":
    import subprocess, socket
    # 检查 MongoDB 是否运行
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", 27017))
        print("MongoDB already running")
    except ConnectionRefusedError:
        print("MongoDB not running, attempting to start...")
        try:
            subprocess.run(["net", "start", "MongoDB"], capture_output=True, timeout=10)
        except Exception:
            mongo_exe = "E:\\MongoDB\\bin\\mongod.exe"
            if os.path.exists(mongo_exe):
                subprocess.Popen(
                    [mongo_exe, "--dbpath", "E:\\MongoDB\\data", "--logpath", "E:\\MongoDB\\log\\mongod.log",
                     "--bind_ip", "127.0.0.1", "--port", "27017", "--logappend"],
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
        print("Waiting for MongoDB...")
        time.sleep(3)
    finally:
        s.close()
    app.run(host="0.0.0.0", port=5003, debug=False, threaded=True)

