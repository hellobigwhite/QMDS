"""产品数据管理路由"""

import json
import os
import re
import time
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from qmds.config import settings
from qmds.config.categories import parse_collection_prefix, normalize_subcategory
from qmds.config.llm_models import DEFAULT_AGENTROUTER_MODEL, list_llm_models
from qmds.modules.web.db_helpers import get_mongo_db, get_product_db, get_site_db
from qmds.modules.web.services.category_tasks import (
    resolve_category_list as _resolve_category_list,
    run_category_optimize_task,
    run_category_process_task,
    parse_subcategory_form,
    subcategory_display,
)
from qmds.modules.web.services.data_allocator import (
    MAX_API_CATEGORIES,
    count_excel_categories,
    resolve_export_file,
    run_allocation_task,
)
from qmds.modules.web.services.site_info_generator import (
    INFO_FILE_NAME,
    _write_info_excel,
    read_site_info_excel,
    repair_row_major_category,
    run_batch_site_info_task,
)
from qmds.modules.web.services.site_review import (
    apply_site_review_task,
    is_site_applied,
    locate_info_file,
    locate_site_folder,
    repair_row_main_category,
)
from qmds.modules.web.services.site_uploader import resolve_export_target
from qmds.utils.agentrouter_client import (
    DEFAULT_AGENTROUTER_BASE_URL,
    fetch_agentrouter_models,
)
from qmds.modules.web.task_manager import make_progress_callback, task_manager
from qmds.utils import winpath
from qmds.utils.logger import get_logger

log = get_logger("web.product_data")

bp = Blueprint("product_data", __name__)


@bp.route("/product-data")
def product_data():
    return redirect(url_for("product_data.product_data_overview"))


@bp.route("/api/product-data/stats")
def api_product_data_stats():
    try:
        product_db = get_product_db()
        stats = product_db.get_all_stats()
        return jsonify({"ok": True, "data": stats})
    except Exception as e:
        log.error(f"获取产品数据统计失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/product-data/simple-stats")
def api_product_data_simple_stats():
    try:
        product_db = get_product_db()
        stats = product_db.get_simple_all_stats()
        return jsonify({"ok": True, "data": stats})
    except Exception as e:
        log.error(f"获取简单统计失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/product-data/subcategories")
def api_product_data_subcategories():
    """获取指定一级分类下的所有二级分类列表"""
    category = request.args.get("category", "").strip()
    if not category:
        return jsonify({"ok": False, "error": "缺少 category 参数"}), 400
    try:
        product_db = get_product_db()
        cat_list = product_db.list_categories_with_sub()
        subcategories = [
            {"subcategory": item["subcategory"], "prefix": item["prefix"]}
            for item in cat_list if item["category"] == category
        ]
        return jsonify({"ok": True, "data": subcategories})
    except Exception as e:
        log.error(f"获取二级分类列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/shopify/filtered-subcategories")
def api_shopify_filtered_subcategories():
    """获取指定一级分类下所有有 filtered 数据的二级分类列表"""
    category = request.args.get("category", "").strip()
    if not category:
        return jsonify({"ok": False, "error": "缺少 category 参数"}), 400
    try:
        source_db = get_mongo_db()
        subs = source_db.list_filtered_subcategories(category)
        return jsonify({"ok": True, "data": subs})
    except Exception as e:
        log.error(f"获取 filtered 二级分类列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/product-data/overview")
def product_data_overview():
    try:
        product_db = get_product_db()
        stats = product_db.get_all_stats()

        source_db = get_mongo_db()
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
                               collections=[],
                               category_stats=stats["categories"],
                               filtered_categories=filtered_categories)
    except Exception as e:
        log.error(f"获取产品数据失败: {e}")
        return render_template("product_overview.html",
                               total_collections=0, non_empty_collections=0,
                               total_rows=0, total_clean_rows=0,
                               total_exported=0, total_unclean=0, total_cleaned=0, total_failed=0,
                               collections=[], category_stats=[], filtered_categories=[],
                               error=str(e))


