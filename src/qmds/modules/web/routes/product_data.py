"""产品数据管理路由"""

import os
import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from qmds.config import settings
from qmds.config.categories import parse_collection_prefix, normalize_subcategory
from qmds.modules.web.db_helpers import get_mongo_db, get_product_db
from qmds.modules.web.task_manager import make_progress_callback, task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.product_data")

bp = Blueprint("product_data", __name__)


def _resolve_category_list(product_db, category: str, subcategory: str):
    """根据 category / subcategory 解析要操作的分类列表

    - category == "__all__" 或两者都是 "__all__" → 所有分类
    - category 指定 + subcategory == "__all__" → 该一级分类下所有二级集合（含 other）
    - 两者都指定 → 仅该分类
    """
    if category == "__all__":
        return product_db.list_categories_with_sub()

    all_cats = product_db.list_categories_with_sub()
    cat_filtered = [item for item in all_cats if item["category"] == category]

    if subcategory == "__all__":
        return cat_filtered

    return [{"category": category, "subcategory": subcategory}]


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
        clear_sku = request.form.get("clear_sku") == "1"
        sub_display = subcategory if subcategory != "__all__" else "all"
        task_id = f"clean_{category}_{sub_display}_{int(time.time())}"
        task_manager.create(task_id, "clean_products", f"{category}/{sub_display}")

        def run_task():
            product_db = None
            try:
                from qmds.db.product_db import ProductDBClient
                force_msg = "（强制模式）" if force else ""
                sku_msg = "（清空SKU）" if clear_sku else ""
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

                    result = product_db.clean_category(cat, sub, force=force, clear_sku=clear_sku)
                    total_processed += result["processed"]
                    total_cleaned += result["cleaned"]
                    total_removed += result["removed"]

                    task_manager.add_log(task_id,
                                         f"分类 {cat}/{sub_d}: 处理 {result['processed']} 条, 通过 {result['cleaned']} 条, 移除 {result['removed']} 条", "info")

                    if result.get("sku_cleared"):
                        task_manager.add_log(task_id, f"  ├─ 清空SKU: {result['sku_cleared']} 条", "info")

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

    return render_template("product_export.html", category_stats=[])


@bp.route("/api/product-data/export-folders")
def api_export_folders():
    """获取导出文件夹列表（包含xlsx文件的文件夹）"""
    try:
        export_dir = settings.data_dir / "exports"
        if not export_dir.exists():
            return jsonify({"ok": True, "data": []})

        folders = []
        for root, dirs, files in os.walk(str(export_dir)):
            xlsx_files = [f for f in files if f.endswith('.xlsx') and not f.startswith('merged_')]
            if xlsx_files:
                rel_path = os.path.relpath(root, str(export_dir))
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

        if not folder_path.exists() or not folder_path.is_dir():
            return jsonify({"ok": False, "error": "文件夹不存在"}), 404

        files = []
        for f in sorted(folder_path.iterdir()):
            if f.suffix == '.xlsx':
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "size_mb": round(f.stat().st_size / 1024 / 1024, 2)
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
                    if not filepath.exists():
                        task_manager.add_log(task_id, f"跳过不存在的文件: {filename}", "warning")
                        continue

                    task_manager.add_log(task_id, f"读取: {filename}", "info")
                    df = pd.read_excel(filepath, engine="openpyxl")
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

                # 按标题去重
                if "标题" in merged_df.columns:
                    before_dedup = len(merged_df)
                    merged_df = merged_df.drop_duplicates(subset=["标题"], keep="first")
                    dedup_count = before_dedup - len(merged_df)
                    task_manager.add_log(task_id, f"按标题去重: 移除 {dedup_count} 条重复，剩余 {len(merged_df)} 条", "info")
                else:
                    task_manager.add_log(task_id, "未找到'标题'列，跳过去重", "warning")

                # 保存合并后的文件
                output_filename = f"merged_{folder_safe}.xlsx"
                output_path = folder_path / output_filename

                merged_df.to_excel(output_path, index=False, engine="openpyxl")
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
