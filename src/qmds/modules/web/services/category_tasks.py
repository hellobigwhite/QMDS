"""分类数据处理 / 模型优化分类（数据库版）共享任务体

供两处入口复用：
- 产品数据管理页（/product-data/category-process）
- 工具箱（/tools/category-merge、/tools/category-optimize 的数据库模式）

两者均处理数据库中"已清洗且未导出"（clean_status=cleaned 且 export_status=unexported）
的数据，并写入状态标识（category_process_status / optimize_status）。
"""

from qmds.modules.web.task_manager import make_progress_callback, task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.category_tasks")


def _normalize_subcategory_param(subcategory):
    """把 subcategory 参数（str 或 list[str]）规范化为 (是否全部, [具体二级分类...])

    - "__all__"、空串、None、空列表 → (True, [])
    - 具体值（str）或值列表（list[str]）→ (False, [去重后的具体值...])
    - 混合列表（具体值 + "__all__"）保守处理：忽略 "__all__"，仅保留具体值
    """
    if isinstance(subcategory, (list, tuple, set)):
        subs = []
        for s in subcategory:
            s = str(s).strip()
            if s and s != "__all__" and s not in subs:
                subs.append(s)
        return (False, subs) if subs else (True, [])
    s = str(subcategory or "").strip()
    if not s or s == "__all__":
        return True, []
    return False, [s]


def parse_subcategory_form(form):
    """从表单读取二级分类（支持多选，字段名 subcategory 可重复出现）

    返回 "__all__"（表示全部）或去重后的具体二级分类值列表。
    """
    return _normalize_subcategory_param(form.getlist("subcategory"))[1] or "__all__"


def subcategory_display(subcategory) -> str:
    """把 subcategory 参数（str 或 list[str]）转为展示/任务ID用字符串"""
    is_all, subs = _normalize_subcategory_param(subcategory)
    return "all" if is_all else ",".join(subs)


def resolve_category_list(product_db, category: str, subcategory):
    """根据 category / subcategory 解析要操作的分类列表

    - category == "__all__" → 所有分类
    - category 指定 + subcategory 为 "__all__"/空 → 该一级分类下所有二级集合（含 other）
    - subcategory 为具体二级分类（str 或 list[str]，支持多选）→ 所选的集合
      （按实际存在的集合过滤，不存在的二级分类名会被忽略）
    """
    if category == "__all__":
        return product_db.list_categories_with_sub()

    all_cats = product_db.list_categories_with_sub()
    cat_filtered = [item for item in all_cats if item["category"] == category]

    is_all, subs = _normalize_subcategory_param(subcategory)
    if is_all:
        return cat_filtered

    sub_set = set(subs)
    return [item for item in cat_filtered if item.get("subcategory", "") in sub_set]


def _filter_exportable_collections(task_id, product_db, cat_list):
    """只保留"已清洗且未导出"数量 > 0 的集合（与前端下拉框口径一致）"""
    exportable_cat_list = []
    for item in cat_list:
        stats = product_db.get_simple_category_stats(item["category"], item.get("subcategory", ""))
        if stats.get("unexported_count", 0) > 0:
            exportable_cat_list.append(item)
    if len(exportable_cat_list) != len(cat_list):
        task_manager.add_log(
            task_id,
            f"过滤后剩余 {len(exportable_cat_list)} 个有已清洗未导出数据的分类"
            f"（跳过 {len(cat_list) - len(exportable_cat_list)} 个无数据分类）",
            "info"
        )
    return exportable_cat_list


