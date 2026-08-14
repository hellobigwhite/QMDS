"""Shopify 店铺管理路由"""

import os
import time
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from qmds.config import settings
from qmds.config.categories import (
    SHOPIFY_CATEGORIES,
    DEFAULT_SUBCATEGORY,
    generate_keywords_from_subcategory,
    get_level2_categories,
    get_standard_subcategories,
    normalize_subcategory,
    parse_collection_prefix,
)
from qmds.modules.web.db_helpers import get_mongo_db
from qmds.modules.web.task_manager import make_progress_callback, task_manager
from qmds.utils.http_client import HttpClient
from qmds.utils.logger import get_logger
from qmds.utils.proxy_manager import ProxyManager

log = get_logger("web.shopify")

bp = Blueprint("shopify", __name__)


def _get_module():
    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    http = HttpClient(proxy_manager=pm)
    from qmds.modules.data_scraper import DataScraperModule
    return DataScraperModule(http_client=http)


def _get_http():
    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    return HttpClient(proxy_manager=pm)


@bp.route("/shopify/fetch-urls", methods=["GET", "POST"])
def shopify_fetch_urls():
    module = _get_module()
    api_status = module.searcher.get_api_status()
    selected_category = request.args.get("category", "")
    page_num = request.args.get("page", 1, type=int)
    per_page = 50
    stores = []
    stores_total = 0
    total_pages = 0

    if selected_category:
        db = get_mongo_db()
        try:
            stores_total = db.get_unfiltered_count(selected_category)
            total_pages = (stores_total + per_page - 1) // per_page
            if page_num < 1:
                page_num = 1
            elif page_num > total_pages and total_pages > 0:
                page_num = total_pages
            skip = (page_num - 1) * per_page
            stores = db.get_unfiltered_stores(selected_category, limit=per_page, skip=skip)
        except Exception as e:
            log.error(f"查询unfiltered数据失败: {e}")

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
            task_manager.create(task_id, "fetch_shopify_urls", f"{category} | {keyword}")

            def run_task():
                try:
                    task_manager.update(task_id, status="running", message=f"搜索中: {keyword}")
                    task_manager.add_log(task_id, f"任务启动: 搜索Shopify店铺", "info")
                    task_manager.add_log(task_id, f"关键词: {keyword}", "info")
                    task_manager.add_log(task_id, f"类目: {category}", "info")
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    task_manager.add_log(task_id, "开始搜索...", "info")

                    result = module.fetch_shopify_urls_by_keyword(
                        category=category, keyword=keyword,
                        max_pages=0, min_products=min_products,
                        keyword_workers=3,
                        save_mongo=save_mongo, save_excel=save_excel,
                        provider_name=provider,
                        progress_callback=make_progress_callback(task_id),
                    )
                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        return
                    task_manager.update(task_id, status="completed",
                                        message=f"完成: 找到 {result['total_shopify']} 个店铺",
                                        result=result, progress=100)
                    task_manager.add_log(task_id, f"任务完成: 找到 {result['total_shopify']} 个店铺", "info")
                except Exception as e:
                    log.error(f"fetch-urls task failed: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务失败: {e}", "error")

            task_manager.start_task_thread(task_id, run_task)
            flash(f"任务已启动: {category} | {keyword}，可在任务页面查看进度")
            return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    return render_template("shopify_urls.html", result=None, categories=SHOPIFY_CATEGORIES,
                           db_name=settings.mongo_db_url, api_status=api_status,
                           selected_category=selected_category, stores=stores,
                           stores_total=stores_total, page=page_num, total_pages=total_pages)


@bp.route("/shopify/get-subcategories", methods=["GET"])
def shopify_get_subcategories():
    category = (request.args.get("category") or "").strip()
    if not category:
        return jsonify({"error": "category 不能为空"}), 400
    subcategories = get_level2_categories(category)
    return jsonify({"category": category, "subcategories": subcategories})


@bp.route("/shopify/generate-keywords", methods=["POST"])
def shopify_generate_keywords():
    category = (request.form.get("category") or "").strip()
    subcategory = (request.form.get("subcategory") or "").strip()
    try:
        count = int(request.form.get("count", 10))
    except ValueError:
        count = 10
    if count < 1:
        count = 10
    if not category:
        return jsonify({"error": "category 不能为空"}), 400
    result = generate_keywords_from_subcategory(category, subcategory, count)
    return jsonify(result)


@bp.route("/shopify/unfiltered/add", methods=["POST"])
def shopify_unfiltered_add():
    category = request.form.get("category", "").strip()
    domain = request.form.get("domain", "").strip()
    if not category or not domain:
        flash("类目和域名不能为空", "error")
        return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    db = get_mongo_db()
    try:
        store_data = {
            "domain": domain,
            "url": request.form.get("url", f"https://{domain}").strip(),
            "platform": request.form.get("platform", "Shopify").strip(),
            "product_count": int(request.form.get("product_count", 0)),
            "store_name": request.form.get("store_name", "").strip(),
            "currency": request.form.get("currency", "USD").strip(),
            "source": "manual",
        }
        if db.add_unfiltered(category, store_data):
            flash(f"已添加店铺: {domain}", "success")
        else:
            flash(f"添加失败: {domain}", "error")
    except Exception as e:
        flash(f"添加失败: {e}", "error")
    return redirect(url_for("shopify.shopify_fetch_urls", category=category))


