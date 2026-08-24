import os
import re
import time
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for

from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.tools")

bp = Blueprint("tools", __name__)


def _get_http():
    from qmds.utils.http_client import HttpClient
    from qmds.utils.proxy_manager import ProxyManager
    from qmds.config import settings
    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    return HttpClient(proxy_manager=pm)


@bp.route("/tools", methods=["GET"])
def tools():
    """工具箱主页"""
    return render_template("tools.html")


@bp.route("/tools/id-distribute", methods=["GET"])
def id_distribute():
    """商品ID分配工具"""
    return render_template("id_distribute.html")


@bp.route("/tools/data-clean", methods=["GET", "POST"])
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
        task_manager.create(task_id, "data_clean", f"数据二次清洗: {os.path.basename(folder_path)}")

        def run_task():
            from qmds.utils.data_cleaner import clean_folder
            try:
                task_manager.add_log(task_id, f"任务启动: 数据二次清洗", "info")
                task_manager.add_log(task_id, f"输入目录: {folder_path}", "info")
                task_manager.add_log(task_id, f"价格阈值: {price_threshold}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, message="正在扫描文件...")
                task_manager.add_log(task_id, "正在扫描文件夹...", "info")

                result = clean_folder(
                    input_folder=folder_path,
                    output_folder=output_folder,
                    price_threshold=price_threshold
                )

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    task_manager.add_log(task_id, "任务被用户停止", "warning")
                    return

                if output_folder:
                    result["output_folder"] = output_folder
                else:
                    result["output_folder"] = str(Path(folder_path) / "cleaned")

                task_manager.add_log(task_id, f"扫描文件数: {result['total_files']}", "info")
                task_manager.add_log(task_id, f"成功处理: {result['processed']}", "info")
                task_manager.add_log(task_id, f"处理失败: {result['failed']}", "info")
                task_manager.add_log(task_id, f"输出目录: {result['output_folder']}", "info")

                for detail in result.get("details", []):
                    if detail.get("status") == "success":
                        orig = detail.get("original_count", 0)
                        cleaned = detail.get("cleaned_count", 0)
                        removed = orig - cleaned if orig and cleaned else 0
                        task_manager.add_log(task_id, f"✓ {detail['file']}: {orig} → {cleaned} (删除 {removed})", "info")
                    else:
                        task_manager.add_log(task_id, f"✗ {detail['file']}: {detail.get('reason', '未知错误')}", "error")

                summary = f"完成: 处理 {result['processed']}/{result['total_files']} 个文件，失败 {result['failed']}"
                task_manager.update(task_id, status="completed", message=summary, progress=100)
                task_manager.add_log(task_id, summary, "info")

            except FileNotFoundError as e:
                task_manager.update(task_id, status="failed", message=str(e))
                task_manager.add_log(task_id, str(e), "error")
            except Exception as e:
                log.error(f"数据二次清洗失败: {e}")
                task_manager.update(task_id, status="failed", message=f"处理失败: {e}")
                task_manager.add_log(task_id, f"处理失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash(f"数据二次清洗任务已启动，可在任务页面查看进度", "success")
        return redirect(url_for("core.tasks"))

    except Exception as e:
        log.error(f"数据二次清洗失败: {e}")
        return render_template("data_clean.html", error=f"处理失败: {str(e)}")


@bp.route("/tools/category-merge", methods=["GET", "POST"])
def category_merge():
    """分类数据处理：将数量过少的分类合并为公共类"""
    if request.method == "GET":
        return render_template("category_merge.html")

    try:
        file_path = request.form.get("file_path", "").strip()
        threshold = int(request.form.get("threshold", 10))
        category_field = request.form.get("category_field", "分类").strip()
        common_category_input = request.form.get("common_category", "Other").strip()
        price_threshold = float(request.form.get("price_threshold", 2500))
        common_categories = [cat.strip() for cat in re.split(r'[,;\n]+', common_category_input) if cat.strip()]
        if not common_categories:
            common_categories = ["Other"]

        if not file_path:
            return render_template("category_merge.html", error="请输入表格文件路径")

        if not file_path.endswith(('.xlsx', '.xls', '.csv')):
            return render_template("category_merge.html", error="不支持的文件格式，请使用 .xlsx 或 .csv 文件")

        task_id = f"category_merge_{int(time.time())}"
        task_manager.create(task_id, "category_merge", f"分类数据处理: {os.path.basename(file_path)}")

        def run_task():
            import pandas as pd
            from collections import Counter
            import random as rng

            try:
                task_manager.add_log(task_id, f"任务启动: 分类数据处理", "info")
                task_manager.add_log(task_id, f"文件: {file_path}", "info")
                task_manager.add_log(task_id, f"分类字段: {category_field}, 阈值: {threshold}", "info")
                task_manager.add_log(task_id, f"公共类: {', '.join(common_categories)}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, message="正在读取文件...")
                task_manager.add_log(task_id, "正在读取表格文件...", "info")

                if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                    df = pd.read_excel(file_path)
                else:
                    df = pd.read_csv(file_path)

                if category_field not in df.columns:
                    task_manager.update(task_id, status="failed", message=f"未找到 '{category_field}' 列")
                    task_manager.add_log(task_id, f"表格中未找到 '{category_field}' 列，可用列: {', '.join(df.columns.tolist())}", "error")
                    return

                total_rows = len(df)
                task_manager.add_log(task_id, f"读取完成，共 {total_rows} 行数据", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.add_log(task_id, "─── 开始数据二次清洗 ───", "info")
                task_manager.update(task_id, message="正在执行数据二次清洗...")

                from qmds.utils.data_cleaner import clean_dataframe
                before_clean = len(df)
                df = clean_dataframe(df, price_threshold=price_threshold)
                after_clean = len(df)
                removed_clean = before_clean - after_clean

                task_manager.add_log(task_id, f"清洗前行数: {before_clean}", "info")
                task_manager.add_log(task_id, f"清洗后行数: {after_clean} (删除 {removed_clean})", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, message="正在统计分类...")
                task_manager.add_log(task_id, f"清洗后数据 {after_clean} 行，开始统计各分类数量...", "info")

                category_counts = Counter(df[category_field].fillna('').astype(str))

                merged_categories = []
                for cat, count in category_counts.items():
                    if cat and count < threshold:
                        merged_categories.append((cat, count))
                merged_categories.sort(key=lambda x: x[1])

                before_count = len([c for c in category_counts.keys() if c])
                task_manager.add_log(task_id, f"修改前分类数: {before_count}, 待合并分类数: {len(merged_categories)}", "info")

                if not merged_categories:
                    task_manager.update(task_id, status="completed",
                                        message=f"无需合并，所有分类数量均大于阈值；二次清洗删除 {removed_clean} 行，最终 {after_clean} 行",
                                        progress=100)
                    task_manager.add_log(task_id, "无需合并，所有分类数量均大于阈值", "info")
                    return

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, message=f"正在合并 {len(merged_categories)} 个分类...")
                task_manager.add_log(task_id, "开始执行合并操作...", "info")

                modified_rows = 0
                for i, (cat, count) in enumerate(merged_categories):
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message=f"任务已停止: 已处理 {i}/{len(merged_categories)}")
                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return

                    mask = df[category_field].fillna('').astype(str) == cat
                    row_count = mask.sum()
                    modified_rows += row_count
                    selected_category = rng.choice(common_categories)
                    df.loc[mask, category_field] = selected_category

                    if (i + 1) % 50 == 0 or i + 1 == len(merged_categories):
                        progress = int((i + 1) / len(merged_categories) * 100)
                        task_manager.update(task_id, progress=progress, current=i + 1, total=len(merged_categories),
                                            message=f"合并中: {i + 1}/{len(merged_categories)}")

                new_category_counts = Counter(df[category_field].fillna('').astype(str))
                after_count = len([c for c in new_category_counts.keys() if c])

                remaining_categories = [(cat, count) for cat, count in new_category_counts.items()
                                       if cat and cat not in common_categories]
                remaining_categories.sort(key=lambda x: x[1], reverse=True)

                task_manager.add_log(task_id, f"修改前分类数: {before_count} → 修改后: {after_count}", "info")
                task_manager.add_log(task_id, f"合并分类数: {len(merged_categories)}, 修改行数: {modified_rows}", "info")

                for cat, count in merged_categories[:20]:
                    task_manager.add_log(task_id, f"  合并: {cat} ({count} 条)", "info")
                if len(merged_categories) > 20:
                    task_manager.add_log(task_id, f"  ... 还有 {len(merged_categories) - 20} 个分类", "info")

                summary = f"分类合并: {len(merged_categories)} 个分类, 修改 {modified_rows} 行, {before_count} → {after_count}"
                task_manager.add_log(task_id, summary, "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.add_log(task_id, "─── 清理无效分类名称 ───", "info")
                task_manager.update(task_id, message="正在清理无效分类名称...")

                def _is_invalid_category(cat_name: str) -> bool:
                    """判断分类名称是否无效：simple、包含Undefined、纯数字符号，或任一级为纯数字"""
                    if not cat_name:
                        return False
                    cat_lower = cat_name.strip().lower()
                    if cat_lower == "simple":
                        return True
                    if "undefined" in cat_lower:
                        return True
                    cleaned = re.sub(r'[\s\-_.,/\\|:;]+', '', cat_name.strip())
                    if cleaned.isdigit():
                        return True
                    parts = re.split(r'\s*\|\|\|\s*|\s*->\s*|\s*>\s*|\s*,\s*|\s*/\s*|\s*[:：]\s*', cat_name.strip())
                    for part in parts:
                        part_cleaned = re.sub(r'[\s\-_.,/\\|:;]+', '', part)
                        if part_cleaned and part_cleaned.isdigit():
                            return True
                    return False

                invalid_categories = []
                current_counts = Counter(df[category_field].fillna('').astype(str))
                for cat, count in current_counts.items():
                    if cat and _is_invalid_category(cat):
                        invalid_categories.append((cat, count))

                if invalid_categories:
                    task_manager.add_log(task_id, f"发现 {len(invalid_categories)} 个无效分类名称", "info")

                    invalid_modified_rows = 0
                    for i, (cat, count) in enumerate(invalid_categories):
                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped", message="任务已停止")
                            return

                        mask = df[category_field].fillna('').astype(str) == cat
                        row_count = mask.sum()
                        invalid_modified_rows += row_count
                        selected_category = rng.choice(common_categories)
                        df.loc[mask, category_field] = selected_category
                        task_manager.add_log(task_id, f"  替换: {cat} ({count} 条) → {selected_category}", "info")

                    task_manager.add_log(task_id, f"无效分类清理完成: 替换 {len(invalid_categories)} 个分类, 修改 {invalid_modified_rows} 行", "info")
                    summary += f"；无效分类替换 {len(invalid_categories)} 个, 修改 {invalid_modified_rows} 行"
                else:
                    task_manager.add_log(task_id, "未发现无效分类名称", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                    df.to_excel(file_path, index=False)
                else:
                    df.to_csv(file_path, index=False)

                task_manager.add_log(task_id, f"最终文件已保存: {file_path}", "info")

                final_summary = f"{summary}；二次清洗删除 {removed_clean} 行，最终 {len(df)} 行"
                task_manager.update(task_id, status="completed", message=final_summary, progress=100)

            except Exception as e:
                log.error(f"分类数据处理失败: {e}")
                task_manager.update(task_id, status="failed", message=f"处理失败: {e}")
                task_manager.add_log(task_id, f"处理失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash(f"分类数据处理任务已启动，可在任务页面查看进度", "success")
        return redirect(url_for("core.tasks"))

    except Exception as e:
        log.error(f"分类数据处理失败: {e}")
        return render_template("category_merge.html", error=f"处理失败: {str(e)}")


@bp.route("/tools/category-optimize", methods=["GET", "POST"])
def category_optimize():
    """模型优化分类结构：同义合并 + 单级分类补全父级"""
    if request.method == "GET":
        return render_template("category_optimize.html")

    try:
        file_path = request.form.get("file_path", "").strip()
        category_field = request.form.get("category_field", "Categories").strip()

        if not file_path:
            return render_template("category_optimize.html", error="请输入表格文件路径")

        if not file_path.endswith(('.xlsx', '.xls', '.csv')):
            return render_template("category_optimize.html", error="不支持的文件格式，请使用 .xlsx 或 .csv 文件")

        task_id = f"category_optimize_{int(time.time())}"
        task_manager.create(task_id, "category_optimize", f"模型优化分类: {os.path.basename(file_path)}")

        def run_task():
            try:
                task_manager.add_log(task_id, f"任务启动: 模型优化分类", "info")
                task_manager.add_log(task_id, f"文件: {file_path}", "info")
                task_manager.add_log(task_id, f"分类字段: {category_field}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.update(task_id, message="正在优化分类结构...")

                from qmds.utils.category_optimizer import optimize_file
                from qmds.db.site_db import SiteDBClient

                def log_callback(message, level="info"):
                    task_manager.add_log(task_id, message, level)

                _site_db = SiteDBClient()

                result = optimize_file(
                    file_path=file_path,
                    category_col=category_field,
                    log_callback=log_callback,
                    site_db=_site_db,
                )

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                task_manager.add_log(task_id, f"输入文件: {result['input_file']}", "info")
                task_manager.add_log(task_id, f"输出文件: {result['output_file']}", "info")
                task_manager.add_log(task_id, f"数据量: {result['original_count']} 行", "info")
                task_manager.add_log(task_id, f"映射数: {result['mappings_count']} 个", "info")

                summary = f"完成: 优化 {result['mappings_count']} 个分类映射，数据 {result['original_count']} 行"
                task_manager.update(task_id, status="completed", message=summary, result=result, progress=100)
                task_manager.add_log(task_id, summary, "success")

            except Exception as e:
                log.error(f"模型优化分类失败: {e}")
                task_manager.update(task_id, status="failed", message=f"处理失败: {e}")
                task_manager.add_log(task_id, f"处理失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash(f"模型优化分类任务已启动，可在任务页面查看进度", "success")
        return redirect(url_for("core.tasks"))

    except Exception as e:
        log.error(f"模型优化分类失败: {e}")
        return render_template("category_optimize.html", error=f"处理失败: {str(e)}")


@bp.route("/tools/html-classifier", methods=["GET", "POST"])
def html_classifier():
    """基于HTML内容的Shopify网站分类器"""
    if request.method == "GET":
        task_id = request.args.get("task_id")
        task_result = None
        if task_id:
            task_result = task_manager.get(task_id)
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

        file_path = re.sub(r'[\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069]', '', file_path)
        file_path = file_path.strip().strip('\u200b').strip('\ufeff')

        if not Path(file_path).exists():
            return render_template("html_classifier.html", error=f"文件不存在: {file_path}")

        if not file_path.endswith(('.xlsx', '.xls')):
            return render_template("html_classifier.html", error="请使用 .xlsx 文件")

        task_id = f"html_classifier_{int(time.time())}"
        task_manager.create(task_id, "html_classifier", f"HTML批量分类: {Path(file_path).name}")

        def run_task():
            try:
                from qmds.utils.html_site_classifier import HTMLSiteClassifier
                classifier = HTMLSiteClassifier(use_proxy=True)

                task_manager.add_log(task_id, f"开始处理: {file_path}", "info")
                stats = classifier.classify_from_excel(file_path)

                final_msg = f"完成! 总计 {stats['total']}: 成功 {stats['success']}, 失败 {stats['error']}, Shopify {stats['shopify']}"
                task_manager.update(task_id, status="completed", message=final_msg, result=stats, progress=100)
                task_manager.add_log(task_id, final_msg, "success")

            except Exception as e:
                log.error(f"HTML批量分类失败: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash(f"HTML批量分类任务已启动，可在任务页面查看进度", "success")
        return redirect(url_for("html_classifier", task_id=task_id))


@bp.route("/tools/site-classifier", methods=["GET", "POST"])
def site_classifier():
    """Shopify 网站分类器：判断专一站/综合站"""
    if request.method == "GET":
        task_id = request.args.get("task_id")
        task_result = None
        if task_id:
            task_result = task_manager.get(task_id)
        return render_template("site_classifier.html", task_result=task_result)

    action = request.form.get("action", "single")

    if action == "single":
        url = request.form.get("url", "").strip()
        if not url:
            return render_template("site_classifier.html", error="请输入网站 URL")

        try:
            from qmds.utils.site_classifier import SiteClassifier
            http = _get_http()
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

        file_path = re.sub(r'[\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069]', '', file_path)
        file_path = file_path.strip().strip('\u200b').strip('\ufeff')

        if not Path(file_path).exists():
            return render_template("site_classifier.html", error=f"文件不存在: {file_path}")

        if not file_path.endswith(('.xlsx', '.xls')):
            return render_template("site_classifier.html", error="请使用 .xlsx 文件")

        task_id = f"classifier_{int(time.time())}"
        task_manager.create(task_id, "site_classifier", f"批量分类: {Path(file_path).name}")

        def run_task():
            try:
                from qmds.utils.site_classifier import SiteClassifier
                http = _get_http()
                classifier = SiteClassifier(http_client=http)

                task_manager.add_log(task_id, f"开始处理: {file_path}", "info")
                stats = classifier.classify_from_excel(file_path)

                final_msg = f"完成! 总计 {stats['total']}: 专一 {stats['niche']}, 综合 {stats['general']}, 未知 {stats['unknown']}, 非英文 {stats['non_english']}"
                task_manager.update(task_id, status="completed", message=final_msg, result=stats, progress=100)
                task_manager.add_log(task_id, final_msg, "success")

            except Exception as e:
                log.error(f"批量分类失败: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"失败: {e}", "error")

        task_manager.start_task_thread(task_id, run_task)
        flash(f"批量分类任务已启动，可在任务页面查看进度", "success")
        return redirect(url_for("site_classifier", task_id=task_id))