def run_category_process_task(task_id: str, category: str, subcategory,
                              threshold: int, common_categories):
    """分类数据处理（数据库版）任务体：低频分类合并 + 状态标识

    由 product_data 与 tools 两个路由共享；调用方负责创建任务并启动线程。
    """
    product_db = None
    sub_display = subcategory_display(subcategory)
    try:
        task_manager.update(task_id, status="running",
                            message=f"开始分类数据处理: {category}/{sub_display}")
        task_manager.add_log(task_id, f"任务启动: 分类数据处理 {category}/{sub_display}", "info")
        task_manager.add_log(task_id, f"参数: 阈值={threshold}, 公共类={', '.join(common_categories)}", "info")

        from qmds.db.product_db import ProductDBClient
        product_db = ProductDBClient()
        cat_list = resolve_category_list(product_db, category, subcategory)
        task_manager.add_log(task_id, f"获取到 {len(cat_list)} 个分类", "info")

        if not cat_list:
            task_manager.update(task_id, status="completed",
                                message="完成: 未找到任何分类数据", progress=100)
            task_manager.add_log(task_id, "未找到任何分类数据，任务结束", "warning")
            return

        cat_list = _filter_exportable_collections(task_id, product_db, cat_list)

        if not cat_list:
            task_manager.update(task_id, status="completed",
                                message="完成: 所选分类中没有已清洗未导出的数据", progress=100)
            task_manager.add_log(task_id, "所选分类中没有已清洗未导出的数据，任务结束", "warning")
            return

        task_manager.update(task_id, message=f"正在跨集合统计分类（{len(cat_list)} 个集合）...")
        task_manager.add_log(task_id, f"开始跨集合分类处理: {len(cat_list)} 个集合", "info")

        result = product_db.process_category_data_global(
            [(item["category"], item.get("subcategory", "")) for item in cat_list],
            threshold=threshold,
            common_categories=common_categories,
            progress_callback=make_progress_callback(task_id),
            stop_event=task_manager.get_stop_event(task_id),
        )

        total_processed = result.get("processed", 0)
        total_modified = result.get("modified_rows", 0)
        total_marked = result.get("status_marked", 0)
        merged = result.get("merged", [])
        before_c = result.get("category_count_before", 0)
        after_c = result.get("category_count_after", 0)
        collections = result.get("collections", [])

        task_manager.add_log(
            task_id,
            f"总体: 已清洗未导出 {total_processed} 条, 合并 {len(merged)} 个低频分类, "
            f"修改 {total_modified} 行, 新标记已处理 {total_marked} 条, "
            f"全局分类数 {before_c} -> {after_c}",
            "info"
        )

        # 每个集合的修改明细
        for detail in collections:
            sub_d = detail.get("subcategory") or "other"
            task_manager.add_log(
                task_id,
                f"  集合 {detail.get('category')}/{sub_d}: 修改 {detail.get('modified_rows', 0)} 行, "
                f"新标记已处理 {detail.get('status_marked', 0)} 条",
                "info"
            )

        # 低频合并明细（全局计数）
        for m_cat, m_cnt, new_cat in merged[:15]:
            task_manager.add_log(task_id, f"  低频合并: {m_cat} (全局 {m_cnt} 条) -> {new_cat}", "info")
        if len(merged) > 15:
            task_manager.add_log(task_id, f"  ... 还有 {len(merged) - 15} 个低频分类", "info")

        task_manager.update(task_id, status="completed",
                            message=f"完成: 处理 {total_processed} 条, 修改 {total_modified} 行, "
                                    f"合并 {len(merged)} 个低频分类, 标记已处理 {total_marked} 条",
                            progress=100)
        task_manager.add_log(task_id, "任务完成", "info")
    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="任务已停止")
        task_manager.add_log(task_id, "任务被用户停止", "warning")
    except Exception as e:
        import traceback
        log.error(f"分类数据处理失败: {e}\n{traceback.format_exc()}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")
    finally:
        if product_db:
            product_db.close()