@bp.route("/shopify/unfiltered/import", methods=["POST"])
def shopify_unfiltered_import():
    category = request.form.get("category", "").strip()
    if not category:
        flash("类目不能为空", "error")
        return redirect(url_for("shopify.shopify_fetch_urls"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择要导入的Excel文件", "error")
        return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    if not file.filename.endswith(('.xlsx', '.xls')):
        flash("请上传Excel文件（.xlsx或.xls格式）", "error")
        return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    db = get_mongo_db()
    try:
        filepath = os.path.join(os.getcwd(), "uploads", file.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        file.save(filepath)

        result = db.import_from_excel(category, filepath)

        messages = [f"新增: {result['created']}", f"更新: {result['updated']}", f"跳过: {result['skipped']}"]
        if result['errors']:
            messages.append(f"错误: {len(result['errors'])}")
            for error in result['errors'][:5]:
                flash(error, "error")
            if len(result['errors']) > 5:
                flash(f"还有 {len(result['errors']) - 5} 个错误...", "error")

        flash(f"导入完成: {', '.join(messages)}", "success")
    except Exception as e:
        flash(f"导入失败: {e}", "error")
    return redirect(url_for("shopify.shopify_fetch_urls", category=category))


@bp.route("/shopify/unfiltered/edit", methods=["POST"])
def shopify_unfiltered_edit():
    category = request.form.get("category", "").strip()
    domain = request.form.get("domain", "").strip()
    if not category or not domain:
        flash("类目和域名不能为空", "error")
        return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    db = get_mongo_db()
    try:
        update_data = {
            "url": request.form.get("url", "").strip(),
            "platform": request.form.get("platform", "").strip(),
            "product_count": int(request.form.get("product_count", 0)),
            "store_name": request.form.get("store_name", "").strip(),
            "currency": request.form.get("currency", "USD").strip(),
        }
        if db.update_unfiltered(category, domain, update_data):
            flash(f"已更新店铺: {domain}", "success")
        else:
            flash(f"更新失败或无变更: {domain}", "error")
    except Exception as e:
        flash(f"更新失败: {e}", "error")
    return redirect(url_for("shopify.shopify_fetch_urls", category=category))


@bp.route("/shopify/unfiltered/delete", methods=["POST"])
def shopify_unfiltered_delete():
    category = request.form.get("category", "").strip()
    if not category:
        flash("类目不能为空", "error")
        return redirect(url_for("shopify.shopify_fetch_urls"))

    domains = request.form.getlist("domains")
    single_domain = request.form.get("domain", "").strip()
    if single_domain and not domains:
        domains = [single_domain]

    if not domains:
        flash("请选择要删除的记录", "error")
        return redirect(url_for("shopify.shopify_fetch_urls", category=category))

    db = get_mongo_db()
    try:
        deleted = db.delete_unfiltered_many(category, domains)
        flash(f"已删除 {deleted} 条记录", "success")
    except Exception as e:
        flash(f"删除失败: {e}", "error")
    return redirect(url_for("shopify.shopify_fetch_urls", category=category))


@bp.route("/api/shopify/unfiltered/<category>/<domain>")
def api_shopify_unfiltered_get(category, domain):
    db = get_mongo_db()
    try:
        doc = db.get_unfiltered_by_domain(category, domain)
        if doc:
            return jsonify({"ok": True, "data": doc})
        return jsonify({"ok": False, "error": "未找到记录"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/shopify/filter-categories", methods=["GET", "POST"])
def shopify_filter_categories():
    selected_category = request.args.get("category", "")
    selected_subcategory = request.args.get("subcategory", "")
    filtered_stores = []
    filtered_total = 0
    available_subcategories = []

    # 获取每个一级分类的 unfiltered 数量
    unfiltered_counts = {}
    db = get_mongo_db()
    try:
        for cat in SHOPIFY_CATEGORIES:
            unfiltered_counts[cat] = db.get_unfiltered_count(cat)
    except Exception as e:
        log.error(f"获取 unfiltered 数量失败: {e}")

    if selected_category:
        try:
            # 获取该一级分类下所有可用的二级分类
            available_subcategories = db.list_filtered_subcategories(selected_category)
            if selected_subcategory:
                filtered_stores = db.get_filtered_stores(selected_category, limit=100, subcategory=selected_subcategory)
                filtered_total = db.get_filtered_count(selected_category, selected_subcategory)
        except Exception as e:
            log.error(f"查询 filtered 数据失败: {e}")

    if request.method == "POST":
        category = (request.form.get("category") or "").strip()
        subcategory = (request.form.get("subcategory") or "").strip()
        action = request.form.get("action", "filter")

        if action == "filter" and category:
            task_id = f"filter_{category}_{int(time.time())}"
            task_manager.create(task_id, "filter_categories", category)

            def run_task():
                from qmds.db.mongodb import MongoDBClient
                from qmds.modules.data_scraper.category_matcher import match_title
                from qmds.modules.data_scraper.collections_fetcher import fetch_collections
                db_inner = MongoDBClient()
                http = _get_http()
                try:
                    stores = db_inner.get_all_urls(category)
                    total = len(stores)
                    task_manager.add_log(task_id, f"任务启动: 精准类目筛选", "info")
                    task_manager.add_log(task_id, f"类目: {category}, 待处理: {total}", "info")
                    if total == 0:
                        task_manager.update(task_id, status="completed",
                                            message=f"类目 {category} 无待处理 URL", progress=100)
                        return

                    matched_count = 0
                    removed_count = 0
                    processed = 0
                    for store in stores:
                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped",
                                                message=f"已停止: {processed}/{total}, 匹配 {matched_count}")
                            return

                        store_url = store["url"]
                        domain = store["domain"]
                        processed += 1
                        try:
                            collections = fetch_collections(http, store_url)
                            task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - {len(collections)} collections", "info")
                            for coll in collections:
                                if match_title(category, coll["title"]):
                                    check_url = f"{store_url}/collections/{coll['handle']}/products.json?limit=1"
                                    try:
                                        check_resp = http.get(check_url, timeout=10)
                                        if check_resp.status_code == 200:
                                            products = check_resp.json().get("products", [])
                                            if not products:
                                                continue
                                    except Exception:
                                        pass
                                    # 自动筛选时 subcategory 未知，归入 "other"
                                    if db_inner.save_filtered_url(category, domain, store_url, coll["title"], coll["handle"]):
                                        matched_count += 1
                                        task_manager.add_log(task_id, f"匹配: {coll['title']}", "info")
                            if db_inner.delete_unfiltered(category, domain):
                                removed_count += 1
                        except Exception as e:
                            task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 失败: {e}", "warning")
                            if db_inner.delete_unfiltered(category, domain):
                                removed_count += 1

                        if processed % 10 == 0 or processed == total:
                            task_manager.update(task_id, progress=int(processed / total * 100),
                                                current=processed, total=total,
                                                message=f"处理中: {processed}/{total}, 匹配 {matched_count}")

                    summary = f"完成: 处理 {total}, 匹配 {matched_count}, 删除 {removed_count}"
                    task_manager.update(task_id, status="completed", message=summary,
                                        result={"total_stores": total, "matched": matched_count, "removed": removed_count},
                                        progress=100)
                    task_manager.add_log(task_id, summary, "info")
                except Exception as e:
                    log.error(f"精准类目任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    db_inner.close()

            task_manager.start_task_thread(task_id, run_task)
            flash(f"精准类目筛选任务已启动: {category}", "info")
            return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))

        elif action == "ai_fetch_info" and category:
            # 阶段1：抓取 unfiltered 集合中所有 URL 的首页信息，写入原文档 page_info 字段
            task_id = f"ai_fetch_info_{category}_{int(time.time())}"
            task_manager.create(task_id, "ai_fetch_info", category)

            def run_ai_fetch_info():
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from qmds.db.mongodb import MongoDBClient
                from qmds.modules.data_scraper.ai_classifier import fetch_page_info
                db_inner = MongoDBClient()
                try:
                    stores = db_inner.get_all_urls(category)
                    total = len(stores)
                    task_manager.add_log(task_id, "任务启动: 抓取网站信息（unfiltered）", "info")
                    task_manager.add_log(task_id, f"类目: {category}, 待处理: {total}", "info")

                    if total == 0:
                        task_manager.update(task_id, status="completed",
                                            message=f"类目 {category} 无待处理 URL", progress=100)
                        return

                    max_workers = min(20, max(1, settings.ai_batch_size))
                    max_rounds = 2

                    pm = ProxyManager.from_settings() if settings.load_proxies() else None
                    http = HttpClient(proxy_manager=pm)

                    def _fetch_one(store):
                        domain = store["domain"]
                        try:
                            page_info = fetch_page_info(domain, http, proxy_manager=pm)
                            db_inner.update_unfiltered_page_info(category, domain, page_info)
                            has_info = bool(page_info.get("homepage_content") or page_info.get("title") or page_info.get("nav_categories"))
                            return domain, page_info, has_info, None
                        except Exception as e:
                            return domain, {}, False, str(e)

                    total_success = 0
                    total_empty = 0
                    total_processed = 0
                    grand_total = total

                    current_batch = stores
                    for round_num in range(1, max_rounds + 1):
                        if not current_batch:
                            break
                        if task_manager.is_stopped(task_id):
                            task_manager.update(
                                task_id, status="stopped",
                                message=f"已停止: 轮次 {round_num}, 成功 {total_success}",
                            )
                            return

                        batch_size = len(current_batch)
                        task_manager.add_log(
                            task_id,
                            f"第 {round_num}/{max_rounds} 轮抓取开始，本轮 {batch_size} 条" + ("（重试信息为空）" if round_num > 1 else ""),
                            "info",
                        )

                        round_success = 0
                        round_empty_domains = []
                        processed_in_round = 0

                        with ThreadPoolExecutor(max_workers=max_workers) as pool:
                            futures = {pool.submit(_fetch_one, s): s for s in current_batch}
                            for future in as_completed(futures):
                                if task_manager.is_stopped(task_id):
                                    for f in futures:
                                        f.cancel()
                                    task_manager.update(
                                        task_id, status="stopped",
                                        message=f"已停止: 轮次 {round_num}, 成功 {total_success}",
                                    )
                                    return
                                processed_in_round += 1
                                total_processed += 1
                                try:
                                    domain, page_info, has_info, err = future.result()
                                    if err:
                                        total_empty += 1
                                        round_empty_domains.append({"domain": domain})
                                        if processed_in_round <= 20 or processed_in_round % 50 == 0:
                                            task_manager.add_log(task_id, f"  轮{round_num} [{processed_in_round}/{batch_size}] {domain} - 失败: {err[:80]}", "warning")
                                    elif has_info:
                                        total_success += 1
                                        round_success += 1
                                        if processed_in_round <= 20 or processed_in_round % 50 == 0:
                                            task_manager.add_log(task_id, f"  轮{round_num} [{processed_in_round}/{batch_size}] {domain} - 抓取成功", "info")
                                    else:
                                        total_empty += 1
                                        round_empty_domains.append({"domain": domain})
                                        if processed_in_round <= 20 or processed_in_round % 50 == 0:
                                            task_manager.add_log(task_id, f"  轮{round_num} [{processed_in_round}/{batch_size}] {domain} - 信息为空", "warning")
                                except Exception:
                                    total_empty += 1
                                    round_empty_domains.append({"domain": domain})

                                if processed_in_round % 10 == 0 or processed_in_round == batch_size:
                                    task_manager.update(
                                        task_id,
                                        progress=int(total_processed / grand_total * 100) if grand_total else 100,
                                        current=total_processed, total=grand_total,
                                        message=f"轮{round_num}/{max_rounds} [{processed_in_round}/{batch_size}] 成功 {total_success}, 空 {total_empty}",
                                    )

                        task_manager.add_log(
                            task_id,
                            f"第 {round_num} 轮完成: 本轮成功 {round_success}, 仍为空 {len(round_empty_domains)}",
                            "info",
                        )

                        current_batch = round_empty_domains
                        if not round_empty_domains or round_num == max_rounds:
                            if round_empty_domains and round_num == max_rounds:
                                task_manager.add_log(
                                    task_id,
                                    f"已达最大重试轮次 {max_rounds}，剩余 {len(round_empty_domains)} 条信息为空",
                                    "warning",
                                )
                            break

                    summary = f"完成: 总计 {grand_total}, 成功 {total_success}, 信息为空 {total_empty}"
                    task_manager.update(task_id, status="completed", message=summary, progress=100)
                    task_manager.add_log(task_id, summary, "info")
                except Exception as e:
                    log.error(f"抓取网站信息任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    db_inner.close()

            task_manager.start_task_thread(task_id, run_ai_fetch_info)
            flash(f"抓取网站信息任务已启动: {category}，可在任务页面查看进度", "info")
            return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))

        elif action == "ai_filter" and category:
            # 阶段2：从 unfiltered 集合读取已抓取的 page_info，调用 LLM 分类（无网络请求）
            task_id = f"ai_filter_{category}_{int(time.time())}"
            task_manager.create(task_id, "ai_filter_categories", category)

            def run_ai_task():
                from qmds.db.mongodb import (
                    MongoDBClient,
                    FILTER_FAIL_REASON_NON_ENGLISH,
                    FILTER_FAIL_REASON_BLACK_FIVE,
                    FILTER_FAIL_REASON_UNRECOGNIZED,
                )
                from qmds.modules.data_scraper.ai_classifier import (
                    classify_store,
                    is_non_english,
                    google_to_qmds_category,
                )
                db_inner = MongoDBClient()
                try:
                    stores = db_inner.get_unfiltered_for_classify(category)
                    total = len(stores)
                    task_manager.add_log(task_id, "任务启动: AI 智能分类（阶段2）", "info")
                    task_manager.add_log(task_id, f"类目: {category}, 待分类: {total}", "info")

                    if not settings.mimo_api_key:
                        task_manager.update(
                            task_id, status="failed",
                            message="失败: MIMO_API_KEY 未配置，无法调用 AI 分类",
                        )
                        task_manager.add_log(task_id, "MIMO_API_KEY 未配置，无法调用 AI 分类", "error")
                        return

                    if total == 0:
                        task_manager.update(task_id, status="completed",
                                            message=f"类目 {category} 无待分类 URL（请先运行阶段1抓取网站信息）", progress=100)
                        return

                    matched_count = 0
                    non_english_count = 0
                    black_five_count = 0
                    comprehensive_count = 0
                    relocated_count = 0
                    unrecognized_count = 0
                    processed = 0

                    for store in stores:
                        if task_manager.is_stopped(task_id):
                            task_manager.update(
                                task_id, status="stopped",
                                message=f"已停止: {processed}/{total}, 匹配 {matched_count}",
                            )
                            return

                        store_url = store.get("url", "")
                        domain = store.get("domain", "")
                        page_info = store.get("page_info") or {}
                        processed += 1

                        try:
                            # 非英文站检测
                            non_en, lang = is_non_english(page_info)
                            if non_en:
                                db_inner.save_filtered_failed(
                                    category=category,
                                    domain=domain,
                                    store_url=store_url,
                                    reason=FILTER_FAIL_REASON_NON_ENGLISH,
                                    language=lang or "",
                                )
                                non_english_count += 1
                                task_manager.add_log(
                                    task_id,
                                    f"[{processed}/{total}] {domain} - 非英文站 ({lang})",
                                    "info",
                                )
                            else:
                                # LLM 分类（http_client=None，无网络请求）
                                result = classify_store(page_info, domain, None)
                                llm_category = result.get("category", "")
                                llm_subcategory = result.get("subcategory", "")
                                source_subcategory = result.get("source_subcategory", "")

                                if result.get("is_filtered"):
                                    # 黑五类
                                    bf_type = result.get("black_five_type", "未分类")
                                    db_inner.save_filtered_failed(
                                        category=category,
                                        domain=domain,
                                        store_url=store_url,
                                        reason=FILTER_FAIL_REASON_BLACK_FIVE,
                                        black_five_type=bf_type,
                                        subcategory_raw=source_subcategory,
                                    )
                                    black_five_count += 1
                                    task_manager.add_log(
                                        task_id,
                                        f"[{processed}/{total}] {domain} - 黑五类 ({bf_type})",
                                        "warning",
                                    )
                                elif result.get("is_comprehensive") or llm_category == "综合站":
                                    # 综合站 -> 写入 comprehensive_stores 集合
                                    subs_list = result.get("subcategories", [])
                                    db_inner.save_comprehensive_store(
                                        domain=domain,
                                        store_url=store_url,
                                        subcategories=subs_list,
                                        source="ai_classifier",
                                        source_subcategory=source_subcategory,
                                    )
                                    # 从 unfiltered 集合删除
                                    db_inner.delete_unfiltered(category, domain)
                                    comprehensive_count += 1
                                    task_manager.add_log(
                                        task_id,
                                        f"[{processed}/{total}] {domain} - 综合站 (subcategories: {subs_list})",
                                        "info",
                                    )
                                elif llm_category == "无法识别":
                                    # 无法识别
                                    db_inner.save_filtered_failed(
                                        category=category,
                                        domain=domain,
                                        store_url=store_url,
                                        reason=FILTER_FAIL_REASON_UNRECOGNIZED,
                                    )
                                    unrecognized_count += 1
                                    task_manager.add_log(
                                        task_id,
                                        f"[{processed}/{total}] {domain} - 无法识别",
                                        "warning",
                                    )
                                else:
                                    # 正常分类：Google 分类名 -> QMDS 简化名
                                    qmds_cat = google_to_qmds_category(llm_category)
                                    if qmds_cat is None:
                                        # 未知 Google 分类，归入无法识别
                                        db_inner.save_filtered_failed(
                                            category=category,
                                            domain=domain,
                                            store_url=store_url,
                                            reason=FILTER_FAIL_REASON_UNRECOGNIZED,
                                            subcategory_raw=llm_category,
                                        )
                                        unrecognized_count += 1
                                        task_manager.add_log(
                                            task_id,
                                            f"[{processed}/{total}] {domain} - 未知分类: {llm_category}",
                                            "warning",
                                        )
                                    elif qmds_cat != category:
                                        # 类目不匹配：LLM 认为属于其他一级分类，移动到目标大类对应二级分类集合
                                        sub_parts = [p.strip() for p in llm_subcategory.split(">")]
                                        sub_name = sub_parts[-1] if sub_parts else "other"
                                        # 校验：非标准 subcategory 归入 other，防止碎片集合
                                        if normalize_subcategory(sub_name) not in get_standard_subcategories(qmds_cat):
                                            sub_name = DEFAULT_SUBCATEGORY
                                        db_inner.save_ai_classified(
                                            category=qmds_cat,
                                            domain=domain,
                                            subcategory=sub_name,
                                            store_url=store_url,
                                            source_subcategory=source_subcategory,
                                            from_category=category,
                                        )
                                        relocated_count += 1
                                        task_manager.add_log(
                                            task_id,
                                            f"[{processed}/{total}] {domain} - 跨大类迁移 ({category} -> {qmds_cat}__{sub_name})",
                                            "info",
                                        )
                                    else:
                                        # 类目匹配，写入 {category}__{subcategory}
                                        sub_parts = [p.strip() for p in llm_subcategory.split(">")]
                                        sub_name = sub_parts[-1] if sub_parts else "other"
                                        # 校验：非标准 subcategory 归入 other，防止碎片集合
                                        if normalize_subcategory(sub_name) not in get_standard_subcategories(category):
                                            sub_name = DEFAULT_SUBCATEGORY
                                        db_inner.save_ai_classified(
                                            category=category,
                                            domain=domain,
                                            subcategory=sub_name,
                                            store_url=store_url,
                                            source_subcategory=source_subcategory,
                                        )
                                        matched_count += 1
                                        task_manager.add_log(
                                            task_id,
                                            f"[{processed}/{total}] {domain} - 匹配: {llm_subcategory}",
                                            "info",
                                        )
                            # 标记该 domain 已分类
                            db_inner.mark_unfiltered_classified(category, domain)
                        except Exception as e:
                            task_manager.add_log(
                                task_id,
                                f"[{processed}/{total}] {domain} - 失败: {e}",
                                "warning",
                            )

                        if processed % 10 == 0 or processed == total:
                            task_manager.update(
                                task_id,
                                progress=int(processed / total * 100),
                                current=processed, total=total,
                                message=(f"处理中: {processed}/{total}, "
                                         f"匹配 {matched_count}, 过滤失败 "
                                         f"{non_english_count + black_five_count + comprehensive_count + relocated_count + unrecognized_count}"),
                            )

                    filtered_total = (non_english_count + black_five_count + comprehensive_count
                                       + relocated_count + unrecognized_count)
                    summary = (f"完成: 处理 {total}, 匹配 {matched_count}, "
                               f"非英文 {non_english_count}, 黑五类 {black_five_count}, "
                               f"综合站 {comprehensive_count}, 跨大类迁移 {relocated_count}, "
                               f"无法识别 {unrecognized_count}")
                    task_manager.update(
                        task_id, status="completed", message=summary,
                        result={
                            "total_stores": total,
                            "matched": matched_count,
                            "non_english": non_english_count,
                            "black_five": black_five_count,
                            "comprehensive": comprehensive_count,
                            "relocated": relocated_count,
                            "unrecognized": unrecognized_count,
                            "filtered_failed": filtered_total,
                        },
                        progress=100,
                    )
                    task_manager.add_log(task_id, summary, "info")
                except Exception as e:
                    log.error(f"AI 分类任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    db_inner.close()

            task_manager.start_task_thread(task_id, run_ai_task)
            flash(f"AI 智能分类任务已启动: {category}，可在任务页面查看进度", "info")
            return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))

        elif action == "delete_selected" and selected_category and selected_subcategory:
            selected_ids = request.form.getlist("selected_ids")
            if selected_ids:
                db = get_mongo_db()
                try:
                    count = db.delete_filtered_many(selected_category, selected_ids, subcategory=selected_subcategory)
                    flash(f"已删除 {count} 条记录", "success")
                except Exception as e:
                    flash(f"删除 filtered 记录失败: {e}", "error")
            return redirect(url_for("shopify.shopify_filter_categories", category=selected_category, subcategory=selected_subcategory))

    return render_template("shopify_categories.html",
                           categories=SHOPIFY_CATEGORIES,
                           selected_category=selected_category,
                           selected_subcategory=selected_subcategory,
                           available_subcategories=available_subcategories,
                           filtered_stores=filtered_stores,
                           filtered_total=filtered_total,
                           unfiltered_counts=unfiltered_counts)


@bp.route("/shopify/filter-categories/<category>/<doc_id>/edit", methods=["GET", "POST"])
def shopify_filter_edit(category, doc_id):
    db = get_mongo_db()
    # 从查询参数获取当前 subcategory，默认为 "other"
    subcategory = request.args.get("subcategory", "other")
    try:
        if request.method == "POST":
            new_subcategory = request.form.get("subcategory", "").strip()
            old_subcategory = request.form.get("old_subcategory", "other").strip()

            updates = {
                "domain": request.form.get("domain", "").strip(),
                "store_url": request.form.get("store_url", "").strip(),
                "url": request.form.get("url", "").strip(),
                "collection_title": request.form.get("collection_title", "").strip(),
                "collection_handle": request.form.get("collection_handle", "").strip(),
                "subcategory": normalize_subcategory(new_subcategory),
            }

            # 如果二级分类变更了，需要从旧集合删除并写入新集合
            if normalize_subcategory(new_subcategory) != normalize_subcategory(old_subcategory):
                # 读取旧记录
                doc = db.get_filtered_by_id(category, doc_id, subcategory=old_subcategory)
                if not doc:
                    flash("记录不存在", "error")
                    return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=old_subcategory))
                # 在新集合中创建（设置 filter_status / crawl_status 字段）
                new_doc = {k: v for k, v in doc.items() if k != "_id"}
                new_doc.update(updates)
                new_doc["category"] = category
                new_doc["filter_status"] = new_doc.get("filter_status", "filtered")
                new_doc["crawl_status"] = new_doc.get("crawl_status", "uncrawled")
                new_col = db.filtered_col(category, new_subcategory)
                new_col.update_one(
                    {"domain": new_doc.get("domain", ""), "collection_handle": new_doc.get("collection_handle", "")},
                    {"$set": new_doc},
                    upsert=True,
                )
                # 从旧集合删除
                db.delete_filtered_by_id(category, doc_id, subcategory=old_subcategory)
                flash(f"记录已更新并迁移到二级分类: {normalize_subcategory(new_subcategory)}", "success")
                return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=normalize_subcategory(new_subcategory)))
            else:
                if db.update_filtered_by_id(category, doc_id, updates, subcategory=old_subcategory):
                    flash("记录已更新", "success")
                else:
                    flash("更新失败", "error")
                return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=old_subcategory))

        doc = db.get_filtered_by_id(category, doc_id, subcategory=subcategory)
        if not doc:
            flash("记录不存在", "error")
            return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))

        return render_template("shopify_filter_edit.html", category=category, subcategory=subcategory, doc=doc)
    except Exception as e:
        log.error(f"编辑 filtered 记录失败: {e}")
        flash(f"操作失败: {e}", "error")
        return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))


