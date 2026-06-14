import os, sys, threading, traceback, uuid
from datetime import datetime
import pandas as pd
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batchdeal import batchdealsite
from web_console import (
    sites_list, built_list, reported_list, tasks_list, api_tasks,
    import_excel, config, report_domain, build_site, health_check,
    delete_site, delete_selected,
    task_report_selected, task_refresh_reported, task_delete_reported,
    task_build_selected, task_health_selected,
    task_upload_main, task_upload_extra, task_upload_main_category,
    task_configure_sites, task_clear_cache_selected,
    run_background_task, run_health_task, run_upload_main_task,
    run_upload_extra_task, run_upload_main_category_task,
    run_configure_sites_task, run_clear_cache_task,
    run_build_task, run_report_task, run_refresh_reported_task,
    run_delete_reported_task,
    run_report_task,
    get_store, now_str, normalize_ids, filter_rows_by_domain,
    get_settings_snapshot, list_all_sites,
    create_task, append_task_log, set_task_status,
    task_progress_callback, task_site_log,
    ensure_required_setting, get_rows_for_ids,
    build_uploaded_sync_updates,
    row_text, sort_rows_by_time_desc, parse_datetime_text,
    domain_status_label, is_reported, get_selected_ids,
    redirect_to_task, get_excel_cell, normalize_form_value
)
from constants import (
    TABLE_NAME, DB_PATH, COLUMNS, BUILD_STATUS_COL, BUILD_TIME_COL,
    DOMAIN_NUMBER_COL, DOMAIN_STATUS_LABELS, CATEGORY_ID_MAP,
    HEALTH_STATUS_COL, HEALTH_TIME_COL,
    MAIN_DATA_STATUS_COL, MAIN_DATA_TIME_COL,
    EXTRA_DATA_STATUS_COL, EXTRA_DATA_TIME_COL,
    MAIN_CATEGORY_STATUS_COL, MAIN_CATEGORY_TIME_COL,
    PLUGIN_STATUS_COL, PLUGIN_TIME_COL,
    MEDIA_STATUS_COL, MEDIA_TIME_COL,
    REPORT_STATUS_COL, REPORT_TIME_COL,
    DOMAIN_RESOLVED_TIME_COL, EXTRA_COLUMNS, SCHEDULE_ENABLED_COL
)
from datastore import DataStore
from domain_reporter_client import DomainReporter
from erp_builder import ERPBuilder
from health_checker import healthcheck_domain
from main_category_uploader_v2 import MainCategoryUploader
from main_data_uploader import MainDataUploader
from wp_media_config import WPMediaConfigurator, DEFAULT_MEDIA_ROOT
from wp_plugin_button_clicker import WpPluginButtonClicker

app = Flask(__name__)
app.secret_key = 'v2-super-secret-key-2025'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads_v2'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('templates_v2', exist_ok=True)

TASKS = {}
TASK_LOCK = threading.Lock()


@app.route('/')
@app.route('/sites')
def sites_list_route():
    return sites_list()


@app.route('/reported')
def reported_list_route():
    return reported_list()


@app.route('/built')
def built_list_route():
    return built_list()


@app.route('/import', methods=['GET', 'POST'])
def import_excel_route():
    return import_excel()


@app.route('/config', methods=['GET', 'POST'])
def config_route():
    return config()


@app.route('/tasks')
def tasks_list_route():
    return tasks_list()


@app.route('/api/tasks')
def api_tasks_route():
    return api_tasks()


@app.route('/site/<int:site_id>/report')
def report_domain_route(site_id):
    return report_domain(site_id)


@app.route('/site/<int:site_id>/build')
def build_site_route(site_id):
    return build_site(site_id)


@app.route('/health/<int:site_id>')
def health_check_route(site_id):
    return health_check(site_id)


@app.route('/site/<int:site_id>/delete')
def delete_site_route(site_id):
    return delete_site(site_id)


@app.route('/sites/delete-selected', methods=['POST'])
def delete_selected_route():
    return delete_selected()


@app.route('/tasks/report-selected', methods=['POST'])
def task_report_selected_route():
    return task_report_selected()


@app.route('/tasks/refresh-reported', methods=['POST'])
def task_refresh_reported_route():
    return task_refresh_reported()


@app.route('/tasks/delete-reported', methods=['POST'])
def task_delete_reported_route():
    return task_delete_reported()


@app.route('/tasks/build-selected', methods=['POST'])
def task_build_selected_route():
    return task_build_selected()


@app.route('/tasks/health-selected', methods=['POST'])
def task_health_selected_route():
    return task_health_selected()


@app.route('/tasks/upload-main', methods=['POST'])
def task_upload_main_route():
    return task_upload_main()


@app.route('/tasks/upload-extra', methods=['POST'])
def task_upload_extra_route():
    return task_upload_extra()


@app.route('/tasks/upload-main-category', methods=['POST'])
def task_upload_main_category_route():
    return task_upload_main_category()


@app.route('/tasks/configure-sites', methods=['POST'])
def task_configure_sites_route():
    return task_configure_sites()


@app.route('/tasks/clear-cache-selected', methods=['POST'])
def task_clear_cache_selected_route():
    return task_clear_cache_selected()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