def run_category_optimize_task(task_id: str, category: str, subcategory):
    """模型优化分类（数据库版）任务体：LLM 同义合并/补全父级 + 状态标识

    处理数据库中已清洗未导出数据的分类字段，规则与表格文件模式一致；
    处理后写入 optimize_status=optimized 状态标识。
    """
    product_db = None
    site_db = None
    sub_display = subcategory_display(subcategory)
    try:
        task_manager.update(task_id, status="running",
                            message=f"开始模型优化分类: {category}/{sub_display}")
        task_manager.add_log(task_id, f"任务启动: 模型优化分类 {category}/{sub_display}", "info")

        from qmds.db.product_db import ProductDBClient
        from qmds.db.site_db import SiteDBClient
        product_db = ProductDBClient()
        site_db = SiteDBClient()

        cat_list = resolve_category_list(product_db, category, subcategory)
        task_manager.add_log(task_id, f"获取到 {len(cat_list)} 个分类", "info")

        if not cat_list:
            task_manager.update(task_id, status="completed",
                                message="完成: 未找到任何分类数据", progress=100)
            task_manager.add_log(task_id, "未找到任何分类数据，任务结束", "warning")
            return

        cat_list = _filter_exportable_collections(task_id, product_db, cat_list)

        if not cat_list:
            task_manager.update(task_id, status="completed",
                                message="完成: 所选分类中没有已清洗未导出的数据", progress=100)
            task_manager.add_log(task_id, "所选分类中没有已清洗未导出的数据，任务结束", "warning")
            return

        task_manager.update(task_id, message=f"正在模型优化分类（{len(cat_list)} 个集合）...")
        task_manager.add_log(task_id, f"开始模型优化分类: {len(cat_list)} 个集合", "info")

        def log_callback(message, level="info"):
            task_manager.add_log(task_id, message, level)

        result = product_db.optimize_category_data_global(
            [(item["category"], item.get("subcategory", "")) for item in cat_list],
            log_callback=log_callback,
            progress_callback=make_progress_callback(task_id),
            stop_event=task_manager.get_stop_event(task_id),
            site_db=site_db,
        )

        total_processed = result.get("processed", 0)
        unique_cats = result.get("unique_categories", 0)
        mappings_count = result.get("mappings_count", 0)
        total_modified = result.get("modified_rows", 0)
        total_marked = result.get("status_marked", 0)
        total_moved = result.get("moved", 0)
        total_moved_skipped = result.get("moved_skipped", 0)
        moved_targets = result.get("moved_targets", {}) or {}
        mappings = result.get("mappings", {})
        collections = result.get("collections", [])

        task_manager.add_log(
            task_id,
            f"总体: 已清洗未导出 {total_processed} 条, 唯一分类 {unique_cats} 个, "
            f"有效映射 {mappings_count} 个, 修改 {total_modified} 行, 新标记已优化 {total_marked} 条, "
            f"跨大类转移 {total_moved} 条"
            + (f"（另 {total_moved_skipped} 条因目标已有同款跳过）" if total_moved_skipped else ""),
            "info"
        )

        # 跨大类转移目标明细
        if moved_targets:
            for tp, cnt in sorted(moved_targets.items()):
                task_manager.add_log(task_id, f"  转入 {tp}: {cnt} 条", "info")

        # 每个集合的修改明细
        for detail in collections:
            sub_d = detail.get("subcategory") or "other"
            task_manager.add_log(
                task_id,
                f"  集合 {detail.get('category')}/{sub_d}: 修改 {detail.get('modified_rows', 0)} 行, "
                f"新标记已优化 {detail.get('status_marked', 0)} 条, "
                f"转移出 {detail.get('moved_out', 0)} 条",
                "info"
            )

        # 映射明细（前20个，含模型判定的一级大类）
        for i, (orig, entry) in enumerate(mappings.items()):
            if i >= 20:
                task_manager.add_log(task_id, f"  ... 还有 {len(mappings) - 20} 个映射", "info")
                break
            if isinstance(entry, dict):
                optimized = entry.get("optimized")
                top = entry.get("top")
            else:
                optimized, top = entry, None
            top_suffix = f" [大类: {top}]" if top else ""
            task_manager.add_log(task_id, f"  映射: {orig} -> {optimized}{top_suffix}", "info")

        task_manager.update(task_id, status="completed",
                            message=f"完成: 处理 {total_processed} 条, 映射 {mappings_count} 个, "
                                    f"修改 {total_modified} 行, 标记已优化 {total_marked} 条, "
                                    f"转移 {total_moved} 条",
                            progress=100)
        task_manager.add_log(task_id, "任务完成", "info")
    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="任务已停止")
        task_manager.add_log(task_id, "任务被用户停止", "warning")
    except Exception as e:
        import traceback
        log.error(f"模型优化分类失败: {e}\n{traceback.format_exc()}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")
    finally:
        if product_db:
            product_db.close()
        if site_db:
            try:
                site_db.close()
            except Exception:
                pass