@bp.route("/shopify/filter-categories/add", methods=["POST"])
def shopify_filter_add():
    category = request.form.get("category", "").strip()
    store_url = request.form.get("store_url", "").strip()
    collection_url = request.form.get("collection_url", "").strip()
    subcategory = request.form.get("subcategory", "").strip()

    if not category or not collection_url:
        flash("类目和 Collection URL 不能为空", "error")
        return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))

    db = get_mongo_db()
    try:
        if db.add_filtered_manual(category, store_url, collection_url, subcategory=subcategory):
            flash("已添加记录", "success")
        else:
            flash("添加失败", "error")
    except Exception as e:
        flash(f"添加失败: {e}", "error")
    return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=normalize_subcategory(subcategory)))


@bp.route("/shopify/filter-categories/import", methods=["POST"])
def shopify_filter_import():
    category = request.form.get("category", "").strip()
    if not category:
        flash("类目不能为空", "error")
        return redirect(url_for("shopify.shopify_filter_categories"))

    urls_to_add = []
    text_subcategory = request.form.get("subcategory", "").strip()

    file = request.files.get("file")
    if file and file.filename:
        if not file.filename.endswith(('.xlsx', '.xls', '.txt')):
            flash("请上传 Excel 或文本文件", "error")
            return redirect(url_for("shopify.shopify_filter_categories", category=category))

        filepath = os.path.join(os.getcwd(), "uploads", file.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        file.save(filepath)

        if file.filename.endswith('.txt'):
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        entry = {"subcategory": text_subcategory}
                        if '/collections/' in line:
                            entry["collection_url"] = line
                        elif '.' in line:
                            url_val = line if line.startswith('http') else f"https://{line}"
                            entry["store_url"] = url_val
                        urls_to_add.append(entry)
        else:
            import pandas as pd
            df = pd.read_excel(filepath)
            for _, row in df.iterrows():
                store_url = str(row.get("store_url", "") or row.get("店铺URL", "") or "").strip()
                collection_url = str(row.get("collection_url", "") or row.get("collection URL", "") or "").strip()
                subcategory_val = str(row.get("subcategory", "") or row.get("二级分类", "") or "").strip()
                if collection_url:
                    urls_to_add.append({"store_url": store_url, "collection_url": collection_url, "subcategory": subcategory_val})

    urls_text = request.form.get("urls", "").strip()
    if urls_text:
        for line in urls_text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                entry = {"subcategory": text_subcategory}
                if '/collections/' in line:
                    entry["collection_url"] = line
                elif '.' in line:
                    url_val = line if line.startswith('http') else f"https://{line}"
                    entry["store_url"] = url_val
                urls_to_add.append(entry)

    if not urls_to_add:
        flash("未找到有效的 URL", "error")
        return redirect(url_for("shopify.shopify_filter_categories", category=category))

    db = get_mongo_db()
    try:
        result = db.add_filtered_batch(category, urls_to_add, subcategory=text_subcategory)
        messages = [f"新增: {result['created']}", f"更新: {result['updated']}"]
        if result['errors']:
            messages.append(f"错误: {len(result['errors'])}")
            for error in result['errors'][:5]:
                flash(error, "error")
        flash(f"导入完成: {', '.join(messages)}", "success")
    except Exception as e:
        flash(f"导入失败: {e}", "error")
    return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=normalize_subcategory(text_subcategory)))


