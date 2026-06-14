import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from constants import COLUMNS, DB_PATH, TABLE_NAME
from datastore import DataStore


UPLOAD_MAX_WORKERS = 10


def main():
    if len(sys.argv) < 4:
        print("用法: python upload_worker.py <上传类型> <站点ID列表> <WP密码>")
        print("上传类型: main, extra, main_category")
        return

    upload_type = sys.argv[1]
    site_ids = json.loads(sys.argv[2])
    wp_password = sys.argv[3]

    print("=" * 60)
    print(f"开始执行 {upload_type} 上传")
    print(f"站点数量: {len(site_ids)}")
    print("=" * 60)

    store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, None, None)
    try:
        upload_tasks = build_upload_tasks(store, upload_type, site_ids)
        if not upload_tasks:
            print("没有可执行的上传任务")
            return

        print(f"\n准备上传 {len(upload_tasks)} 个站点\n")
        max_workers = min(UPLOAD_MAX_WORKERS, len(upload_tasks))
        print(f"线程池上限 {UPLOAD_MAX_WORKERS}，本次实际启用 {max_workers} 个线程\n")

        results = run_upload_tasks(upload_type, upload_tasks, wp_password, max_workers)
        summarize_results(store, upload_type, results)
    finally:
        store.close()

    print("\n按回车键关闭窗口...")
    input()


def build_upload_tasks(store, upload_type, site_ids):
    upload_tasks = []
    for row_id in site_ids:
        row = store.get_row(row_id)
        if not row:
            continue

        domain = (row.get("domain") or "").strip()
        if upload_type == "main":
            payload = (row.get("main_data_source_id") or "").strip()
            if not domain or not payload:
                print(f"[跳过] {domain or row_id}: 缺少域名或主数据源ID")
                continue
        elif upload_type == "extra":
            payload = (row.get("extra_data_source_id") or "").strip()
            if not domain or not payload:
                print(f"[跳过] {domain or row_id}: 缺少域名或补充数据源ID")
                continue
        elif upload_type == "main_category":
            payload = (row.get("main_category") or "").strip()
            if not domain or not payload:
                print(f"[跳过] {domain or row_id}: 缺少域名或主分类")
                continue
        else:
            print(f"[跳过] {row_id}: 不支持的上传类型 {upload_type}")
            continue

        upload_tasks.append((row_id, domain, payload))

    return upload_tasks


def run_upload_tasks(upload_type, upload_tasks, wp_password, max_workers):
    results = []
    lock = threading.Lock()

    def progress_callback_factory(domain):
        def progress_callback(message):
            with lock:
                print(f"[{domain}] {message}")

        return progress_callback

    def upload_single_task(task):
        row_id, domain, payload = task
        progress_callback = progress_callback_factory(domain)
        try:
            if upload_type == "main":
                from main_data_uploader import MainDataUploader

                uploader = MainDataUploader(wp_password)
                result = uploader.upload_main_data(domain, payload, "0", progress_callback) or {}
                return row_id, domain, result, None

            if upload_type == "extra":
                from extra_data_uploader import ExtraDataUploader

                uploader = ExtraDataUploader(wp_password)
                result = uploader.upload_extra_data(domain, payload, "0", progress_callback) or {}
                return row_id, domain, result, None

            if upload_type == "main_category":
                from main_category_uploader_v2 import MainCategoryUploader

                uploader = MainCategoryUploader(domain)
                result = uploader.upload_main_category(domain, payload, progress_callback)
                return row_id, domain, result, None

            return row_id, domain, 0, f"不支持的上传类型: {upload_type}"
        except Exception as exc:
            return row_id, domain, 0, str(exc)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_single_task, task) for task in upload_tasks]
        for future in as_completed(futures):
            results.append(future.result())

    return results


def summarize_results(store, upload_type, results):
    success_total = 0
    failed_total = 0
    failed_details = []

    print("\n" + "=" * 60)
    print("上传结果统计")
    print("=" * 60)

    for row_id, domain, result, error in results:
        success_flag = False
        success_message = ""

        if upload_type == "main":
            result = result or {}
            success_count = int(result.get("upload_success", 0) or 0)
            repeat_count = int(result.get("repeat_count", 0) or 0)
            completed = bool(result.get("completed"))
            success_flag = error is None and (completed or success_count > 0 or repeat_count > 0)
            success_message = f"上传完成 (新增{success_count}条, 重复{repeat_count}条)"
        elif upload_type == "extra":
            result = result or {}
            success_count = int(result.get("upload_success", 0) or 0)
            repeat_count = int(result.get("repeat_count", 0) or 0)
            completed = bool(result.get("completed"))
            success_flag = error is None and (completed or success_count > 0 or repeat_count > 0)
            success_message = f"上传完成 (新增{success_count}条, 重复{repeat_count}条)"
        elif upload_type == "main_category":
            success_flag = error is None and result == 1
            success_message = "主分类设置成功"

        if success_flag:
            mark_success(store, upload_type, row_id)
            print(f"[成功] {domain}: {success_message}")
            success_total += 1
            continue

        failed_total += 1
        detail = f"{domain}: {error or '上传失败'}"
        failed_details.append(detail)
        print(f"[失败] {detail}")

    print("\n" + "=" * 60)
    print(f"总结: 成功 {success_total}, 失败 {failed_total}")
    print("=" * 60)

    if failed_details:
        print("\n失败详情:")
        for detail in failed_details:
            print(f"  - {detail}")


def mark_success(store, upload_type, row_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if upload_type == "main":
        store.update_fields(
            row_id,
            {
                "main_data_status": "已上传",
                "main_data_time": now,
            },
        )
        return

    if upload_type == "extra":
        store.update_fields(
            row_id,
            {
                "extra_data_status": "已上传",
                "extra_data_time": now,
            },
        )
        return

    if upload_type == "main_category":
        store.update_fields(
            row_id,
            {
                "main_category_status": "已上传",
                "main_category_time": now,
            },
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n发生错误: {exc}")
        traceback.print_exc()
        print("\n按回车键关闭窗口...")
        input()