@bp.route("/product-data/crawl", methods=["GET", "POST"])
def product_data_crawl():
    source_db = get_mongo_db()
    filtered_categories = source_db.list_filtered_categories()

    if request.method == "POST":
        category = request.form.get("category", "").strip()
        subcategory = request.form.get("subcategory", "").strip()
        max_sites = int(request.form.get("max_sites", 0))
        max_workers = int(request.form.get("max_workers", 10))

        if not category:
            flash("请选择类目", "error")
            return redirect(url_for("product_data.product_data_crawl"))

        crawl_all = subcategory == "__all__"
        sub_display = "全部二级分类" if crawl_all else (subcategory if subcategory else "other")
        task_id = f"crawl_{category}_{'all' if crawl_all else sub_display}_{int(time.time())}"
        task_manager.create(task_id, "crawl_products", f"{category}/{sub_display}")

        def run_task():
            crawler = None
            try:
                task_manager.update(task_id, status="running",
                                    message=f"开始爬取: {category}/{sub_display} ({max_workers} 线程)")
                task_manager.add_log(task_id, f"任务启动: 爬取分类 {category}/{sub_display}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                from qmds.modules.data_scraper.product_crawler import create_crawler
                crawler = create_crawler()
                task_manager.add_log(task_id, "爬取器创建成功", "info")

                if crawl_all:
                    result = crawler.crawl_category_all_subcategories(
                        category, max_sites=max_sites,
                        workers=max_workers,
                        progress_callback=make_progress_callback(task_id),
                        stop_event=task_manager.get_stop_event(task_id),
                    )
                else:
                    result = crawler.crawl_category(
                        category, max_sites=max_sites,
                        workers=max_workers,
                        progress_callback=make_progress_callback(task_id),
                        stop_event=task_manager.get_stop_event(task_id),
                        subcategory=subcategory,
                    )

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, status="completed",
                                    message=f"完成: 爬取 {result['success_sites']}/{result['total_sites']} 个站点, {result['total_products']} 件商品",
                                    progress=100)
                task_manager.add_log(task_id, f"任务完成: 成功 {result['success_sites']}/{result['total_sites']} 个站点", "info")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
            except Exception as e:
                log.error(f"爬取任务失败: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"任务失败: {e}", "error")
            finally:
                if crawler:
                    crawler.close()

        task_manager.start_task_thread(task_id, run_task)
        flash(f"数据爬取任务已启动: {category}/{sub_display}", "info")
        return redirect(url_for("product_data.product_data_crawl"))

    return render_template("product_crawl.html", filtered_categories=filtered_categories)


@bp.route("/product-data/clean", methods=["GET", "POST"])
def product_data_clean():
    if request.method == "POST":
        category = request.form.get("category", "__all__")
        subcategory = request.form.get("subcategory", "__all__").strip() or "__all__"
        force = request.form.get("force") == "1"
        regenerate_sku = request.form.get("regenerate_sku") == "1"
        sub_display = subcategory if subcategory != "__all__" else "all"
        task_id = f"clean_{category}_{sub_display}_{int(time.time())}"
        task_manager.create(task_id, "clean_products", f"{category}/{sub_display}")

        def run_task():
            product_db = None
            try:
                from qmds.db.product_db import ProductDBClient
                force_msg = "（强制模式）" if force else ""
                sku_msg = "（重新编号SKU）" if regenerate_sku else ""
                task_manager.update(task_id, status="running", message=f"开始清洗: {category}/{sub_display}{force_msg}{sku_msg}")
                task_manager.add_log(task_id, f"任务启动: 清洗数据 {category}/{sub_display}{force_msg}{sku_msg}", "info")

                product_db = ProductDBClient()
                cat_list = _resolve_category_list(product_db, category, subcategory)
                task_manager.add_log(task_id, f"获取到 {len(cat_list)} 个分类", "info")

                total_processed = 0
                total_cleaned = 0
                total_removed = 0

                for item in cat_list:
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped",
                                            message=f"已停止: 已处理 {total_processed} 条数据")
                        return

                    cat = item["category"]
                    sub = item.get("subcategory", "")
                    sub_d = sub if sub else "other"
                    task_manager.update(task_id, message=f"清洗分类: {cat}/{sub_d}")
                    task_manager.add_log(task_id, f"开始清洗分类: {cat}/{sub_d}", "info")

                    result = product_db.clean_category(cat, sub, force=force,
                                                        regenerate_sku=regenerate_sku)
                    total_processed += result["processed"]
                    total_cleaned += result["cleaned"]
                    total_removed += result["removed"]

                    task_manager.add_log(task_id,
                                         f"分类 {cat}/{sub_d}: 处理 {result['processed']} 条, 通过 {result['cleaned']} 条, 移除 {result['removed']} 条", "info")

                    if result.get("sku_generated"):
                        task_manager.add_log(task_id, f"  ├─ 新SKU: {result['sku_generated']} 条", "info")

                    filter_stats = result.get("stats", {})
                    for reason, count in filter_stats.items():
                        if count > 0:
                            task_manager.add_log(task_id, f"  ├─ {reason}: {count} 条", "info")

                task_manager.update(task_id, status="completed",
                                    message=f"完成: 处理 {total_processed} 条, 清洗后 {total_cleaned} 条, 移除 {total_removed} 条",
                                    progress=100)
                task_manager.add_log(task_id, f"任务完成", "info")
            except Exception as e:
                import traceback
                log.error(f"清洗任务失败: {e}\n{traceback.format_exc()}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"任务失败: {e}", "error")
            finally:
                if product_db:
                    product_db.close()

        task_manager.start_task_thread(task_id, run_task)
        flash(f"数据清洗任务已启动: {category}/{sub_display}", "info")
        return redirect(url_for("product_data.product_data_clean"))

    return render_template("product_clean.html", category_stats=[])


@bp.route("/product-data/delete-cleaned-raw", methods=["POST"])
def product_data_delete_cleaned_raw():
    category = request.form.get("category", "").strip()
    subcategory = request.form.get("subcategory", "__all__").strip() or "__all__"
    if not category:
        flash("请指定类目", "error")
        return redirect(url_for("product_data.product_data_clean"))

    sub_display = subcategory if subcategory != "__all__" else "all"
    task_id = f"delete_cleaned_{category}_{sub_display}_{int(time.time())}"
    task_manager.create(task_id, "delete_cleaned_raw", f"{category}/{sub_display}")

    def run_task():
        product_db = None
        try:
            task_manager.update(task_id, status="running",
                                message=f"开始从 {category}/{sub_display} 删除已处理数据")
            task_manager.add_log(task_id, f"任务启动", "info")

            from qmds.db.product_db import ProductDBClient
            product_db = ProductDBClient()
            cat_list = _resolve_category_list(product_db, category, subcategory)
            total_deleted = 0

            for item in cat_list:
                cat = item["category"]
                sub = item.get("subcategory", "")
                sub_d = sub if sub else "other"
                deleted = product_db.delete_cleaned_from_raw(cat, sub)
                total_deleted += deleted
                task_manager.add_log(task_id, f"从 {cat}/{sub_d} 删除 {deleted} 条已处理数据", "info")

            task_manager.update(task_id, status="completed",
                                message=f"完成: 共删除 {total_deleted} 条已处理数据",
                                progress=100)
            task_manager.add_log(task_id, f"任务完成: 共删除 {total_deleted} 条数据", "info")
        except Exception as e:
            import traceback
            log.error(f"删除已清洗数据失败: {e}\n{traceback.format_exc()}")
            task_manager.update(task_id, status="failed", message=f"失败: {e}")
            task_manager.add_log(task_id, f"任务失败: {e}", "error")
        finally:
            if product_db:
                product_db.close()

    task_manager.start_task_thread(task_id, run_task)
    flash(f"删除任务已启动: {category}/{sub_display}", "info")
    return redirect(url_for("product_data.product_data_clean"))


@bp.route("/product-data/export", methods=["GET", "POST"])
def product_data_export():
    if request.method == "POST":
        category = request.form.get("category", "").strip()
        subcategory = request.form.get("subcategory", "__all__").strip() or "__all__"
        export_format = request.form.get("format", "excel")
        merge_export = request.form.get("merge_export") == "1"
        limit = request.form.get("limit", "").strip()
        limit = int(limit) if limit and limit.isdigit() else None

        if not category:
            flash("请选择要导出的类目", "error")
            return redirect(url_for("product_data.product_data_export"))

        sub_display = subcategory if subcategory != "__all__" else "all"
        mode_label = "合并导出" if merge_export else "导出"
        task_id = f"{'merge_export' if merge_export else 'export'}_{category}_{sub_display}_{int(time.time())}"
        task_manager.create(task_id, "export_products", f"{mode_label}: {category}/{sub_display}")

        def run_task():
            product_db = None
            try:
                limit_msg = f"（限制 {limit} 条）" if limit else ""
                task_manager.update(task_id, status="running", message=f"开始{mode_label}: {category}/{sub_display}{limit_msg}")
                task_manager.add_log(task_id, f"任务启动: {mode_label}数据 {category}/{sub_display}{limit_msg}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                from qmds.db.product_db import ProductDBClient
                product_db = ProductDBClient()
                export_dir = str(settings.data_dir / "exports")
                task_manager.add_log(task_id, f"导出目录: {export_dir}", "info")

                if merge_export:
                    # 合并导出：将一级分类下所有二级分类合并到一个 Excel
                    result = product_db.merge_export_category(
                        category, export_dir, limit=limit,
                        progress_callback=make_progress_callback(task_id)
                    )
                    if result:
                        filepath = result["filepath"]
                        count = result["count"]
                        dedup_count = result.get("dedup_count", 0)
                        sub_count = result.get("sub_count", 0)
                        marked = result.get("marked_count", 0)
                        task_manager.add_log(
                            task_id,
                            f"合并导出 {category}: {sub_count} 个二级分类, {count} 条 (去重 {dedup_count}), 标记 {marked} 条 -> {os.path.basename(filepath)}",
                            "info"
                        )
                        total_count = count
                    else:
                        task_manager.add_log(task_id, f"合并导出 {category}: 无清洗后数据", "info")
                        total_count = 0
                else:
                    # 普通导出：按分类分别导出
                    cat_list = _resolve_category_list(product_db, category, subcategory)
                    total_count = 0

                    for item in cat_list:
                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped", message="任务已停止")
                            return

                        cat = item["category"]
                        sub = item.get("subcategory", "")
                        sub_d = sub if sub else "other"
                        task_manager.update(task_id, message=f"导出分类: {cat}/{sub_d}")

                        export_result = product_db.export_category_to_excel(
                            cat, sub, export_dir, limit=limit,
                            progress_callback=make_progress_callback(task_id)
                        )

                        if export_result:
                            filepath = export_result["filepath"]
                            count = export_result["count"]
                            total_count += count
                            task_manager.add_log(task_id, f"导出 {cat}/{sub_d}: {count} 条 -> {os.path.basename(filepath)}", "info")
                        else:
                            task_manager.add_log(task_id, f"导出 {cat}/{sub_d}: 无清洗后数据", "info")

                task_manager.update(task_id, status="completed",
                                    message=f"完成: 共{mode_label} {total_count} 条数据",
                                    progress=100)
                task_manager.add_log(task_id, f"任务完成: 共{mode_label} {total_count} 条", "info")
            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
            except Exception as e:
                log.error(f"导出任务失败: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"任务失败: {e}", "error")
            finally:
                if product_db:
                    product_db.close()

        task_manager.start_task_thread(task_id, run_task)
        flash(f"数据{mode_label}任务已启动: {category}/{sub_display}", "info")
        return redirect(url_for("product_data.product_data_export"))

    # AgentRouter 模型列表缓存（页面加载直接展示，无需每次连接平台获取）
    ar_cache = {"models": [], "fetched_at": ""}
    ar_saved_model = ""
    try:
        site_db = get_site_db()
        ar_saved_model = (site_db.get_setting("agentrouter_model", "")
                          or settings.agentrouter_model or "").strip()
        ar_base = (site_db.get_setting("agentrouter_base_url", "")
                   or settings.agentrouter_base_url
                   or DEFAULT_AGENTROUTER_BASE_URL).strip()
        ar_cache = _load_ar_models_cache(site_db, ar_base)
    except Exception as e:
        log.warning(f"读取 AgentRouter 模型缓存失败: {e}")

    return render_template("product_export.html", category_stats=[],
                           llm_models=list_llm_models(),
                           default_info_model=DEFAULT_AGENTROUTER_MODEL,
                           agentrouter_models=ar_cache["models"],
                           agentrouter_models_fetched_at=ar_cache["fetched_at"],
                           agentrouter_saved_model=ar_saved_model)


AR_MODELS_CACHE_KEY = "agentrouter_models_cache"


def _load_ar_models_cache(site_db, base_url: str) -> dict:
    """读取已缓存的 AgentRouter 模型列表（site_db）

    base_url 与缓存时不一致则视为无缓存（不同网关的模型列表不同）。
    返回 {"models": [...], "fetched_at": "..."}。
    """
    import json

    raw = (site_db.get_setting(AR_MODELS_CACHE_KEY, "") or "").strip()
    if not raw:
        return {"models": [], "fetched_at": ""}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"models": [], "fetched_at": ""}
    if not isinstance(data, dict):
        return {"models": [], "fetched_at": ""}
    if ((data.get("base_url") or "").rstrip("/")
            != (base_url or "").rstrip("/")):
        return {"models": [], "fetched_at": ""}
    models = [str(m) for m in (data.get("models") or []) if str(m).strip()]
    return {"models": models, "fetched_at": str(data.get("fetched_at") or "")}


def _save_ar_models_cache(site_db, models: list, base_url: str) -> str:
    """缓存 AgentRouter 模型列表到 site_db（页面加载时直接使用，不重新连接平台）

    返回缓存时间戳字符串。
    """
    import json

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    site_db.set_setting(AR_MODELS_CACHE_KEY, json.dumps({
        "models": list(models),
        "base_url": (base_url or "").strip(),
        "fetched_at": fetched_at,
    }, ensure_ascii=False))
    return fetched_at


@bp.route("/api/product-data/agentrouter/models", methods=["GET", "POST"])
def api_agentrouter_models():
    """真实连接 AgentRouter 平台，拉取实际可用模型列表（GET {base_url}/models）

    - GET：使用已保存的配置（site_db 设置，回退 .env）；
    - POST：可携带配置页中已填写但尚未保存的 api_key / base_url（JSON body），
      便于「先验证连接，再保存配置」。
    模型列表不硬编码，保证与平台真实可用模型一致。
    拉取成功后自动缓存到 site_db（agentrouter_models_cache），页面加载时
    直接读缓存展示，无需每次重新获取；本接口保留用于手动更新缓存。
    """
    try:
        site_db = get_site_db()
        api_key = (site_db.get_setting("agentrouter_api_key", "")
                   or settings.agentrouter_api_key or "").strip()
        base_url = (site_db.get_setting("agentrouter_base_url", "")
                    or settings.agentrouter_base_url
                    or DEFAULT_AGENTROUTER_BASE_URL).strip()

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            api_key = (str(body.get("api_key") or "").strip()) or api_key
            base_url = (str(body.get("base_url") or "").strip()) or base_url

        if not api_key:
            return jsonify({"ok": False,
                            "error": "未配置 AgentRouter API Key（请先在「配置」页填写并保存）"}), 400

        models = fetch_agentrouter_models(api_key, base_url)
        # 成功后更新缓存（含拉取时间），页面加载时直接使用
        fetched_at = _save_ar_models_cache(site_db, models, base_url)
        saved_model = (site_db.get_setting("agentrouter_model", "")
                       or settings.agentrouter_model or "").strip()
        return jsonify({"ok": True, "data": {
            "models": models,
            "base_url": base_url,
            "saved_model": saved_model,
            "fetched_at": fetched_at,
        }})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        log.error(f"获取 AgentRouter 模型列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


@bp.route("/api/product-data/export-folders")
def api_export_folders():
    """获取导出文件夹列表（包含xlsx文件的文件夹）"""
    try:
        export_dir = settings.data_dir / "exports"
        if not export_dir.exists():
            return jsonify({"ok": True, "data": []})

        folders = []
        # 深层分配输出（分类文件夹）路径可能超过 Windows 260 字符，
        # os.walk 配合扩展长度前缀才能完整枚举
        top = winpath.long_path(export_dir)
        for root, dirs, files in os.walk(top):
            xlsx_files = [f for f in files if f.endswith('.xlsx')
                          and not f.startswith('merged_') and not f.startswith('~$')]
            if xlsx_files:
                rel_path = os.path.relpath(root, top)
                folders.append({
                    "path": rel_path,
                    "name": rel_path.replace('\\', '/'),
                    "file_count": len(xlsx_files)
                })

        folders.sort(key=lambda x: x["name"], reverse=True)
        return jsonify({"ok": True, "data": folders})
    except Exception as e:
        log.error(f"获取导出文件夹失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/product-data/export-files")
def api_export_files():
    """获取指定文件夹中的Excel文件列表"""
    try:
        folder = request.args.get("folder", "").strip()
        if not folder:
            return jsonify({"ok": False, "error": "请指定文件夹"}), 400

        export_dir = settings.data_dir / "exports"
        folder_path = export_dir / folder

        if not winpath.is_dir(folder_path):
            return jsonify({"ok": False, "error": "文件夹不存在"}), 404

        # os.scandir + 扩展长度前缀：深层文件夹内的文件路径可能超过
        # Windows 260 字符，iterdir/stat 对超长路径会抛错或漏文件
        files = []
        with os.scandir(winpath.long_path(folder_path)) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            # 跳过 Excel 打开时产生的 ~$ 临时锁文件
            if not e.name.lower().endswith('.xlsx') or e.name.startswith('~$'):
                continue
            if not e.is_file():
                continue
            st = e.stat()  # DirEntry 自带信息，超长路径可用
            files.append({
                "name": e.name,
                "size": st.st_size,
                "size_mb": round(st.st_size / 1024 / 1024, 2)
            })

        return jsonify({"ok": True, "data": files})
    except Exception as e:
        log.error(f"获取文件列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/product-data/merge", methods=["POST"])
def product_data_merge():
    """合并选中的Excel文件（按标题去重）"""
    try:
        folder = request.form.get("folder", "").strip()
        files = request.form.getlist("files")

        if not folder:
            flash("请选择文件夹", "error")
            return redirect(url_for("product_data.product_data_export"))

        if not files:
            flash("请选择要合并的文件", "error")
            return redirect(url_for("product_data.product_data_export"))

        export_dir = settings.data_dir / "exports"
        folder_path = export_dir / folder

        if not folder_path.exists():
            flash("文件夹不存在", "error")
            return redirect(url_for("product_data.product_data_export"))

        # 合并任务
        folder_safe = folder.replace('/', '_').replace('\\', '_')
        task_id = f"merge_{folder_safe}_{int(time.time())}"
        task_manager.create(task_id, "merge_tables", f"合并 {folder}")

        def run_merge_task():
            try:
                import pandas as pd

                all_dfs = []
                total_rows = 0

                for i, filename in enumerate(files):
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        return

                    filepath = folder_path / filename
                    if not winpath.exists(filepath):
                        task_manager.add_log(task_id, f"跳过不存在的文件: {filename}", "warning")
                        continue

                    task_manager.add_log(task_id, f"读取: {filename}", "info")
                    # 深层文件夹内的文件路径可能超过 Windows 260 字符
                    df = pd.read_excel(winpath.long_path(filepath), engine="openpyxl")
                    all_dfs.append(df)
                    total_rows += len(df)

                    progress = int((i + 1) / len(files) * 50)
                    task_manager.update(task_id, progress=progress)

                if not all_dfs:
                    task_manager.update(task_id, status="failed", message="没有有效数据可合并")
                    return

                task_manager.add_log(task_id, f"合并 {len(all_dfs)} 个文件，共 {total_rows} 行", "info")

                # 合并所有数据
                merged_df = pd.concat(all_dfs, ignore_index=True)
                task_manager.add_log(task_id, f"合并后: {len(merged_df)} 行", "info")

                # 按名称去重（兼容 BB 新导出的 Name 列与历史导出的标题列）
                dedup_col = None
                for col in ("Name", "标题", "name", "title"):
                    if col in merged_df.columns:
                        dedup_col = col
                        break
                if dedup_col:
                    before_dedup = len(merged_df)
                    merged_df = merged_df.drop_duplicates(subset=[dedup_col], keep="first")
                    dedup_count = before_dedup - len(merged_df)
                    task_manager.add_log(task_id, f"按{dedup_col}去重: 移除 {dedup_count} 条重复，剩余 {len(merged_df)} 条", "info")
                else:
                    task_manager.add_log(task_id, "未找到名称列（Name/标题），跳过去重", "warning")

                # 保存合并后的文件
                output_filename = f"merged_{folder_safe}.xlsx"
                output_path = folder_path / output_filename

                merged_df.to_excel(winpath.long_path(output_path), index=False,
                                   engine="openpyxl")
                task_manager.add_log(task_id, f"保存: {output_filename}", "info")

                task_manager.update(task_id, status="completed",
                                   message=f"完成: 合并 {len(merged_df)} 条数据，去重 {dedup_count} 条",
                                   progress=100)
                task_manager.add_log(task_id, "合并任务完成", "info")

            except InterruptedError:
                task_manager.update(task_id, status="stopped", message="任务已停止")
            except Exception as e:
                log.error(f"合并任务失败: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"任务失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_merge_task)
        flash(f"表格合并任务已启动: {folder}", "info")
        return redirect(url_for("product_data.product_data_export"))

    except Exception as e:
        log.error(f"合并请求处理失败: {e}")
        flash(f"合并失败: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))


@bp.route("/api/product-data/file-categories")
def api_product_data_file_categories():
    """获取指定导出表格中的分类统计（用于数据分配）"""
    folder = request.args.get("folder", "").strip()
    filename = request.args.get("file", "").strip()
    if not folder or not filename:
        return jsonify({"ok": False, "error": "请指定文件夹和表格文件"}), 400
    try:
        file_path = resolve_export_file(folder, filename)
        col, total, cats = count_excel_categories(file_path)
        truncated = len(cats) > MAX_API_CATEGORIES
        return jsonify({"ok": True, "data": {
            "category_column": col,
            "total_rows": total,
            "truncated": truncated,
            "categories": [
                {"category": c, "count": n}
                for c, n in (cats[:MAX_API_CATEGORIES] if truncated else cats)
            ],
        }})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        log.error(f"获取表格分类统计失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def _parse_portion_size(raw, default, min_value=1):
    """解析数值参数（非法值回退默认值）"""
    try:
        value = int(str(raw).strip())
        return value if value >= min_value else default
    except (TypeError, ValueError):
        return default


@bp.route("/product-data/allocate", methods=["POST"])
def product_data_allocate():
    """数据分配：主分类单独成表 + 剩余数据按分类打包为补充表分配给各主分类"""
    folder = request.form.get("folder", "").strip()
    filename = request.form.get("file", "").strip()
    main_categories = [c for c in request.form.getlist("main_categories") if c.strip()]
    min_size = _parse_portion_size(request.form.get("min_size"), 40000)
    max_size = _parse_portion_size(request.form.get("max_size"), 50000)
    # 大分类拆分阈值：数量超过该值的分类将拆分后均匀分配到各补充表
    split_threshold = _parse_portion_size(request.form.get("split_threshold"), 3000,
                                          min_value=0)

    # 分配后批量拆表选项（移植自 BB 批量拆表工具）
    # 主数据表格不设上限；补充数据按 supp_rows_per_file 拆分（默认 5000）
    split_options = {
        "enabled": request.form.get("split_enabled") == "on",
        "supp_rows_per_file": _parse_portion_size(request.form.get("supp_rows_per_file"),
                                                  5000),
        "suffix_mode": request.form.get("split_suffix_mode", "none"),
        "custom_suffix": (request.form.get("split_custom_suffix") or "").strip(),
        "remove_source": request.form.get("split_remove_source") != "off",
    }
    if split_options["suffix_mode"] not in ("none", "custom", "part"):
        split_options["suffix_mode"] = "none"
    if split_options["suffix_mode"] != "custom":
        split_options["custom_suffix"] = None

    if not folder or not filename:
        flash("请选择文件夹和表格文件", "error")
        return redirect(url_for("product_data.product_data_export"))
    if not main_categories:
        flash("请选择至少一个主分类", "error")
        return redirect(url_for("product_data.product_data_export"))
    if max_size <= min_size:
        flash("每份条数范围无效：最多条数必须大于最少条数", "error")
        return redirect(url_for("product_data.product_data_export"))

    try:
        file_path = resolve_export_file(folder, filename)
    except FileNotFoundError as e:
        flash(f"文件不存在或已移动: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))

    task_id = f"allocate_{file_path.stem}_{int(time.time())}"
    task_manager.create(task_id, "data_allocate", f"数据分配: {filename}")

    task_manager.start_task_thread(
        task_id,
        lambda: run_allocation_task(task_id, file_path, main_categories,
                                    min_size, max_size, split_threshold,
                                    split_options))
    msg = f"数据分配任务已启动: {filename}（{len(main_categories)} 个主分类）"
    if split_options["enabled"]:
        msg += f"，分配完成后将批量拆表（主数据不设上限，补充数据每份 {split_options['supp_rows_per_file']} 条）"
    msg += "，拆表后自动统计每个网站数据的分类结构（分类统计.xlsx）"
    flash(msg, "info")
    return redirect(url_for("product_data.product_data_export"))


@bp.route("/product-data/site-info", methods=["POST"])
def product_data_site_info():
    """批量 AI 生成网站信息：遍历所选文件夹下所有「最后一层文件夹」
    （每个 = 一个网站的数据，数据分配后每个主分类一个文件夹），
    按顺序逐个调用 LLM（默认 AgentRouter 平台模型）生成
    域名/标题/描述/地址/关键词 -> 所选文件夹下的 网站信息.xlsx（每行一个网站）"""
    folder = request.form.get("folder", "").strip()
    model = request.form.get("model", "").strip()
    model_id_override = (request.form.get("model_id") or "").strip()
    # 下拉框选择「手动输入其他模型 ID」时，取配套文本框的值
    if model_id_override == "__custom__":
        model_id_override = (request.form.get("model_id_manual") or "").strip()

    if not folder:
        flash("请选择数据文件夹", "error")
        return redirect(url_for("product_data.product_data_export"))
    try:
        folder_path = resolve_export_target(folder, "")
    except FileNotFoundError as e:
        flash(f"文件夹不存在: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))

    task_id = f"site_info_{re.sub(r'[^\w-]+', '_', folder)}_{int(time.time())}"
    task_manager.create(task_id, "site_info", f"批量AI生成网站信息: {folder}")

    task_manager.start_task_thread(
        task_id,
        lambda: run_batch_site_info_task(task_id, folder_path, model,
                                         model_id_override))
    flash(f"批量 AI 生成网站信息任务已启动: {folder}（将遍历其下最后一层文件夹，"
          "逐个网站顺序生成）", "info")
    return redirect(url_for("product_data.product_data_export"))


@bp.route("/product-data/site-info/table", methods=["GET"])
def product_data_site_info_table():
    """读取所选文件夹下的 网站信息.xlsx，返回表格数据（前端展示/审核用）

    每行附带 applied 字段：该网站是否已应用过审核（数据表已带 data_ 前缀）。
    """
    folder = request.args.get("folder", "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "请选择数据文件夹"})
    try:
        folder_path = resolve_export_target(folder, "")
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": f"文件夹不存在: {e}"})

    # 所选文件夹可以是网站信息的上级目录（如日期/大类目录），递归定位
    info_path = locate_info_file(folder_path)
    if info_path is None:
        return jsonify({"ok": False,
                        "error": f"该文件夹下暂无 {INFO_FILE_NAME}，请先批量生成网站信息"})
    try:
        rows = read_site_info_excel(info_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取 {INFO_FILE_NAME} 失败: {e}"})

    # 标记已应用审核的网站（数据表已加 data_ 前缀）+ 修复历史生成的主类目
    # （还原为表格原始分类值，含 ||| 层级分隔符）+ 补写网站大类
    # （旧表没有该列；从数据表 自定义分类 列聚合，数据的自定义分类就是
    # 网站的大类）
    # 定位：文件夹名列优先；未命中时回退用域名列（审核应用后文件夹
    # 已改名为域名，而 xlsx 行可能仍是旧文件夹名）
    repaired = 0
    for row in rows:
        site_folder = (locate_site_folder(folder_path,
                                          row.get("网站（文件夹）"))
                       or locate_site_folder(folder_path, row.get("域名")))
        row["applied"] = bool(site_folder and is_site_applied(site_folder))
        if site_folder is not None:
            try:
                repaired += 1 if repair_row_main_category(site_folder, row) else 0
                repaired += 1 if repair_row_major_category(site_folder, row) else 0
            except Exception as e:
                log.warning(f"修复主类目/网站大类失败（{row.get('网站（文件夹）')}）: {e}")

    # 有修复时回写 网站信息.xlsx（表格被 Excel 占用等失败时仅本次显示生效）
    if repaired:
        try:
            _write_info_excel(info_path, rows)
            log.info(f"已修复 {repaired} 行主类目并回写 {INFO_FILE_NAME}")
        except Exception as e:
            log.warning(f"主类目修复回写 {INFO_FILE_NAME} 失败（仅本次显示已修复）: {e}")

    return jsonify({"ok": True, "folder": folder, "rows": rows})


@bp.route("/product-data/site-info/apply", methods=["POST"])
def product_data_site_info_apply():
    """应用网站信息审核：审核通过的网站用通过后的域名修改其数据表

    - 每个数据表前五条数据的「原站域名」列改为域名标记：
      主数据 {域名}_main_part{N} / 补充数据 {域名}_part{N}（每个表格不同）
    - 数据表名加 data_ 前缀（后续上传只识别 data_ 前缀的网站数据表）
    - 审核结果（域名等编辑值 + 审核通过备注）回写 网站信息.xlsx
    """
    folder = request.form.get("folder", "").strip()
    sites_raw = request.form.get("sites", "").strip()

    if not folder:
        flash("请选择数据文件夹", "error")
        return redirect(url_for("product_data.product_data_export"))
    try:
        folder_path = resolve_export_target(folder, "")
    except FileNotFoundError as e:
        flash(f"文件夹不存在: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))

    try:
        sites = json.loads(sites_raw) if sites_raw else []
    except json.JSONDecodeError:
        flash("审核数据格式错误，请刷新页面重试", "error")
        return redirect(url_for("product_data.product_data_export"))
    if not isinstance(sites, list) or not sites:
        flash("请先勾选审核通过的网站", "error")
        return redirect(url_for("product_data.product_data_export"))
    # 只保留必要字段；域名/网站名缺失的项直接报错（前端已校验）
    sites = [{"folder": str(s.get("folder") or "").strip(),
              "domain": str(s.get("domain") or "").strip(),
              "title": str(s.get("title") or ""),
              "description": str(s.get("description") or ""),
              "theme": str(s.get("theme") or ""),
              "address": str(s.get("address") or ""),
              "keywords": str(s.get("keywords") or "")}
             for s in sites if isinstance(s, dict)]
    bad = [s["folder"] for s in sites if not s["folder"] or not s["domain"]]
    if bad:
        flash(f"以下网站的域名为空，请填写后再应用: {', '.join(bad)}", "error")
        return redirect(url_for("product_data.product_data_export"))

    task_id = f"site_review_{re.sub(r'[^\w-]+', '_', folder)}_{int(time.time())}"
    task_manager.create(task_id, "site_review", f"应用网站信息审核: {folder}")
    task_manager.start_task_thread(
        task_id, lambda: apply_site_review_task(task_id, folder_path, sites))
    flash(f"网站信息审核应用任务已启动: {len(sites)} 个网站（用通过后的域名"
          "修改数据表并加 data_ 前缀）", "info")
    return redirect(url_for("product_data.product_data_export"))


@bp.route("/product-data/category-process", methods=["POST"])
def product_data_category_process():
    """分类数据处理（数据库版）：处理已清洗未导出数据的分类字段。

    低频分类合并：所选范围内所有集合同名分类计数合并后 < threshold 的，
    确定性轮询归入公共类（同名分类统一分配同一公共类）。
    （无效分类名清理：simple / undefined / 纯数字 已在数据清洗操作中处理）
    仅修改"分类"字段并写入 category_process_status=processed 状态标识，
    不改变 clean_status / export_status，不增删文档。
    """
    category = request.form.get("category", "").strip()
    subcategory = parse_subcategory_form(request.form)
    threshold_raw = request.form.get("threshold", "").strip()
    common_category_input = request.form.get("common_category", "Other").strip()

    try:
        threshold = int(threshold_raw) if threshold_raw else 10
        if threshold < 1:
            threshold = 1
    except ValueError:
        threshold = 10

    common_categories = [c.strip() for c in re.split(r'[,;\n]+', common_category_input) if c.strip()]
    if not common_categories:
        common_categories = ["Other"]

    if not category:
        flash("请选择要处理的一级分类", "error")
        return redirect(url_for("product_data.product_data_clean"))

    # 防护：一级分类为 __all__ 时二级分类必须也是 __all__。
    # 该组合历史上会被后端当作"处理全部数据"（subcategory 被忽略），
    # 曾因前端轮询重置一级分类下拉框导致误提交，这里直接拦截。
    if category == "__all__" and subcategory != "__all__":
        flash("选择范围无效：一级分类为“全部”时，二级分类必须也为“全部”。请重新选择后再提交", "error")
        return redirect(url_for("product_data.product_data_clean"))

    sub_display = subcategory_display(subcategory)
    task_id = f"category_process_{category}_{sub_display}_{int(time.time())}"
    task_manager.create(task_id, "category_process", f"{category}/{sub_display}")

    task_manager.start_task_thread(
        task_id,
        lambda: run_category_process_task(task_id, category, subcategory,
                                          threshold, common_categories))
    flash(f"分类数据处理任务已启动: {category}/{sub_display}", "info")
    return redirect(url_for("product_data.product_data_clean"))


@bp.route("/product-data/category-optimize", methods=["POST"])
def product_data_category_optimize():
    """模型优化分类（数据库版）：优化已清洗未导出数据的分类字段。

    LLM 同义合并 + 单级分类补全父级，与工具箱 → 模型优化分类（数据库模式）
    共用同一任务体（category_optimize_db）。
    仅修改"分类"字段并写入 optimize_status=optimized 状态标识，
    不改变 clean_status / export_status，不增删文档。
    """
    category = request.form.get("category", "__all__").strip() or "__all__"
    subcategory = parse_subcategory_form(request.form)

    # 防护：一级分类为 __all__ 时二级分类必须也是 __all__（同 category-process）
    if category == "__all__" and subcategory != "__all__":
        flash("选择范围无效：一级分类为“全部”时，二级分类必须也为“全部”。请重新选择后再提交", "error")
        return redirect(url_for("product_data.product_data_clean"))

    sub_display = subcategory_display(subcategory)
    task_id = f"category_optimize_db_{category}_{sub_display}_{int(time.time())}"
    task_manager.create(task_id, "category_optimize_db", f"{category}/{sub_display}")

    task_manager.start_task_thread(
        task_id,
        lambda: run_category_optimize_task(task_id, category, subcategory))
    flash(f"模型优化分类任务已启动: {category}/{sub_display}", "info")
    return redirect(url_for("product_data.product_data_clean"))


# ── 数据表上传站群系统（惠升版） ─────────────────────────────

@bp.route("/api/product-data/site-upload/config", methods=["GET"])
def api_site_upload_config():
    """获取站群系统上传配置（密码不回传明文，仅返回是否已设置）"""
    try:
        from qmds.modules.web.services.site_uploader import load_upload_config
        cfg = load_upload_config()
        return jsonify({"ok": True, "data": {
            "login_url": cfg.get("login_url", ""),
            "upload_page_url": cfg.get("upload_page_url", ""),
            "username": cfg.get("username", ""),
            "has_password": bool(cfg.get("password", "").strip()),
        }})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/product-data/site-upload/config", methods=["POST"])
def product_data_site_upload_config():
    """保存站群系统上传配置（密码留空表示保持原值不变）"""
    from qmds.modules.web.services.site_uploader import load_upload_config, save_upload_config

    login_url = request.form.get("login_url", "").strip()
    upload_page_url = request.form.get("upload_page_url", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    try:
        if not login_url or not upload_page_url or not username:
            raise ValueError("登录地址、上传地址和账号均为必填项")
        # 密码留空 -> 沿用已保存的密码
        if not password:
            old = load_upload_config()
            password = old.get("password", "")
        if not password:
            raise ValueError("密码不能为空")
        save_upload_config({
            "login_url": login_url,
            "upload_page_url": upload_page_url,
            "username": username,
            "password": password,
        })
        flash("站群系统上传配置已保存", "info")
    except ValueError as e:
        flash(f"保存配置失败: {e}", "error")
    return redirect(url_for("product_data.product_data_export"))


@bp.route("/product-data/site-upload", methods=["POST"])
def product_data_site_upload():
    """启动站群系统上传任务（惠升版）：上传所选文件夹/表格下的全部 .xlsx"""
    from qmds.modules.web.services.site_uploader import (
        load_upload_config, resolve_export_target, run_upload_task, validate_config)

    folder = request.form.get("upload_folder", "").strip()
    filename = request.form.get("upload_file", "").strip()

    try:
        if not folder:
            raise ValueError("请选择要上传的文件夹")
        cfg = load_upload_config()
        validate_config(cfg)
        target = resolve_export_target(folder, filename)
    except (ValueError, FileNotFoundError) as e:
        flash(f"启动上传失败: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))

    target_label = target.name if target.is_file() else f"{target.name}/"
    task_id = f"site_upload_{target.name}_{int(time.time())}"
    task_manager.create(task_id, "site_upload_huisheng", f"站群上传: {target_label}")

    task_manager.start_task_thread(
        task_id, lambda: run_upload_task(task_id, target, cfg))
    flash(f"站群系统上传任务已启动: {target_label}", "info")
    return redirect(url_for("product_data.product_data_export"))


@bp.route("/product-data/site-upload/sites")
def product_data_site_upload_sites():
    """列出所选文件夹下所有以域名命名的网站文件夹（含 data_ 数据表数量）"""
    from qmds.modules.web.services.site_uploader import (
        collect_domain_sites, resolve_export_target)

    folder = request.args.get("folder", "").strip()
    try:
        if not folder:
            raise ValueError("请选择文件夹")
        target = resolve_export_target(folder)
        sites = collect_domain_sites(target)
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "sites": sites})


@bp.route("/product-data/site-upload/domain", methods=["POST"])
def product_data_site_upload_domain():
    """按网站（域名文件夹）上传：逐站上传选中网站内 data_ 开头的数据表"""
    from qmds.modules.web.services.site_uploader import (
        collect_domain_sites, load_upload_config, resolve_export_target,
        run_domain_upload_task, validate_config)

    folder = request.form.get("upload_folder", "").strip()
    raw_sites = request.form.get("sites", "").strip()

    try:
        if not folder:
            raise ValueError("请选择文件夹")
        try:
            site_names = [str(s).strip() for s in json.loads(raw_sites)
                          if str(s).strip()]
        except (json.JSONDecodeError, TypeError):
            raise ValueError("网站列表格式不正确")
        if not site_names:
            raise ValueError("请勾选要上传的网站")
        cfg = load_upload_config()
        validate_config(cfg)
        target = resolve_export_target(folder)
        # 提前校验域名文件夹存在（避免任务启动后才失败）
        known = {s["name"] for s in collect_domain_sites(target)}
        missing = [n for n in site_names if n not in known]
        if missing:
            raise ValueError("未找到域名文件夹: " + ", ".join(missing))
    except (ValueError, FileNotFoundError) as e:
        flash(f"启动上传失败: {e}", "error")
        return redirect(url_for("product_data.product_data_export"))

    task_id = f"site_upload_domain_{int(time.time())}"
    task_manager.create(task_id, "site_upload_huisheng",
                        f"站群上传: {len(site_names)} 个网站")
    task_manager.start_task_thread(
        task_id, lambda: run_domain_upload_task(task_id, target, site_names, cfg))
    flash(f"站群系统上传任务已启动: {len(site_names)} 个网站", "info")
    return redirect(url_for("product_data.product_data_export"))