@bp.route("/shopify/filter-categories/<category>/<doc_id>/delete", methods=["POST"])
def shopify_filter_delete(category, doc_id):
    from qmds.db.mongodb import MongoDBClient
    subcategory = request.form.get("subcategory", "other").strip()
    db = MongoDBClient()
    try:
        if db.delete_filtered_by_id(category, doc_id, subcategory=subcategory):
            flash("记录已删除", "success")
        else:
            flash("删除失败", "error")
    except Exception as e:
        log.error(f"删除 filtered 记录失败: {e}")
        flash(f"删除失败: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("shopify.shopify_filter_categories", category=category, subcategory=subcategory))


@bp.route("/shopify/subcategory-management")
def shopify_subcategory_management():
    """二级分类管理页面：展示各一级分类下的二级分类及数据量"""
    db = get_mongo_db()
    try:
        # 一次从 _counters 读取所有 filtered 集合的计数
        counts_data = db.get_all_collection_counts()
        filtered_list = counts_data.get("filtered", [])

        # 按一级分类分组
        category_map = {}
        for item in filtered_list:
            cat = item.get("category", "")
            sub = item.get("subcategory", "")
            if cat not in category_map:
                category_map[cat] = []
            counts = item.get("counts", {})
            category_map[cat].append({
                "subcategory": sub,
                "prefix": item.get("_id", ""),
                "count": counts.get("uncrawled", 0),
            })

        # 按一级分类名排序
        sorted_categories = sorted(category_map.items())

        return render_template("subcategory_management.html",
                               categories=sorted_categories)
    except Exception as e:
        log.error(f"二级分类管理页面加载失败: {e}")
        return render_template("subcategory_management.html",
                               categories=[],
                               error=str(e))


@bp.route("/shopify/model-filter-import", methods=["GET", "POST"])
def shopify_model_filter_import():
    """模型筛站导入：从 cc_c.shopify_site_01 导入到 {category}__{subcategory} 集合

    支持三种 action：
      - import（默认）: 直接导入，信任源 subcategory 字段
      - ai_fetch_info: 在 cc_c.shopify_site 上抓取 page_info 并写回源文档（仅处理 page_info 为空且无 category/subcategory 的记录）
      - ai_filter: 在 cc_c.shopify_site 上调用 LLM 分类，将结果写回源文档
    """
    if request.method == "POST":
        action = (request.form.get("action") or "import").strip()
        category = (request.form.get("category") or "").strip()
        source_db = (request.form.get("source_db") or "cc_c").strip()
        source_collection = (request.form.get("source_collection") or "shopify_site_01").strip()
        batch_size = int(request.form.get("batch_size", 500))

        # ===== AI 抓取网站信息：在 cc_c.shopify_site 源集合上操作 =====
        if action == "ai_fetch_info":
            if not settings.mimo_api_key:
                flash("MIMO_API_KEY 未配置，无法进行 AI 分类流程", "error")
                return redirect(url_for("shopify.shopify_model_filter_import"))

            src_collection = (request.form.get("source_collection") or "shopify_site").strip()
            task_id = f"shopify_site_fetch_{int(time.time())}"
            task_manager.create(task_id, "shopify_site_ai_fetch", "")

            def run_fetch_task():
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from qmds.db.mongodb import MongoDBClient
                from qmds.modules.data_scraper.ai_classifier import fetch_page_info
                db_inner = MongoDBClient()
                try:
                    task_manager.add_log(task_id, "任务启动: 抓取网站信息（cc_c.shopify_site）", "info")
                    task_manager.add_log(task_id, f"源: {source_db}.{src_collection}", "info")

                    stores = db_inner.get_shopify_site_pending_for_fetch(source_db, src_collection)
                    total = len(stores)
                    task_manager.add_log(task_id, f"待抓取记录（page_info为空且无category/subcategory）: {total} 条", "info")

                    if total == 0:
                        task_manager.update(task_id, status="completed",
                                            message="完成: 无待抓取记录", progress=100)
                        return

                    max_workers = min(20, max(1, settings.ai_batch_size))
                    pm = ProxyManager.from_settings() if settings.load_proxies() else None
                    http = HttpClient(proxy_manager=pm)

                    total_success = 0
                    total_empty = 0
                    total_processed = 0

                    def _fetch_one(store):
                        doc_id = store["_id"]
                        domain = (store.get("domain") or "").strip()
                        try:
                            page_info = fetch_page_info(domain, http, proxy_manager=pm)
                            db_inner.update_shopify_site_page_info(
                                doc_id, page_info, source_db, src_collection
                            )
                            has_info = bool(
                                page_info.get("homepage_content")
                                or page_info.get("title")
                                or page_info.get("nav_categories")
                            )
                            return domain, has_info, None
                        except Exception as e:
                            return domain, False, str(e)

                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        futures = {pool.submit(_fetch_one, s): s for s in stores}
                        for future in as_completed(futures):
                            if task_manager.is_stopped(task_id):
                                for f in futures:
                                    f.cancel()
                                task_manager.update(
                                    task_id, status="stopped",
                                    message=f"已停止: 成功 {total_success}/{total}",
                                )
                                return
                            total_processed += 1
                            try:
                                domain, has_info, err = future.result()
                                if err:
                                    total_empty += 1
                                    if total_processed <= 20 or total_processed % 50 == 0:
                                        task_manager.add_log(task_id, f"  [{total_processed}/{total}] {domain} - 失败: {err[:80]}", "warning")
                                elif has_info:
                                    total_success += 1
                                    if total_processed <= 20 or total_processed % 50 == 0:
                                        task_manager.add_log(task_id, f"  [{total_processed}/{total}] {domain} - 抓取成功", "info")
                                else:
                                    total_empty += 1
                                    if total_processed <= 20 or total_processed % 50 == 0:
                                        task_manager.add_log(task_id, f"  [{total_processed}/{total}] {domain} - 信息为空", "warning")
                            except Exception:
                                total_empty += 1

                            if total_processed % 10 == 0 or total_processed == total:
                                task_manager.update(
                                    task_id,
                                    progress=int(total_processed / total * 100),
                                    current=total_processed, total=total,
                                    message=f"处理中: {total_processed}/{total}, 成功 {total_success}, 空 {total_empty}",
                                )

                    summary = f"完成: 总计 {total}, 成功 {total_success}, 信息为空 {total_empty}"
                    task_manager.update(task_id, status="completed", message=summary, progress=100)
                    task_manager.add_log(task_id, summary, "info")
                except Exception as e:
                    log.error(f"shopify_site 抓取信息任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    db_inner.close()

            task_manager.start_task_thread(task_id, run_fetch_task)
            flash(f"抓取网站信息任务已启动，可在任务页面查看进度", "info")
            return redirect(url_for("shopify.shopify_model_filter_import"))

        # ===== AI 智能分类：在 cc_c.shopify_site 源集合上操作 =====
        if action == "ai_filter":
            if not settings.mimo_api_key:
                flash("MIMO_API_KEY 未配置，无法调用 AI 分类", "error")
                return redirect(url_for("shopify.shopify_model_filter_import"))

            src_collection = (request.form.get("source_collection") or "shopify_site").strip()
            task_id = f"shopify_site_filter_{int(time.time())}"
            task_manager.create(task_id, "shopify_site_ai_filter", "")

            def run_classify_task():
                from qmds.db.mongodb import MongoDBClient
                from qmds.modules.data_scraper.ai_classifier import (
                    classify_store, is_non_english,
                )
                db_inner = MongoDBClient()
                try:
                    task_manager.add_log(task_id, "任务启动: AI 智能分类（cc_c.shopify_site）", "info")
                    task_manager.add_log(task_id, f"源: {source_db}.{src_collection}", "info")

                    stores = db_inner.get_shopify_site_pending_for_classify(source_db, src_collection)
                    total = len(stores)
                    task_manager.add_log(task_id, f"待分类记录（已抓取page_info且未分类）: {total} 条", "info")

                    if total == 0:
                        task_manager.update(task_id, status="completed",
                                            message="完成: 无待分类记录", progress=100)
                        return

                    matched_count = 0
                    non_english_count = 0
                    black_five_count = 0
                    comprehensive_count = 0
                    unrecognized_count = 0
                    processed = 0

                    for store in stores:
                        if task_manager.is_stopped(task_id):
                            task_manager.update(
                                task_id, status="stopped",
                                message=f"已停止: {processed}/{total}, 匹配 {matched_count}",
                            )
                            return

                        doc_id = store["_id"]
                        domain = (store.get("domain") or "").strip()
                        page_info = store.get("page_info") or {}
                        processed += 1

                        try:
                            # 非英文站检测
                            non_en, lang = is_non_english(page_info)
                            if non_en:
                                db_inner.update_shopify_site_classification(
                                    doc_id, ai_status="non_english",
                                    extra={"ai_language": lang or ""},
                                    source_db_name=source_db, source_collection=src_collection,
                                )
                                non_english_count += 1
                                task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 非英文站 ({lang})", "info")
                            else:
                                # LLM 分类
                                result = classify_store(page_info, domain, None)
                                llm_category = result.get("category", "")
                                llm_subcategory = result.get("subcategory", "")

                                if result.get("is_filtered"):
                                    # 黑五类
                                    bf_type = result.get("black_five_type", "未分类")
                                    db_inner.update_shopify_site_classification(
                                        doc_id, ai_status="black_five",
                                        ai_black_five_type=bf_type,
                                        ai_subcategory=llm_subcategory,
                                        source_db_name=source_db, source_collection=src_collection,
                                    )
                                    black_five_count += 1
                                    task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 黑五类 ({bf_type})", "warning")
                                elif result.get("is_comprehensive") or llm_category == "综合站":
                                    # 综合站
                                    subs_list = result.get("subcategories", [])
                                    db_inner.update_shopify_site_classification(
                                        doc_id, ai_category="综合站",
                                        ai_subcategory=", ".join(subs_list) if subs_list else "",
                                        ai_status="comprehensive",
                                        ai_subcategories=subs_list,
                                        source_db_name=source_db, source_collection=src_collection,
                                    )
                                    comprehensive_count += 1
                                    task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 综合站 (subcategories: {subs_list})", "info")
                                elif llm_category == "无法识别":
                                    db_inner.update_shopify_site_classification(
                                        doc_id, ai_status="unrecognized",
                                        source_db_name=source_db, source_collection=src_collection,
                                    )
                                    unrecognized_count += 1
                                    task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 无法识别", "warning")
                                else:
                                    # 正常分类：写入 category/subcategory（Google Taxonomy 格式）
                                    db_inner.update_shopify_site_classification(
                                        doc_id, ai_category=llm_category,
                                        ai_subcategory=llm_subcategory,
                                        ai_status="classified",
                                        source_db_name=source_db, source_collection=src_collection,
                                    )
                                    matched_count += 1
                                    task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 匹配: {llm_subcategory}", "info")
                        except Exception as e:
                            task_manager.add_log(task_id, f"[{processed}/{total}] {domain} - 失败: {e}", "warning")

                        if processed % 10 == 0 or processed == total:
                            task_manager.update(
                                task_id,
                                progress=int(processed / total * 100),
                                current=processed, total=total,
                                message=(f"处理中: {processed}/{total}, "
                                         f"匹配 {matched_count}, 非英文 {non_english_count}, "
                                         f"黑五 {black_five_count}, 综合 {comprehensive_count}, "
                                         f"未识别 {unrecognized_count}"),
                            )

                    filtered_total = (non_english_count + black_five_count
                                      + comprehensive_count + unrecognized_count)
                    summary = (f"完成: 处理 {total}, 匹配 {matched_count}, "
                               f"非英文 {non_english_count}, 黑五类 {black_five_count}, "
                               f"综合站 {comprehensive_count}, 无法识别 {unrecognized_count}")
                    task_manager.update(
                        task_id, status="completed", message=summary,
                        result={
                            "total": total, "matched": matched_count,
                            "non_english": non_english_count,
                            "black_five": black_five_count,
                            "comprehensive": comprehensive_count,
                            "unrecognized": unrecognized_count,
                            "filtered_failed": filtered_total,
                        },
                        progress=100,
                    )
                    task_manager.add_log(task_id, summary, "info")
                except Exception as e:
                    log.error(f"shopify_site AI分类任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"失败: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    db_inner.close()

            task_manager.start_task_thread(task_id, run_classify_task)
            flash(f"AI 智能分类任务已启动，可在任务页面查看进度", "info")
            return redirect(url_for("shopify.shopify_model_filter_import"))

        # ===== 普通导入模式（原有逻辑） =====
        if not category:
            flash("请选择一级分类", "error")
            return redirect(url_for("shopify.shopify_model_filter_import"))

        is_all = category == "__all__"
        task_id = f"model_filter_{category}_{int(time.time())}"
        task_manager.create(task_id, "model_filter_import", category)

        def run_task():
            from qmds.db.mongodb import MongoDBClient
            db_inner = MongoDBClient()
            try:
                task_manager.add_log(task_id, f"任务启动: 模型筛站导入", "info")
                if is_all:
                    task_manager.add_log(task_id, f"类目: 全部类目", "info")
                else:
                    task_manager.add_log(task_id, f"类目: {category}", "info")
                task_manager.add_log(task_id, f"源: {source_db}.{source_collection}", "info")

                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    return

                def progress_cb(processed, total, message):
                    task_manager.update(
                        task_id,
                        progress=int(processed / total * 100) if total else 0,
                        current=processed, total=total,
                        message=message,
                    )
                    if processed % 100 == 0 or processed == total:
                        task_manager.add_log(task_id, message, "info")

                def stop_cb():
                    return task_manager.is_stopped(task_id)

                if is_all:
                    # 预查询总数
                    from qmds.config.categories import SHOPIFY_CATEGORIES, get_google_category_name
                    src_col = db_inner.get_collection(source_db, source_collection)
                    google_cats = [get_google_category_name(c) for c in SHOPIFY_CATEGORIES]
                    pending = src_col.count_documents(
                        {"category": {"$in": google_cats}, "extracted": {"$ne": True}}
                    )
                    task_manager.add_log(task_id, f"待提取记录（全部类目）: {pending} 条", "info")

                    if pending == 0:
                        task_manager.update(
                            task_id, status="completed",
                            message="完成: 无待提取记录",
                            result={"total": 0, "imported": 0, "skipped": 0, "errors": []},
                            progress=100,
                        )
                        return

                    result = db_inner.import_all_from_shopify_site_01(
                        source_db_name=source_db,
                        source_collection=source_collection,
                        batch_size=batch_size,
                        progress_callback=progress_cb,
                        stop_check=stop_cb,
                    )
                else:
                    # 预查询总数
                    from qmds.config.categories import get_google_category_name
                    google_cat = get_google_category_name(category)
                    src_col = db_inner.get_collection(source_db, source_collection)
                    pending = src_col.count_documents(
                        {"category": google_cat, "extracted": {"$ne": True}}
                    )
                    task_manager.add_log(task_id, f"待提取记录: {pending} 条", "info")

                    if pending == 0:
                        task_manager.update(
                            task_id, status="completed",
                            message=f"完成: 无待提取记录（Google 分类: {google_cat}）",
                            result={"total": 0, "imported": 0, "skipped": 0, "errors": []},
                            progress=100,
                        )
                        return

                    result = db_inner.import_from_shopify_site_01(
                        category=category,
                        source_db_name=source_db,
                        source_collection=source_collection,
                        batch_size=batch_size,
                        progress_callback=progress_cb,
                        stop_check=stop_cb,
                    )

                if task_manager.is_stopped(task_id):
                    task_manager.update(
                        task_id, status="stopped",
                        message=f"已停止: 处理 {result['total']}, 导入 {result['imported']}",
                    )
                    return

                summary = (f"完成: 总计 {result['total']}, 导入 {result['imported']}, "
                           f"跳过 {result['skipped']}, 错误 {len(result['errors'])}")
                task_manager.update(
                    task_id, status="completed", message=summary,
                    result=result, progress=100,
                )
                task_manager.add_log(task_id, summary, "info")

                # 类目分布日志（仅批量模式）
                cat_stats = result.get("category_stats", {})
                if cat_stats:
                    task_manager.add_log(task_id, "各类目导入分布:", "info")
                    for cat, cnt in sorted(cat_stats.items(), key=lambda x: -x[1]):
                        if cnt > 0:
                            task_manager.add_log(task_id, f"  {cat}: {cnt} 条", "info")

                # 二级分类分布日志
                stats = result.get("subcategory_stats", {})
                if stats:
                    task_manager.add_log(task_id, "二级分类分布:", "info")
                    for sub, cnt in sorted(stats.items(), key=lambda x: -x[1])[:30]:
                        task_manager.add_log(task_id, f"  {sub}: {cnt} 条", "info")
                    if len(stats) > 30:
                        task_manager.add_log(
                            task_id, f"  ... 还有 {len(stats) - 30} 个二级分类未显示", "info"
                        )

                # 错误日志（最多记录 20 条）
                for err in result["errors"][:20]:
                    task_manager.add_log(task_id, err, "warning")
                if len(result["errors"]) > 20:
                    task_manager.add_log(
                        task_id, f"... 还有 {len(result['errors']) - 20} 个错误未显示", "warning"
                    )
            except Exception as e:
                log.error(f"模型筛站导入任务异常: {e}")
                task_manager.update(task_id, status="failed", message=f"失败: {e}")
                task_manager.add_log(task_id, f"任务异常: {e}", "error")
            finally:
                db_inner.close()

        task_manager.start_task_thread(task_id, run_task)
        if is_all:
            flash(f"模型筛站导入任务已启动: 全部类目，可在任务页面查看进度", "info")
        else:
            flash(f"模型筛站导入任务已启动: {category}，可在任务页面查看进度", "info")
        return redirect(url_for("shopify.shopify_model_filter_import", category=category))

    # GET: 渲染页面
    selected_category = request.args.get("category", "")
    return render_template(
        "shopify_model_filter.html",
        categories=SHOPIFY_CATEGORIES,
        selected_category=selected_category,
    )
