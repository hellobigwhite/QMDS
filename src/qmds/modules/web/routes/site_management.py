import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for, send_file, g

from qmds.config import settings
from qmds.db.site_db import SiteDBClient
from qmds.modules.web.db_helpers import get_site_db
from qmds.modules.web.task_manager import task_manager, make_progress_callback
from qmds.utils.logger import get_logger
from qmds.utils.domain_reporter import DomainReporter, DOMAIN_STATUS_LABELS, REPORT_API_BASE_URL, REPORT_CATEGORY_ID_MAP

log = get_logger("web")

bp = Blueprint("site_management", __name__)


@bp.route("/site-management", methods=["GET"])
def site_management():
    """建站管理主页 - 显示统计概览"""
    site_db = get_site_db()
    try:
        stats = site_db.get_stats()
    except Exception as e:
        log.error(f"获取站点统计失败: {e}")
        stats = {"total_sites": 0, "local_sites": 0, "reported_sites": 0, "scheduled_sites": 0, "built_sites": 0}
    return render_template("site_management.html", stats=stats)


@bp.route("/site-management/local", methods=["GET", "POST"])
def site_local():
    """本地站点管理"""
    site_db = get_site_db()
    try:
        q = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))

        if request.method == "POST":
            action = request.form.get("action", "")

            if action == "add":
                domain = request.form.get("domain", "").strip()
                if domain:
                    site_data = {
                        "domain": domain,
                        "template": request.form.get("template", ""),
                        "server": request.form.get("server", ""),
                        "category": request.form.get("category", ""),
                        "main_category": request.form.get("main_category", ""),
                        "main_data_source_id": request.form.get("main_data_source_id", ""),
                        "extra_data_source_id": request.form.get("extra_data_source_id", ""),
                        "title": request.form.get("title", ""),
                        "description": request.form.get("description", ""),
                        "address": request.form.get("address", ""),
                    }
                    site_db.add_site(site_data)
                    flash(f"站点 {domain} 已添加", "success")
                return redirect(url_for("site_management.site_local"))

            elif action == "import":
                file = request.files.get("file")
                if file and file.filename:
                    filepath = os.path.join(os.getcwd(), "uploads", file.filename)
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    file.save(filepath)
                    result = site_db.import_from_excel(filepath)

                    messages = []
                    messages.append(f"新增: {result['created']}")
                    messages.append(f"更新: {result['updated']}")
                    messages.append(f"跳过: {result['skipped']}")

                    if result['errors']:
                        messages.append(f"错误: {len(result['errors'])}")
                        for error in result['errors'][:5]:
                            flash(error, "error")
                        if len(result['errors']) > 5:
                            flash(f"还有 {len(result['errors']) - 5} 个错误...", "error")

                    flash(f"导入完成: {', '.join(messages)}", "success")
                return redirect(url_for("site_management.site_local"))

            elif action == "delete_selected":
                selected_ids = request.form.getlist("selected_ids")
                if selected_ids:
                    count = site_db.delete_sites_by_ids(selected_ids)
                    flash(f"已删除 {count} 个站点", "success")
                return redirect(url_for("site_management.site_local"))

            elif action == "report_selected":
                selected_ids = request.form.getlist("selected_ids")
                if not selected_ids:
                    flash("请先勾选要上报的站点", "error")
                    return redirect(url_for("site_management.site_local"))

                task_id = f"report_sites_{int(time.time())}"
                task_manager.create(task_id, "report_domains", f"域名上报 ({len(selected_ids)} sites)")

                def run_report_task():
                    _site_db = SiteDBClient()
                    try:
                        _settings = _site_db.get_all_settings()
                        username = _settings.get("report_username", "")
                        password = _settings.get("report_password", "")
                        if not username or not password:
                            task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                            task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                            return

                        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                        total = len(selected_ids)
                        success = 0
                        failed = 0
                        errors = []
                        task_manager.add_log(task_id, f"任务启动: 域名上报", "info")
                        task_manager.add_log(task_id, f"待上报站点: {total}", "info")

                        for i, site_id in enumerate(selected_ids):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return

                            site = _site_db.get_site_by_id(site_id)
                            if not site:
                                failed += 1
                                errors.append(f"ID {site_id}: 站点不存在")
                                task_manager.add_log(task_id, f"ID {site_id}: 站点不存在", "error")
                                continue

                            domain = (site.get("domain") or "").strip()
                            server = (site.get("server") or "").strip()
                            template = (site.get("template") or "").strip()
                            category_name = (site.get("category") or "").strip()

                            current = i + 1

                            missing = []
                            if not domain:
                                missing.append("域名")
                            if not server:
                                missing.append("服务器")
                            if not template:
                                missing.append("模板")
                            if not category_name:
                                missing.append("大类")

                            if missing:
                                failed += 1
                                errors.append(f"{domain or site_id}: 缺少字段 {', '.join(missing)}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain or site_id} - 缺少字段")
                                task_manager.add_log(task_id, f"[{current}/{total}] {domain or site_id} - 缺少字段: {', '.join(missing)}", "error")
                                continue

                            category_id = REPORT_CATEGORY_ID_MAP.get(category_name)
                            if not category_id:
                                failed += 1
                                errors.append(f"{domain}: 无效分类 {category_name}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain} - 无效分类")
                                task_manager.add_log(task_id, f"[{current}/{total}] {domain} - 无效分类: {category_name}", "error")
                                continue

                            payload = {
                                "name": domain,
                                "serverip": server,
                                "template": template,
                                "category": category_id,
                                "categoryTag": None,
                                "language": None,
                            }

                            try:
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.5) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 正在上报...")
                                task_manager.add_log(task_id, f"[{current}/{total}] [{domain}] 正在上报...", "info")
                                reporter.submit_domain(payload)

                                report_id = ""
                                domain_status = ""
                                try:
                                    info = reporter.fetch_domain_info(domain)
                                    report_id = str(info.get("id") or "")
                                    status_val = info.get("status")
                                    domain_status = str(status_val) if status_val is not None else ""
                                except Exception as e:
                                    log.warning(f"获取域名信息失败: {domain} - {e}")

                                now = datetime.utcnow().isoformat()
                                _site_db.update_site(domain, {
                                    "report_status": "已报",
                                    "report_time": now,
                                    "report_id": report_id,
                                    "domain_status": domain_status,
                                    "schedule_enabled": "0",
                                })

                                success += 1
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✓ {domain} 上报成功")
                                log.info(f"[上报] [{domain}] ✓ 上报成功, report_id={report_id}")

                            except Exception as e:
                                failed += 1
                                errors.append(f"{domain}: {e}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain} 上报失败 - {e}")
                                log.error(f"[上报] [{domain}] 失败: {e}")

                        summary = f"域名上报完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                        task_manager.update(task_id, status="completed", message=summary,
                                            progress=100, current=total, total=total)

                        if errors:
                            log.warning("[上报] 失败明细:")
                            for err in errors:
                                log.warning(f"  {err}")

                    except Exception as e:
                        log.error(f"[上报] 任务异常: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                    finally:
                        _site_db.close()

                task_manager.start_task_thread(task_id, run_report_task)
                flash(f"域名上报任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("core.tasks"))

            elif action == "schedule_selected":
                selected_ids = request.form.getlist("selected_ids")
                schedule_time = request.form.get("schedule_time", "")
                if selected_ids and schedule_time:
                    count = site_db.batch_set_schedule(selected_ids, schedule_time)
                    flash(f"已设置 {count} 个站点的计划时间", "success")
                return redirect(url_for("site_management.site_local"))

            elif action == "clear_schedule_selected":
                selected_ids = request.form.getlist("selected_ids")
                if selected_ids:
                    count = site_db.batch_clear_schedule(selected_ids)
                    flash(f"已清除 {count} 个站点的计划", "success")
                return redirect(url_for("site_management.site_local"))

        result = site_db.list_local_sites(q, page=page, page_size=page_size, category=category)
        stats = site_db.get_stats()
        categories = site_db.list_local_categories()
        return render_template("site_local.html", sites=result["items"], stats=stats, q=q,
                               category=category, categories=categories,
                               total=result["total"], page=result["page"], page_size=result["page_size"])
    except Exception as e:
        log.error(f"本地站点页面错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_local.html", sites=[], stats={"local_sites": 0}, q=q,
                               category=category, categories=[],
                               total=0, page=1, page_size=20)


@bp.route("/site-management/generate-logos", methods=["GET", "POST"])
def site_generate_logos():
    """批量生成Logo"""
    from qmds.utils.logo_generator import LogoGenerator, get_available_fonts

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "start":
            selected_ids = request.form.getlist("selected_ids")
            logo_dir = request.form.get("logo_dir", "").strip()
            font_name = request.form.get("font", "").strip() or None

            if not selected_ids:
                flash("请选择要生成Logo的站点", "error")
                return redirect(url_for("site_management.site_generate_logos"))

            if not logo_dir:
                logo_dir = os.path.join(settings.data_dir, "logos", "setting")

            task_id = f"logo_gen_{int(time.time())}"
            task_manager.create(task_id, "generate_logos", f"{len(selected_ids)} sites")

            def run_task():
                _site_db = SiteDBClient()
                try:
                    task_manager.add_log(task_id, f"任务启动: 批量生成Logo", "info")
                    task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                    domains = []
                    for site_id in selected_ids:
                        site = _site_db.get_site_by_id(site_id)
                        if site and site.get("domain"):
                            domains.append(site["domain"])

                    if not domains:
                        task_manager.update(task_id, status="failed", message="未找到有效域名")
                        task_manager.add_log(task_id, "未找到有效域名", "error")
                        return

                    task_manager.add_log(task_id, f"有效域名: {len(domains)}", "info")
                    task_manager.add_log(task_id, f"输出目录: {logo_dir}", "info")

                    task_manager.update(task_id, status="running",
                                        message=f"开始生成 {len(domains)} 个Logo",
                                        total=len(domains))

                    def progress_callback(current, total, domain, result):
                        if task_manager.is_stopped(task_id):
                            generator.stop()
                            return
                        status_icon = "✓" if result["success"] else "✗"
                        task_manager.update(
                            task_id,
                            progress=int(current / total * 100),
                            current=current,
                            message=f"[{current}/{total}] {status_icon} {domain}"
                        )
                        task_manager.add_log(task_id, f"[{current}/{total}] {status_icon} {domain}", "info" if result["success"] else "warning")

                    generator = LogoGenerator()
                    result = generator.generate_batch(domains, logo_dir, progress_callback)

                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return

                    for domain in domains:
                        logo_path = os.path.join(logo_dir, domain, "logo.png")
                        if os.path.exists(logo_path):
                            _site_db.update_site(domain, {"logo": logo_path})

                    task_manager.update(
                        task_id,
                        status="completed",
                        message=f"完成: 成功 {result['success']}, 失败 {result['failed']}",
                        progress=100,
                        current=result["total"],
                        total=result["total"]
                    )
                    task_manager.add_log(task_id, f"任务完成: 成功 {result['success']}, 失败 {result['failed']}", "info")

                    if result["errors"]:
                        for error in result["errors"][:5]:
                            task_manager.add_log(task_id, f"错误: {error}", "error")

                except Exception as e:
                    log.error(f"Logo生成任务失败: {e}")
                    task_manager.update(task_id, status="failed", message=str(e))
                    task_manager.add_log(task_id, f"任务失败: {e}", "error")
                finally:
                    _site_db.close()

            task_manager.start_task_thread(task_id, run_task)
            flash(f"Logo生成任务已启动: {len(selected_ids)} 个站点", "success")
            return redirect(url_for("core.tasks"))

    site_db = get_site_db()
    try:
        q = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        result = site_db.list_local_sites(q, page=page, page_size=page_size)
        fonts = get_available_fonts()
        default_logo_dir = os.path.join(settings.data_dir, "logos", "setting")

        return render_template("site_generate_logos.html",
                               sites=result["items"],
                               total=result["total"],
                               page=result["page"],
                               page_size=result["page_size"],
                               q=q,
                               fonts=fonts,
                               default_logo_dir=default_logo_dir)
    except Exception as e:
        log.error(f"Logo生成页面错误: {e}")
        flash(f"加载失败: {e}", "error")
        return render_template("site_generate_logos.html", sites=[], fonts=[], default_logo_dir="")


@bp.route("/site-management/generate-images", methods=["GET", "POST"])
def site_generate_images():
    """批量生成图片（Banner + Icon + Logo + 合并）"""
    from qmds.utils.image_generator import (
        ImageGenerator, load_jisuai_keys, list_image_models, get_image_model_config,
        DEFAULT_IMAGE_MODEL,
    )

    site_db = get_site_db()
    try:
        jisuai_api_keys = load_jisuai_keys()
    except Exception:
        jisuai_api_keys = []

    has_api_key = bool(jisuai_api_keys)
    # 火山方舟 API Key（存储于 settings 集合）
    ark_api_key = site_db.get_setting("ark_api_key", "") or ""
    has_ark_key = bool(ark_api_key)
    image_models = list_image_models()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "start":
            selected_ids = request.form.getlist("selected_ids")
            output_dir = request.form.get("output_dir", "").strip()
            keyword_source = request.form.get("keyword_source", "main_category")
            manual_keywords = request.form.get("manual_keywords", "").strip()
            gen_banner = request.form.get("gen_banner") == "1"
            gen_icon = request.form.get("gen_icon") == "1"
            gen_logo = request.form.get("gen_logo") == "1"
            gen_combine = request.form.get("gen_combine") == "1"
            selected_model = request.form.get("image_model", DEFAULT_IMAGE_MODEL)
            model_cfg = get_image_model_config(selected_model) or get_image_model_config(DEFAULT_IMAGE_MODEL)
            provider = model_cfg["provider"]

            # 校验密钥
            if provider == "jisuai" and not has_api_key:
                flash("请先在配置页面设置极速AI API密钥", "error")
                return redirect(url_for("site_management.site_generate_images"))
            if provider == "ark" and not has_ark_key:
                flash("使用火山方舟模型时，请先在配置页面设置火山方舟 ARK API Key", "error")
                return redirect(url_for("site_management.site_generate_images"))

            if not selected_ids:
                flash("请选择要生成图片的站点", "error")
                return redirect(url_for("site_management.site_generate_images"))

            if not (gen_banner or gen_icon or gen_logo or gen_combine):
                flash("请至少选择一项生成内容", "error")
                return redirect(url_for("site_management.site_generate_images"))

            if not output_dir:
                output_dir = str(settings.data_dir / "logos" / "setting")

            manual_kw_list = [k.strip() for k in manual_keywords.split("\n") if k.strip()] if manual_keywords else []

            task_id = f"img_gen_{int(time.time())}"
            task_manager.create(task_id, "generate_images", f"{len(selected_ids)} sites")

            def run_task():
                _site_db = SiteDBClient()
                try:
                    _jisuai_api_keys = load_jisuai_keys()
                    _ark_api_key = _site_db.get_setting("ark_api_key", "") or ""
                    task_manager.add_log(task_id, f"任务启动: 批量生成图片", "info")
                    task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")
                    task_manager.add_log(task_id, f"生成内容: Banner={gen_banner}, Icon={gen_icon}, Logo={gen_logo}, 合并={gen_combine}", "info")
                    task_manager.add_log(task_id, f"模型: {model_cfg['model_id']} (provider={provider})", "info")
                    if provider == "jisuai":
                        task_manager.add_log(task_id, f"极速AI密钥池大小: {len(_jisuai_api_keys)} 个", "info")
                    else:
                        task_manager.add_log(task_id, f"火山方舟 API Key: 已配置", "info")

                    from qmds.utils.proxy_manager import ProxyManager as _PM
                    _proxy_mgr = _PM.from_settings() if settings.load_proxies() else None
                    task_manager.add_log(task_id, f"代理池大小: {_proxy_mgr.total_count if _proxy_mgr else 0} 个", "info")

                    items = []
                    for idx, site_id in enumerate(selected_ids):
                        site = _site_db.get_site_by_id(site_id)
                        if not site or not site.get("domain"):
                            continue

                        domain = site["domain"]

                        if keyword_source == "manual" and idx < len(manual_kw_list):
                            keyword = manual_kw_list[idx]
                        else:
                            keyword = site.get("main_category", "")
                            if keyword and "|||" in keyword:
                                parts = [p.strip() for p in keyword.split("|||") if p.strip()]
                                keyword = " > ".join(parts) if len(parts) > 1 else parts[-1]
                            if not keyword:
                                keyword = domain.replace(".com", "").replace(".net", "").replace(".org", "")

                        items.append((domain, keyword))

                    if not items:
                        task_manager.update(task_id, status="failed", message="未找到有效域名")
                        task_manager.add_log(task_id, "未找到有效域名", "error")
                        return

                    task_manager.add_log(task_id, f"有效域名: {len(items)}", "info")
                    task_manager.add_log(task_id, f"输出目录: {output_dir}", "info")

                    for i, (d, k) in enumerate(items[:10], 1):
                        task_manager.add_log(task_id, f"  {i}. {d} -> {k}", "info")
                    if len(items) > 10:
                        task_manager.add_log(task_id, f"  ... 还有 {len(items) - 10} 个站点", "info")

                    task_manager.update(task_id, status="running",
                                        message=f"开始生成 {len(items)} 个站点的图片",
                                        total=len(items))

                    def progress_callback(step, current, total, domain, result):
                        if task_manager.is_stopped(task_id):
                            generator.stop()
                            return

                        step_labels = {
                            "banner": "Banner",
                            "icon": "Icon",
                            "logo": "Logo",
                            "rename": "重命名",
                            "combine": "合并",
                        }
                        step_label = step_labels.get(step, step)
                        status_icon = "✓" if result.get("success") else "✗"
                        skipped = " [跳过]" if result.get("skipped") else ""

                        task_manager.update(
                            task_id,
                            progress=int(current / total * 100),
                            current=current,
                            message=f"[{step_label}] [{current}/{total}] {status_icon} {domain}{skipped}"
                        )
                        task_manager.add_log(
                            task_id,
                            f"[{step_label}] [{current}/{total}] {status_icon} {domain}{skipped}",
                            "info" if result.get("success") else "warning"
                        )

                    generator = ImageGenerator(
                        api_keys=_jisuai_api_keys,
                        proxy_manager=_proxy_mgr,
                        model=selected_model,
                        ark_api_key=_ark_api_key,
                    )

                    try:
                        result = asyncio.run(
                            generator.generate_batch(
                                items=items,
                                base_dir=output_dir,
                                generate_banner=gen_banner,
                                generate_icon=gen_icon,
                                generate_logo=gen_logo,
                                do_combine=gen_combine,
                                progress_callback=progress_callback
                            )
                        )
                    except RuntimeError as e:
                        if "cannot be called from a running event loop" in str(e):
                            import nest_asyncio
                            nest_asyncio.apply()
                            result = asyncio.run(
                                generator.generate_batch(
                                    items=items,
                                    base_dir=output_dir,
                                    generate_banner=gen_banner,
                                    generate_icon=gen_icon,
                                    generate_logo=gen_logo,
                                    do_combine=gen_combine,
                                    progress_callback=progress_callback
                                )
                            )
                        else:
                            raise

                    if task_manager.is_stopped(task_id):
                        task_manager.update(task_id, status="stopped", message="任务已停止")
                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                        return

                    for domain, _ in items:
                        site_dir = os.path.join(output_dir, domain)
                        has_banner = os.path.isfile(os.path.join(site_dir, "banner.jpg"))
                        has_icon = os.path.isfile(os.path.join(site_dir, "icon.png"))
                        has_logo = os.path.isfile(os.path.join(site_dir, "logo.png"))

                        update_data = {}
                        if has_banner:
                            update_data["banner"] = os.path.join(site_dir, "banner.jpg")
                        if has_icon:
                            update_data["icon"] = os.path.join(site_dir, "icon.png")
                        if has_logo:
                            update_data["logo"] = os.path.join(site_dir, "logo.png")

                        if update_data:
                            _site_db.update_site(domain, update_data)
                        _site_db.update_image_status(domain, has_banner, has_icon, has_logo)

                    summary_parts = []
                    if gen_banner:
                        summary_parts.append(f"Banner: {result['banner']['success']}/{len(items)}")
                    if gen_icon:
                        summary_parts.append(f"Icon: {result['icon']['success']}/{len(items)}")
                    if gen_logo:
                        summary_parts.append(f"Logo: {result['logo']['success']}/{len(items)}")
                    if gen_combine:
                        summary_parts.append(f"合并: {result['combine']['success']}/{len(items)}")

                    summary = f"完成: {', '.join(summary_parts)}"
                    task_manager.update(task_id, status="completed", message=summary, progress=100)
                    task_manager.add_log(task_id, summary, "info")

                    if result["errors"]:
                        for error in result["errors"][:10]:
                            task_manager.add_log(task_id, f"错误: {error}", "error")

                except Exception as e:
                    log.error(f"图片生成任务失败: {e}")
                    task_manager.update(task_id, status="failed", message=str(e))
                    task_manager.add_log(task_id, f"任务失败: {e}", "error")
                finally:
                    _site_db.close()

            task_manager.start_task_thread(task_id, run_task)
            flash(f"图片生成任务已启动: {len(selected_ids)} 个站点", "success")
            return redirect(url_for("core.tasks"))

    try:
        q = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        result = site_db.list_active_sites(q, page=page, page_size=page_size)
        default_output_dir = str(settings.data_dir / "logos" / "setting")

        for site in result["items"]:
            domain = site.get("domain", "")
            if domain:
                site["has_banner"] = site.get("has_banner", False)
                site["has_icon"] = site.get("has_icon", False)
                site["has_logo"] = site.get("has_logo", False)

        return render_template("site_generate_images.html",
                               sites=result["items"],
                               total=result["total"],
                               page=result["page"],
                               page_size=result["page_size"],
                               q=q,
                               has_api_key=has_api_key,
                               has_ark_key=has_ark_key,
                               image_models=image_models,
                               default_image_model=DEFAULT_IMAGE_MODEL,
                               default_output_dir=default_output_dir)
    except Exception as e:
        log.error(f"图片生成页面错误: {e}")
        flash(f"加载失败: {e}", "error")
        return render_template("site_generate_images.html",
                               sites=[],
                               has_api_key=has_api_key,
                               has_ark_key=has_ark_key,
                               image_models=image_models,
                               default_image_model=DEFAULT_IMAGE_MODEL,
                               default_output_dir="")


@bp.route("/site-management/refresh-image-status", methods=["POST"])
def refresh_image_status():
    """从文件系统扫描并更新所有站点的图片状态"""
    site_db = get_site_db()
    try:
        logos_dir = str(settings.data_dir / "logos")
        setting_dir = os.path.join(logos_dir, "setting")
        result = site_db.list_active_sites(page=1, page_size=99999)
        updated = 0

        for site in result["items"]:
            domain = site.get("domain", "")
            if not domain:
                continue
            primary_dir = os.path.join(logos_dir, domain)
            fallback_dir = os.path.join(setting_dir, domain)
            has_banner = os.path.isfile(os.path.join(primary_dir, "banner.jpg")) or os.path.isfile(os.path.join(fallback_dir, "banner.jpg"))
            has_icon = os.path.isfile(os.path.join(primary_dir, "icon.png")) or os.path.isfile(os.path.join(fallback_dir, "icon.png"))
            has_logo = os.path.isfile(os.path.join(primary_dir, "logo.png")) or os.path.isfile(os.path.join(fallback_dir, "logo.png"))
            site_db.update_image_status(domain, has_banner, has_icon, has_logo)
            updated += 1

        flash(f"已刷新 {updated} 个站点的图片状态", "success")
    except Exception as e:
        log.error(f"刷新图片状态失败: {e}")
        flash(f"刷新失败: {e}", "error")
    return redirect(url_for("site_management.site_generate_images"))


@bp.route("/site-management/image-preview/<domain>/<image_type>")
def site_image_preview(domain: str, image_type: str):
    """预览站点生成的图片（banner/icon/logo）"""
    if image_type not in ("banner", "icon", "logo"):
        return ("Invalid type", 400)

    logos_dir = str(settings.data_dir / "logos")
    setting_dir = os.path.join(logos_dir, "setting")
    filename = "banner.jpg" if image_type == "banner" else f"{image_type}.png"

    for base_dir in (os.path.join(logos_dir, domain), os.path.join(setting_dir, domain)):
        filepath = os.path.join(base_dir, filename)
        if os.path.isfile(filepath):
            mimetype = "image/jpeg" if image_type == "banner" else "image/png"
            return send_file(filepath, mimetype=mimetype)

    return ("Not found", 404)


@bp.route("/site-management/reported", methods=["GET", "POST"])
def site_reported():
    """已报域名管理"""
    site_db = get_site_db()
    try:
        q = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))

        if request.method == "POST":
            action = request.form.get("action", "")

            if action == "build_selected":
                selected_ids = request.form.getlist("selected_ids")
                if not selected_ids:
                    flash("请先勾选要建站的站点", "error")
                    return redirect(url_for("site_management.site_reported"))

                task_id = f"build_sites_{int(time.time())}"
                task_manager.create(task_id, "build_sites", f"ERP建站 ({len(selected_ids)} sites)")

                def run_build_task():
                    _site_db = SiteDBClient()
                    try:
                        from qmds.utils.erp_builder import get_erp_builder
                        from qmds.config import settings as qmds_settings

                        task_manager.add_log(task_id, f"任务启动: ERP建站", "info")
                        task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                        sites = []
                        for sid in selected_ids:
                            site = _site_db.get_site_by_id(sid)
                            if site and site.get("domain"):
                                sites.append(site)

                        if not sites:
                            task_manager.update(task_id, status="failed", message="未找到有效站点")
                            task_manager.add_log(task_id, "未找到有效站点", "error")
                            return

                        total = len(sites)
                        task_manager.add_log(task_id, f"有效站点: {total}", "info")

                        task_manager.update(task_id, status="running",
                                            message=f"开始ERP建站: {total} 个站点",
                                            total=total, current=0)

                        task_manager.update(task_id, current=0,
                                            message="登录ERP系统...")
                        task_manager.add_log(task_id, "正在登录ERP系统...", "info")

                        image_root = str(qmds_settings.data_dir / "logos")
                        erp_username = _site_db.get_setting("erp_username")
                        erp_password = _site_db.get_setting("erp_password")
                        if not erp_username or not erp_password:
                            task_manager.add_log(task_id, "未配置ERP账号密码", "error")
                            raise Exception("未配置ERP账号密码，请在设置中配置 erp_username/erp_password")

                        erp = get_erp_builder(username=erp_username, password=erp_password,
                                              image_root=image_root)
                        login_result = erp.login()
                        if not login_result["success"]:
                            task_manager.update(task_id, status="failed",
                                                message=f"ERP登录失败: {login_result['message']}")
                            task_manager.add_log(task_id, f"ERP登录失败: {login_result['message']}", "error")
                            return

                        task_manager.add_log(task_id, "ERP登录成功，开始建站...", "info")
                        log.info("ERP登录成功，开始建站...")

                        success = 0
                        failed = 0
                        errors = []

                        for i, site in enumerate(sites):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                return

                            current = i + 1
                            domain = site.get("domain", "")
                            server = site.get("server", "")
                            template = site.get("template", "")
                            title = site.get("title", "")
                            desc = site.get("description", "")
                            address = site.get("address", "")
                            category = site.get("category", "")

                            try:
                                missing = []
                                if not server:
                                    missing.append("服务器")
                                if not template:
                                    missing.append("模板")
                                if not title:
                                    missing.append("标题")
                                if not desc:
                                    missing.append("描述")
                                if not address:
                                    missing.append("地址")
                                if not category:
                                    missing.append("大类")

                                if missing:
                                    raise Exception(f"缺少字段: {', '.join(missing)}")

                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.5) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 开始建站...")

                                def build_progress(msg):
                                    task_manager.update(task_id, current=current,
                                                        progress=int((current - 0.3) / total * 100),
                                                        message=f"[{current}/{total}] [{domain}] {msg}")

                                result = erp.build_site(
                                    domain=domain,
                                    server=server,
                                    template=template,
                                    title=title,
                                    description=desc,
                                    address=address,
                                    category=category,
                                    progress_callback=build_progress
                                )

                                if result["success"]:
                                    _site_db.update_site(domain, {
                                        "build_status": "已建站",
                                        "build_time": datetime.utcnow().isoformat()
                                    })
                                    success += 1
                                    task_manager.update(task_id, current=current,
                                                        progress=int(current / total * 100),
                                                        message=f"[{current}/{total}] ✓ [{domain}] 建站成功")
                                    log.info(f"[建站] [{domain}] ✓ 建站成功")
                                else:
                                    raise Exception(result["message"])

                            except Exception as e:
                                failed += 1
                                errors.append(f"{domain}: {e}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ [{domain}] 建站失败 - {e}")
                                task_manager.add_log(task_id, f"[{current}/{total}] ✗ [{domain}] 建站失败 - {e}", "error")
                                log.error(f"[建站] [{domain}] 失败: {e}")

                        summary = f"ERP建站完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                        task_manager.update(task_id, status="completed", message=summary,
                                            progress=100, current=total, total=total)
                        task_manager.add_log(task_id, summary, "info")

                        if errors:
                            log.warning("[建站] 失败明细:")
                            for err in errors:
                                log.warning(f"  {err}")
                                task_manager.add_log(task_id, f"失败: {err}", "error")

                    except Exception as e:
                        log.error(f"[建站] 任务异常: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                        task_manager.add_log(task_id, f"任务异常: {e}", "error")
                    finally:
                        _site_db.close()

                task_manager.start_task_thread(task_id, run_build_task)
                flash(f"ERP建站任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("core.tasks"))

            elif action == "delete_selected":
                selected_ids = request.form.getlist("selected_ids")
                if selected_ids:
                    count = site_db.batch_update_report_status(selected_ids, "未报")
                    flash(f"已取消上报 {count} 个站点", "success")
                return redirect(url_for("site_management.site_reported"))

            elif action == "update_status":
                selected_ids = request.form.getlist("selected_ids")
                if not selected_ids:
                    flash("请先勾选要更新状态的站点", "error")
                    return redirect(url_for("site_management.site_reported"))

                task_id = f"update_status_{int(time.time())}"
                task_manager.create(task_id, "update_domain_status", f"{len(selected_ids)} 个站点")

                def run_task():
                    site_db_inner = SiteDBClient()
                    try:
                        task_manager.add_log(task_id, f"任务启动: 更新域名状态", "info")
                        task_manager.add_log(task_id, f"待更新站点: {len(selected_ids)}", "info")

                        _settings = site_db_inner.get_all_settings()
                        username = _settings.get("report_username", "")
                        password = _settings.get("report_password", "")
                        if not username or not password:
                            task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                            task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                            return

                        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                        success_count = 0
                        fail_count = 0

                        for site_id in selected_ids:
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped",
                                    message=f"任务已停止: 成功 {success_count} 个, 失败 {fail_count} 个")
                                task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return

                            site = site_db_inner.get_site_by_id(site_id)
                            if not site:
                                fail_count += 1
                                task_manager.add_log(task_id, f"ID {site_id}: 站点不存在", "warning")
                                continue

                            domain = site.get("domain", "")
                            if not domain:
                                fail_count += 1
                                continue

                            try:
                                info = reporter.fetch_domain_info(domain)
                                report_id = str(info.get("id") or "")
                                status_val = info.get("status")
                                status_label = DOMAIN_STATUS_LABELS.get(status_val, "未知")
                                site_db_inner.update_domain_status(domain, report_id, str(status_val) if status_val is not None else "")
                                success_count += 1
                                task_manager.add_log(task_id, f"✓ {domain} → {status_label}", "info")
                                log.info(f"更新域名状态成功: {domain} -> {status_label}")
                            except Exception as e:
                                fail_count += 1
                                task_manager.add_log(task_id, f"✗ {domain} - {e}", "error")
                                log.error(f"更新域名状态失败: {domain} - {e}")

                        summary = f"完成: 成功 {success_count} 个, 失败 {fail_count} 个"
                        task_manager.update(
                            task_id,
                            status="completed",
                            message=summary,
                            progress=100
                        )
                        task_manager.add_log(task_id, summary, "info")
                    except Exception as e:
                        log.error(f"更新域名状态任务失败: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                        task_manager.add_log(task_id, f"任务失败: {e}", "error")
                    finally:
                        site_db_inner.close()

                task_manager.start_task_thread(task_id, run_task)
                flash(f"更新域名状态任务已启动，可在任务页面查看进度", "info")
                return redirect(url_for("site_management.site_reported"))

            elif action == "review_reported":
                task_id = f"review_reported_{int(time.time())}"
                task_manager.create(task_id, "review_reported", "审查已报域名")

                def run_review_task():
                    site_db_inner = SiteDBClient()
                    try:
                        task_manager.add_log(task_id, f"任务启动: 审查已报域名", "info")

                        _settings = site_db_inner.get_all_settings()
                        username = _settings.get("report_username", "")
                        password = _settings.get("report_password", "")
                        if not username or not password:
                            task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                            task_manager.add_log(task_id, "请先在配置页面设置上报账号和密码", "error")
                            return

                        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                        all_reported = site_db_inner.list_reported_domains_for_sync()
                        total = len(all_reported)
                        found_count = 0
                        not_found_count = 0
                        error_count = 0

                        task_manager.add_log(task_id, f"已报域名总数: {total}", "info")

                        for i, site in enumerate(all_reported):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped",
                                    message=f"任务已停止: 审查 {i}/{total}, 平台存在 {found_count}, 不存在 {not_found_count}")
                                task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return

                            domain = site.get("domain", "")
                            if not domain:
                                continue

                            try:
                                info = reporter.fetch_domain_info(domain)
                                if info and info.get("id"):
                                    report_id = str(info.get("id") or "")
                                    status_val = info.get("status")
                                    site_db_inner.update_domain_status(domain, report_id, str(status_val) if status_val is not None else "")
                                    found_count += 1
                                    task_manager.add_log(task_id, f"[{i+1}/{total}] ✓ {domain} - 平台存在", "info")
                                else:
                                    site_db_inner.update_site(domain, {"report_status": "未报"})
                                    not_found_count += 1
                                    task_manager.add_log(task_id, f"[{i+1}/{total}] △ {domain} - 平台不存在，已标记未报", "warning")
                            except Exception:
                                site_db_inner.update_site(domain, {"report_status": "未报"})
                                error_count += 1
                                task_manager.add_log(task_id, f"[{i+1}/{total}] ✗ {domain} - 查询失败，已标记未报", "error")

                            if (i + 1) % 10 == 0 or i + 1 == total:
                                task_manager.update(task_id,
                                    progress=int((i + 1) / total * 100),
                                    message=f"审查中: {i + 1}/{total}")

                        summary = f"审查完成: 平台存在 {found_count}, 不存在 {not_found_count}, 失败 {error_count}"
                        task_manager.update(
                            task_id,
                            status="completed",
                            message=summary,
                            progress=100
                        )
                        task_manager.add_log(task_id, summary, "info")
                    except Exception as e:
                        log.error(f"审查已报域名任务失败: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务失败: {e}")
                        task_manager.add_log(task_id, f"任务失败: {e}", "error")
                    finally:
                        site_db_inner.close()

                task_manager.start_task_thread(task_id, run_review_task)
                flash(f"审查已报域名任务已启动，可在任务页面查看进度", "info")
                return redirect(url_for("site_management.site_reported"))

            elif action == "generate_logos":
                selected_ids = request.form.getlist("selected_ids")
                if not selected_ids:
                    flash("请先勾选要生成Logo的站点", "error")
                    return redirect(url_for("site_management.site_reported"))

                task_id = f"logo_gen_reported_{int(time.time())}"
                task_manager.create(task_id, "generate_logos", f"批量生成Logo ({len(selected_ids)} sites)")

                def run_logo_task():
                    from qmds.utils.logo_generator import LogoGenerator
                    _site_db = SiteDBClient()
                    try:
                        logo_dir = str(settings.data_dir / "logos" / "setting")
                        task_manager.add_log(task_id, f"任务启动: 批量生成Logo (已报域名)", "info")
                        task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")

                        domains = []
                        for site_id in selected_ids:
                            site = _site_db.get_site_by_id(site_id)
                            if site and site.get("domain"):
                                domains.append(site["domain"])

                        if not domains:
                            task_manager.update(task_id, status="failed", message="未找到有效域名")
                            task_manager.add_log(task_id, "未找到有效域名", "error")
                            return

                        task_manager.add_log(task_id, f"有效域名: {len(domains)}", "info")
                        task_manager.add_log(task_id, f"输出目录: {logo_dir}", "info")

                        task_manager.update(task_id, status="running",
                                            message=f"开始生成 {len(domains)} 个Logo",
                                            total=len(domains))

                        def progress_callback(current, total, domain, result):
                            if task_manager.is_stopped(task_id):
                                generator.stop()
                                return
                            status_icon = "✓" if result["success"] else "✗"
                            task_manager.update(
                                task_id,
                                progress=int(current / total * 100),
                                current=current,
                                message=f"[{current}/{total}] {status_icon} {domain}"
                            )
                            task_manager.add_log(task_id, f"[{current}/{total}] {status_icon} {domain}", "info" if result["success"] else "warning")

                        generator = LogoGenerator()
                        result = generator.generate_batch(domains, logo_dir, progress_callback)

                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped", message="任务已停止")
                            task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return

                        for domain in domains:
                            logo_path = os.path.join(logo_dir, domain, "logo.png")
                            if os.path.exists(logo_path):
                                _site_db.update_site(domain, {"logo": logo_path})

                        task_manager.update(
                            task_id,
                            status="completed",
                            message=f"完成: 成功 {result['success']}, 失败 {result['failed']}",
                            progress=100,
                            current=result["total"],
                            total=result["total"]
                        )
                        task_manager.add_log(task_id, f"任务完成: 成功 {result['success']}, 失败 {result['failed']}", "info")

                        if result["errors"]:
                            for error in result["errors"][:5]:
                                task_manager.add_log(task_id, f"错误: {error}", "error")

                    except Exception as e:
                        log.error(f"Logo生成任务失败: {e}")
                        task_manager.update(task_id, status="failed", message=str(e))
                        task_manager.add_log(task_id, f"任务失败: {e}", "error")
                    finally:
                        _site_db.close()

                task_manager.start_task_thread(task_id, run_logo_task)
                flash(f"Logo生成任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("core.tasks"))

            elif action == "batch_update":
                selected_ids = request.form.getlist("selected_ids")
                field = request.form.get("batch_field", "").strip()
                value = request.form.get("batch_value", "").strip()

                if not selected_ids:
                    flash("请先勾选要更新的站点", "error")
                    return redirect(url_for("site_management.site_reported"))

                if not field:
                    flash("请选择要更新的字段", "error")
                    return redirect(url_for("site_management.site_reported"))

                allowed_fields = {"template", "server", "category", "main_category",
                                  "main_data_source_id", "extra_data_source_id",
                                  "title", "description", "address", "build_status"}
                if field not in allowed_fields:
                    flash("不允许修改该字段", "error")
                    return redirect(url_for("site_management.site_reported"))

                count = site_db.batch_update_fields(selected_ids, field, value)
                flash(f"已更新 {count} 个站点的 {field} 字段", "success")
                return redirect(url_for("site_management.site_reported"))

        result = site_db.list_reported_sites(q, page=page, page_size=page_size)
        stats = site_db.get_stats()
        return render_template("site_reported.html", sites=result["items"], stats=stats, q=q,
                               total=result["total"], page=result["page"], page_size=result["page_size"])
    except Exception as e:
        log.error(f"已报域名页面错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_reported.html", sites=[], stats={"reported_sites": 0}, q=q,
                               total=0, page=1, page_size=20)


@bp.route("/site-management/scheduled", methods=["GET", "POST"])
def site_scheduled():
    """计划上报管理"""
    site_db = get_site_db()
    try:
        q = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))

        if request.method == "POST":
            action = request.form.get("action", "")

            if action == "report_selected":
                selected_ids = request.form.getlist("selected_ids")
                if not selected_ids:
                    flash("请先勾选要上报的站点", "error")
                    return redirect(url_for("site_management.site_scheduled"))

                task_id = f"report_scheduled_{int(time.time())}"
                task_manager.create(task_id, "report_domains", f"计划上报 ({len(selected_ids)} sites)")

                def run_report_task():
                    _site_db = SiteDBClient()
                    try:
                        _settings = _site_db.get_all_settings()
                        username = _settings.get("report_username", "")
                        password = _settings.get("report_password", "")
                        if not username or not password:
                            task_manager.update(task_id, status="failed", message="请先在配置页面设置上报账号和密码")
                            return

                        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
                        total = len(selected_ids)
                        success = 0
                        failed = 0
                        errors = []

                        for i, site_id in enumerate(selected_ids):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                return

                            site = _site_db.get_site_by_id(site_id)
                            if not site:
                                failed += 1
                                errors.append(f"ID {site_id}: 站点不存在")
                                continue

                            domain = (site.get("domain") or "").strip()
                            server = (site.get("server") or "").strip()
                            template = (site.get("template") or "").strip()
                            category_name = (site.get("category") or "").strip()

                            current = i + 1

                            missing = []
                            if not domain:
                                missing.append("域名")
                            if not server:
                                missing.append("服务器")
                            if not template:
                                missing.append("模板")
                            if not category_name:
                                missing.append("大类")

                            if missing:
                                failed += 1
                                errors.append(f"{domain or site_id}: 缺少字段 {', '.join(missing)}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain or site_id} - 缺少字段")
                                continue

                            category_id = REPORT_CATEGORY_ID_MAP.get(category_name)
                            if not category_id:
                                failed += 1
                                errors.append(f"{domain}: 无效分类 {category_name}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain} - 无效分类")
                                continue

                            payload = {
                                "name": domain,
                                "serverip": server,
                                "template": template,
                                "category": category_id,
                                "categoryTag": None,
                                "language": None,
                            }

                            try:
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.5) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 正在上报...")
                                reporter.submit_domain(payload)

                                report_id = ""
                                domain_status = ""
                                try:
                                    info = reporter.fetch_domain_info(domain)
                                    report_id = str(info.get("id") or "")
                                    status_val = info.get("status")
                                    domain_status = str(status_val) if status_val is not None else ""
                                except Exception as e:
                                    log.warning(f"获取域名信息失败: {domain} - {e}")

                                now = datetime.utcnow().isoformat()
                                _site_db.update_site(domain, {
                                    "report_status": "已报",
                                    "report_time": now,
                                    "report_id": report_id,
                                    "domain_status": domain_status,
                                    "schedule_enabled": "0",
                                })

                                success += 1
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✓ {domain} 上报成功")
                                log.info(f"[上报] [{domain}] ✓ 上报成功, report_id={report_id}")

                            except Exception as e:
                                failed += 1
                                errors.append(f"{domain}: {e}")
                                task_manager.update(task_id, current=current,
                                                    progress=int(current / total * 100),
                                                    message=f"[{current}/{total}] ✗ {domain} 上报失败 - {e}")
                                log.error(f"[上报] [{domain}] 失败: {e}")

                        summary = f"域名上报完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                        task_manager.update(task_id, status="completed", message=summary,
                                            progress=100, current=total, total=total)

                        if errors:
                            log.warning("[上报] 失败明细:")
                            for err in errors:
                                log.warning(f"  {err}")

                    except Exception as e:
                        log.error(f"[上报] 任务异常: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                    finally:
                        _site_db.close()

                task_manager.start_task_thread(task_id, run_report_task)
                flash(f"域名上报任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("core.tasks"))

            elif action == "reschedule":
                selected_ids = request.form.getlist("selected_ids")
                schedule_time = request.form.get("schedule_time", "")
                if selected_ids and schedule_time:
                    count = site_db.batch_set_schedule(selected_ids, schedule_time)
                    flash(f"已重新设置 {count} 个站点的计划时间", "success")
                return redirect(url_for("site_management.site_scheduled"))

            elif action == "clear_selected":
                selected_ids = request.form.getlist("selected_ids")
                if selected_ids:
                    count = site_db.batch_clear_schedule(selected_ids)
                    flash(f"已清除 {count} 个站点的计划", "success")
                return redirect(url_for("site_management.site_scheduled"))

        result = site_db.list_scheduled_sites(q, page=page, page_size=page_size)
        stats = site_db.get_stats()
        return render_template("site_scheduled.html", sites=result["items"], stats=stats, q=q,
                               total=result["total"], page=result["page"], page_size=result["page_size"])
    except Exception as e:
        log.error(f"计划上报页面错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_scheduled.html", sites=[], stats={"scheduled_sites": 0}, q=q,
                               total=0, page=1, page_size=20)


@bp.route("/site-management/built", methods=["GET", "POST"])
def site_built():
    """已建站管理"""
    site_db = get_site_db()
    try:
        q = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))

        if request.method == "POST":
            action = request.form.get("action", "")
            selected_ids = request.form.getlist("selected_ids")

            if not selected_ids:
                flash("请先勾选要操作的站点", "error")
                return redirect(url_for("site_management.site_built", q=q))

            if action == "delete_selected":
                count = site_db.delete_sites_by_ids(selected_ids)
                flash(f"已删除 {count} 个站点", "success")
                return redirect(url_for("site_management.site_built", q=q))

            elif action == "reset_one_click":
                count = site_db.batch_reset_one_click_progress(selected_ids)
                flash(f"已重置 {count} 个站点的一键建站进度", "success")
                return redirect(url_for("site_management.site_built", q=q))

            elif action == "one_click_build":
                task_id = f"one_click_{int(time.time())}"
                task_manager.create(task_id, "one_click_build", f"一键建站 ({len(selected_ids)} sites)")

                def run_one_click_task():
                    _site_db = SiteDBClient()
                    try:
                        from qmds.utils.site_operator import get_operator, SiteOperator
                        from qmds.utils.ai_menu_builder import AiMenuConfigurator
                        from qmds.config import settings as qmds_settings

                        # 5个步骤的固定顺序：每步保持与单独运行时相同的线程模式
                        # configure_sites: 单线程顺序; upload_main/upload_extra: 10线程并行;
                        # set_main_category: 单线程顺序; ai_configure_menu: 单线程顺序
                        STEPS = [
                            ("configure_sites", "配置站点"),
                            ("upload_main", "上传主数据"),
                            ("set_main_category", "设置主分类"),
                            ("upload_extra", "上传补充数据"),
                            ("ai_configure_menu", "AI构建菜单"),
                        ]

                        task_manager.add_log(task_id, "任务启动: 一键建站", "info")
                        task_manager.add_log(task_id, f"选中站点: {len(selected_ids)}", "info")
                        task_manager.add_log(task_id, f"执行步骤顺序: {' -> '.join(label for _, label in STEPS)}", "info")

                        # 读取所有站点
                        sites = []
                        for sid in selected_ids:
                            site = _site_db.get_site_by_id(sid)
                            if site and site.get("domain"):
                                sites.append(site)

                        if not sites:
                            task_manager.update(task_id, status="failed", message="未找到有效站点")
                            task_manager.add_log(task_id, "未找到有效站点", "error")
                            return

                        total_sites = len(sites)
                        total_steps = len(STEPS)
                        task_manager.add_log(task_id, f"有效站点: {total_sites}", "info")

                        task_manager.update(task_id, status="running",
                                            message=f"开始一键建站: {total_sites} 个站点",
                                            total=total_sites, current=0)

                        # 当前待处理的站点列表（domain -> site dict）
                        pending_sites = {s["domain"]: s for s in sites}
                        # 统计
                        total_success_sites = 0
                        total_failed_sites = 0
                        all_errors = []

                        # ── 按步骤分批处理 ──────────────────────────────
                        for step_idx, (step_key, step_label) in enumerate(STEPS):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                task_manager.add_log(task_id, "任务被用户停止", "warning")
                                return

                            if not pending_sites:
                                task_manager.add_log(task_id, f"没有待处理站点，跳过步骤 [{step_label}]", "info")
                                continue

                            step_base_progress = int(step_idx / total_steps * 100)
                            step_end_progress = int((step_idx + 1) / total_steps * 100)
                            all_domains = list(pending_sites.keys())
                            step_total = len(all_domains)

                            task_manager.add_log(task_id, f"========== 步骤 {step_idx + 1}/{total_steps}: {step_label} (共 {step_total} 个站点) ==========", "info")
                            task_manager.update(task_id, current=step_base_progress, progress=step_base_progress,
                                                message=f"步骤 {step_idx + 1}/{total_steps}: {step_label} (待处理 {step_total} 站点)")

                            # 该步骤成功的站点集合
                            step_success_domains = set()

                            # ── 断点记忆：跳过本步骤已成功的站点 ──
                            current_domains = []
                            for d in all_domains:
                                site_info = pending_sites.get(d, {})
                                step_status = site_info.get(f"one_click_{step_key}", "")
                                if step_status == "success":
                                    step_success_domains.add(d)
                                    task_manager.add_log(task_id, f"[{d}] [{step_label}] 已是成功状态，跳过（断点记忆）", "info")
                                else:
                                    current_domains.append(d)

                            run_total = len(current_domains)
                            if step_success_domains:
                                task_manager.add_log(task_id, f"步骤 [{step_label}]: 跳过 {len(step_success_domains)} 个已完成, 实际执行 {run_total} 个", "info")
                            if run_total == 0:
                                task_manager.add_log(task_id, f"步骤 [{step_label}]: 全部站点已完成，跳过整个步骤", "info")
                                # 直接进入下一步筛选
                                new_pending = {}
                                for d in all_domains:
                                    if d in step_success_domains:
                                        new_pending[d] = pending_sites[d]
                                    else:
                                        total_failed_sites += 1
                                pending_sites = new_pending
                                task_manager.update(task_id, progress=step_end_progress,
                                                    message=f"步骤 {step_idx + 1}/{total_steps}: {step_label} (全部跳过)")
                                continue

                            # ── 步骤1: 配置站点 (单线程顺序, 与单独运行 configure_sites 相同) ──
                            if step_key == "configure_sites":
                                for i, domain in enumerate(current_domains):
                                    if task_manager.is_stopped(task_id):
                                        task_manager.update(task_id, status="stopped", message="任务已停止")
                                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                                        return
                                    current = i + 1
                                    site_info = pending_sites.get(domain, {})
                                    intra_progress = int((step_base_progress + (current - 0.5) / run_total * (step_end_progress - step_base_progress)))
                                    try:
                                        _site_db.update_one_click_progress(domain, step_key, "running", "")
                                        from qmds.utils.site_operator import get_operator
                                        operator = get_operator()
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 登录站点...", "info")
                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 登录站点...")
                                        login_result = operator.login(domain)
                                        if not login_result["success"]:
                                            raise Exception(f"登录失败: {login_result['message']}")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 登录成功", "info")

                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 配置WP Rocket...")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 配置WP Rocket...", "info")
                                        rocket_result = operator.process_rocket(domain)
                                        log.info(f"[配置站点] [{domain}] WP Rocket: {rocket_result['message']}")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] WP Rocket: {rocket_result['message']}", "info" if rocket_result["success"] else "warning")

                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 配置Yoast SEO...")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 配置Yoast SEO...", "info")
                                        yoast_result = operator.process_yoast(domain)
                                        log.info(f"[配置站点] [{domain}] Yoast: {yoast_result['message']}")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] Yoast: {yoast_result['message']}", "info" if yoast_result["success"] else "warning")
                                        _site_db.update_site(domain, {"plugin_status": "已配置",
                                                                      "plugin_time": datetime.utcnow().isoformat()})

                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 配置媒体...")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 配置媒体...", "info")
                                        media_root = str(qmds_settings.data_dir / "logos")
                                        media_result = operator.configure_media(domain, media_root)
                                        log.info(f"[配置站点] [{domain}] 媒体: {media_result['message']}")
                                        task_manager.add_log(task_id, f"[{domain}] [配置站点] 媒体: {media_result['message']}", "info" if media_result["success"] else "warning")
                                        _site_db.update_site(domain, {"media_status": "已配置",
                                                                      "media_time": datetime.utcnow().isoformat()})
                                        if not media_result["success"]:
                                            raise Exception(f"媒体配置失败: {media_result['message']}")

                                        _site_db.update_one_click_progress(domain, step_key, "success", "插件+媒体配置完成")
                                        task_manager.add_log(task_id, f"[{domain}] ✓ [配置站点] 成功", "info")
                                        step_success_domains.add(domain)
                                    except Exception as e:
                                        err_msg = str(e)
                                        _site_db.update_one_click_progress(domain, step_key, "failed", err_msg)
                                        task_manager.add_log(task_id, f"[{domain}] ✗ [配置站点] 失败 - {err_msg}", "error")
                                        log.error(f"[一键建站] [{domain}] [配置站点] 失败: {err_msg}")
                                        all_errors.append(f"{domain} [配置站点]: {err_msg}")
                                    task_manager.update(task_id, current=current,
                                                        progress=int(step_base_progress + current / run_total * (step_end_progress - step_base_progress)),
                                                        message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 完成")

                            # ── 步骤2/4: 上传主数据/上传补充数据 (10线程并行, 与单独运行 upload_main/upload_extra 相同) ──
                            elif step_key in ("upload_main", "upload_extra"):
                                completed = 0
                                def _upload_worker(idx_domain):
                                    idx, domain = idx_domain
                                    site_info = pending_sites.get(domain, {})
                                    from qmds.utils.site_operator import get_operator
                                    operator = get_operator()
                                    try:
                                        _site_db.update_one_click_progress(domain, step_key, "running", "")
                                        if step_key == "upload_main":
                                            data_source = site_info.get("main_data_source_id", "")
                                            if not data_source:
                                                return (domain, False, "未配置主数据源ID", None)
                                            from qmds.utils.site_operator import SiteOperator
                                            try:
                                                data_source = SiteOperator._normalize_data_source_ids(data_source)
                                            except ValueError as e:
                                                return (domain, False, str(e), None)
                                            start_cs = site_info.get("main_data_cs", "0")
                                            cs_field = "main_data_cs"
                                        else:
                                            data_source = site_info.get("extra_data_source_id", "")
                                            if not data_source:
                                                return (domain, False, "未配置补充数据源ID", None)
                                            from qmds.utils.site_operator import SiteOperator
                                            try:
                                                data_source = SiteOperator._normalize_data_source_ids(data_source)
                                            except ValueError as e:
                                                return (domain, False, str(e), None)
                                            start_cs = site_info.get("extra_data_cs", "0")
                                            cs_field = "extra_data_cs"

                                        if start_cs and start_cs != "0":
                                            task_manager.add_log(task_id, f"[{domain}] [{step_label}] 从断点 {start_cs} 继续", "info")

                                        def progress(msg):
                                            task_manager.add_log(task_id, f"[{domain}] [{step_label}] {msg}", "info")
                                            task_manager.update(task_id, current=idx + 1,
                                                                progress=int(step_base_progress + (idx + 0.5) / run_total * (step_end_progress - step_base_progress)),
                                                                message=f"[步骤{step_idx+1}] [{idx + 1}/{run_total}] [{domain}] {msg}")
                                        def save_breakpoint(cs_val):
                                            _site_db.update_site(domain, {cs_field: cs_val})
                                        def check_stop():
                                            return task_manager.is_stopped(task_id)

                                        result = operator.upload_data(domain, data_source, progress, start_cs=start_cs, breakpoint_callback=save_breakpoint, stop_callback=check_stop)
                                        final_cs = result.get("final_cs", "0")
                                        if result["success"]:
                                            return (domain, True, "", final_cs)
                                        return (domain, False, result["message"], final_cs)
                                    except Exception as e:
                                        return (domain, False, str(e), None)

                                with ThreadPoolExecutor(max_workers=10) as executor:
                                    futures = {executor.submit(_upload_worker, (i, d)): d for i, d in enumerate(current_domains)}
                                    for future in as_completed(futures):
                                        if task_manager.is_stopped(task_id):
                                            task_manager.update(task_id, status="stopped", message="任务已停止")
                                            executor.shutdown(wait=False, cancel_futures=True)
                                            return
                                        domain, ok, msg, final_cs = future.result()
                                        completed += 1
                                        if ok:
                                            if step_key == "upload_main":
                                                _site_db.update_site(domain, {"main_data_status": "已上传", "main_data_time": datetime.utcnow().isoformat(), "main_data_cs": "0"})
                                            else:
                                                _site_db.update_site(domain, {"extra_data_status": "已上传", "extra_data_time": datetime.utcnow().isoformat(), "extra_data_cs": "0"})
                                            _site_db.update_one_click_progress(domain, step_key, "success", "上传成功")
                                            task_manager.add_log(task_id, f"[{domain}] ✓ [{step_label}] 上传成功", "info")
                                            log.info(f"[一键建站] [{domain}] [{step_label}] ✓ 成功")
                                            step_success_domains.add(domain)
                                        else:
                                            if final_cs and final_cs != "0":
                                                cs_field = "main_data_cs" if step_key == "upload_main" else "extra_data_cs"
                                                _site_db.update_site(domain, {cs_field: final_cs})
                                                task_manager.add_log(task_id, f"[{domain}] [{step_label}] 断点已保存: {final_cs}", "warning")
                                            _site_db.update_one_click_progress(domain, step_key, "failed", msg)
                                            all_errors.append(f"{domain} [{step_label}]: {msg}")
                                            task_manager.add_log(task_id, f"[{domain}] ✗ [{step_label}] 上传失败: {msg}", "error")
                                            log.error(f"[一键建站] [{domain}] [{step_label}] 失败: {msg}")
                                        task_manager.update(task_id, current=completed,
                                                            progress=int(step_base_progress + completed / run_total * (step_end_progress - step_base_progress)),
                                                            message=f"[步骤{step_idx+1}] [{completed}/{run_total}] [{domain}] {'成功' if ok else '失败'}")

                            # ── 步骤3: 设置主分类 (单线程顺序, 与单独运行 set_main_category 相同) ──
                            elif step_key == "set_main_category":
                                for i, domain in enumerate(current_domains):
                                    if task_manager.is_stopped(task_id):
                                        task_manager.update(task_id, status="stopped", message="任务已停止")
                                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                                        return
                                    current = i + 1
                                    site_info = pending_sites.get(domain, {})
                                    intra_progress = int(step_base_progress + (current - 0.5) / run_total * (step_end_progress - step_base_progress))
                                    try:
                                        _site_db.update_one_click_progress(domain, step_key, "running", "")
                                        main_cat = site_info.get("main_category", "")
                                        task_manager.add_log(task_id, f"[{domain}] [设置主分类] 主分类: {main_cat}", "info")
                                        if not main_cat:
                                            raise Exception("未配置主分类")
                                        from qmds.utils.site_operator import get_operator
                                        operator = get_operator()
                                        task_manager.add_log(task_id, f"[{domain}] [设置主分类] 登录站点...", "info")
                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 登录站点...")
                                        login_result = operator.login(domain)
                                        if not login_result["success"]:
                                            raise Exception(f"登录失败: {login_result['message']}")
                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 设置分类中...")

                                        def cat_progress(msg):
                                            task_manager.add_log(task_id, f"[{domain}] [设置主分类] {msg}", "info")
                                            task_manager.update(task_id, current=current, progress=intra_progress,
                                                                message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] {msg}")
                                        set_result = operator.set_main_category(domain, main_cat, cat_progress)
                                        if not set_result["success"]:
                                            raise Exception(set_result["message"])
                                        _site_db.update_site(domain, {"main_category_status": "已上传",
                                                                      "main_category_time": datetime.utcnow().isoformat()})
                                        _site_db.update_one_click_progress(domain, step_key, "success", set_result.get("message", "主分类已设置"))
                                        task_manager.add_log(task_id, f"[{domain}] ✓ [设置主分类] 成功 - {set_result.get('message', '')}", "info")
                                        step_success_domains.add(domain)
                                    except Exception as e:
                                        err_msg = str(e)
                                        _site_db.update_one_click_progress(domain, step_key, "failed", err_msg)
                                        task_manager.add_log(task_id, f"[{domain}] ✗ [设置主分类] 失败 - {err_msg}", "error")
                                        log.error(f"[一键建站] [{domain}] [设置主分类] 失败: {err_msg}")
                                        all_errors.append(f"{domain} [设置主分类]: {err_msg}")
                                    task_manager.update(task_id, current=current,
                                                        progress=int(step_base_progress + current / run_total * (step_end_progress - step_base_progress)),
                                                        message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 完成")

                            # ── 步骤5: AI构建菜单 (单线程顺序, 与单独运行 ai_configure_menu 相同) ──
                            elif step_key == "ai_configure_menu":
                                for i, domain in enumerate(current_domains):
                                    if task_manager.is_stopped(task_id):
                                        task_manager.update(task_id, status="stopped", message="任务已停止")
                                        task_manager.add_log(task_id, "任务被用户停止", "warning")
                                        return
                                    current = i + 1
                                    site_info = pending_sites.get(domain, {})
                                    intra_progress = int(step_base_progress + (current - 0.5) / run_total * (step_end_progress - step_base_progress))
                                    try:
                                        _site_db.update_one_click_progress(domain, step_key, "running", "")
                                        main_cat = site_info.get("main_category", "")
                                        task_manager.add_log(task_id, f"[{domain}] [AI构建菜单] 主分类: {main_cat}", "info")
                                        wp_password = _site_db.get_setting("wp_password") or os.environ.get("WP_PASSWORD", "")
                                        ai_configurator = AiMenuConfigurator(wp_password)
                                        task_manager.update(task_id, current=current, progress=intra_progress,
                                                            message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] AI构建菜单...")

                                        def ai_menu_progress(msg):
                                            task_manager.add_log(task_id, f"[{domain}] [AI构建菜单] {msg}", "info")
                                            task_manager.update(task_id, current=current, progress=intra_progress,
                                                                message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] {msg}")
                                        result = ai_configurator.configure(domain, main_cat=main_cat, progress_callback=ai_menu_progress)
                                        if not result["success"]:
                                            raise Exception(result["message"])
                                        _site_db.update_site(domain, {"auto_category_status": "已配置",
                                                                      "auto_category_time": datetime.utcnow().isoformat()})
                                        _site_db.update_one_click_progress(domain, step_key, "success", result.get("message", "AI菜单构建完成"))
                                        task_manager.add_log(task_id, f"[{domain}] ✓ [AI构建菜单] 成功 - {result.get('message', '')}", "info")
                                        step_success_domains.add(domain)
                                    except Exception as e:
                                        err_msg = str(e)
                                        _site_db.update_one_click_progress(domain, step_key, "failed", err_msg)
                                        task_manager.add_log(task_id, f"[{domain}] ✗ [AI构建菜单] 失败 - {err_msg}", "error")
                                        log.error(f"[一键建站] [{domain}] [AI构建菜单] 失败: {err_msg}")
                                        all_errors.append(f"{domain} [AI构建菜单]: {err_msg}")
                                    task_manager.update(task_id, current=current,
                                                        progress=int(step_base_progress + current / run_total * (step_end_progress - step_base_progress)),
                                                        message=f"[步骤{step_idx+1}] [{current}/{run_total}] [{domain}] 完成")

                            # ── 本步骤结束：统计并筛选进入下一步的站点 ──
                            step_success_count = len(step_success_domains)
                            step_failed_count = step_total - step_success_count
                            task_manager.add_log(task_id, f"步骤 [{step_label}] 完成: 成功 {step_success_count}, 失败 {step_failed_count}, 共 {step_total}", "info")

                            # 成功站点（含断点跳过的）进入下一步，失败站点从待处理列表移除（断点已保存，下次运行从失败步骤继续）
                            new_pending = {}
                            for d in all_domains:
                                if d in step_success_domains:
                                    new_pending[d] = pending_sites[d]
                                else:
                                    total_failed_sites += 1
                            pending_sites = new_pending

                        # 所有步骤完成，剩余 pending_sites 即全程成功的站点
                        total_success_sites = len(pending_sites)

                        summary = f"一键建站完成: 全程成功 {total_success_sites}, 失败 {total_failed_sites}, 共 {total_sites} 个站点"
                        task_manager.update(task_id, status="completed", message=summary,
                                            progress=100, current=total_sites, total=total_sites)
                        task_manager.add_log(task_id, summary, "info")

                        if all_errors:
                            log.warning("[一键建站] 失败明细:")
                            for err in all_errors:
                                log.warning(f"  {err}")
                                task_manager.add_log(task_id, f"失败: {err}", "error")

                    except Exception as e:
                        log.error(f"[一键建站] 任务异常: {e}")
                        task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                        task_manager.add_log(task_id, f"任务异常: {e}", "error")
                    finally:
                        _site_db.close()

                task_manager.start_task_thread(task_id, run_one_click_task)
                flash(f"一键建站任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
                return redirect(url_for("core.tasks"))

            elif action == "update_status":
                field = request.form.get("status_field", "").strip()
                value = request.form.get("status_value", "").strip()

                if not field:
                    flash("请选择要更新的状态字段", "error")
                    return redirect(url_for("site_management.site_built", q=q))

                status_updaters = {
                    "health_status": site_db.batch_update_health_status,
                    "main_data_status": site_db.batch_update_main_data_status,
                    "extra_data_status": site_db.batch_update_extra_data_status,
                    "main_category_status": site_db.batch_update_main_category_status,
                    "auto_category_status": site_db.batch_update_auto_category_status,
                    "plugin_status": site_db.batch_update_plugin_status,
                    "media_status": site_db.batch_update_media_status,
                }

                updater = status_updaters.get(field)
                if not updater:
                    flash("不允许修改该字段", "error")
                    return redirect(url_for("site_management.site_built", q=q))

                count = updater(selected_ids, value)
                flash(f"已更新 {count} 个站点的 {field}", "success")
                return redirect(url_for("site_management.site_built", q=q))

            elif action == "export_selected":
                export_fields = request.form.getlist("export_fields")
                if not export_fields:
                    export_fields = ["build_time", "domain", "server", "template"]

                field_labels = {
                    "build_time": "建站时间",
                    "domain": "域名",
                    "server": "服务器",
                    "template": "模板底板",
                    "category": "大类",
                    "main_category": "主分类",
                    "title": "标题",
                    "description": "描述",
                    "address": "地址",
                    "health_status": "健康状态",
                    "main_data_status": "主数据状态",
                    "extra_data_status": "补充数据状态",
                    "main_category_status": "主分类状态",
                    "auto_category_status": "菜单状态",
                    "plugin_status": "插件状态",
                    "media_status": "媒体状态",
                    "report_status": "上报状态",
                    "report_time": "上报时间",
                    "created_at": "创建时间",
                }
                time_fields = {"build_time", "report_time", "created_at"}

                rows = []
                for sid in selected_ids:
                    site = site_db.get_site_by_id(sid)
                    if not site:
                        continue
                    row = {}
                    for field in export_fields:
                        value = site.get(field, "")
                        if field in time_fields and value:
                            try:
                                dt = datetime.fromisoformat(str(value))
                                value = dt.strftime("%Y/%m/%d")
                            except Exception:
                                value = str(value)[:10].replace("-", "/")
                        row[field_labels.get(field, field)] = value
                    rows.append(row)

                if not rows:
                    flash("没有可导出的站点数据", "error")
                    return redirect(url_for("site_management.site_built", q=q))

                from io import BytesIO
                import pandas as pd
                output = BytesIO()
                columns = [field_labels.get(f, f) for f in export_fields]
                pd.DataFrame(rows, columns=columns).to_excel(output, index=False, engine="openpyxl")
                output.seek(0)

                filename = f"built_sites_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
                return send_file(
                    output,
                    as_attachment=True,
                    download_name=filename,
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

            task_id = f"built_{action}_{int(time.time())}"
            action_labels = {
                "health_check": "健康检查",
                "upload_main": "上传主数据",
                "upload_extra": "上传补充数据",
                "set_main_category": "设置主分类",
                "clear_cache": "清理缓存",
                "configure_menu": "设置菜单",
                "ai_configure_menu": "AI构建菜单",
                "configure_sites": "配置站点",
            }
            label = action_labels.get(action, action)
            task_manager.create(task_id, action, f"{label} ({len(selected_ids)} sites)")

            def run_task():
                _site_db = SiteDBClient()
                try:
                    task_manager.update(task_id, message=f"[准备] 读取 {len(selected_ids)} 个站点信息...", current=0, total=len(selected_ids))
                    task_manager.add_log(task_id, f"任务启动: {label}", "info")
                    task_manager.add_log(task_id, f"读取 {len(selected_ids)} 个站点信息...", "info")
                    domains = []
                    site_map = {}
                    for sid in selected_ids:
                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped", message="任务已停止")
                            task_manager.add_log(task_id, "任务被用户停止", "warning")
                            return
                        site = _site_db.get_site_by_id(sid)
                        if site and site.get("domain"):
                            domains.append(site["domain"])
                            site_map[site["domain"]] = site

                    if not domains:
                        task_manager.update(task_id, status="failed", message="未找到有效站点")
                        task_manager.add_log(task_id, "未找到有效站点", "error")
                        return

                    total = len(domains)
                    task_manager.update(task_id, status="running",
                                        message=f"[初始化] 共 {total} 个站点待处理",
                                        total=total, current=0)
                    task_manager.add_log(task_id, f"共 {total} 个站点待处理", "info")

                    success = 0
                    failed = 0
                    errors = []

                    if action in ("upload_main", "upload_extra"):
                        completed = 0
                        def _worker(idx_domain):
                            idx, domain = idx_domain
                            site_info = site_map.get(domain, {})
                            from qmds.utils.site_operator import get_operator
                            operator = get_operator()
                            try:
                                if action == "upload_main":
                                    data_source = site_info.get("main_data_source_id", "")
                                    if not data_source:
                                        return (domain, False, "未配置主数据源ID", None)
                                    from qmds.utils.site_operator import SiteOperator
                                    try:
                                        data_source = SiteOperator._normalize_data_source_ids(data_source)
                                    except ValueError as e:
                                        return (domain, False, str(e), None)
                                    start_cs = site_info.get("main_data_cs", "0")
                                    if start_cs and start_cs != "0":
                                        task_manager.add_log(task_id, f"[{domain}] 从断点 {start_cs} 继续上传")
                                    def progress(msg):
                                        task_manager.add_log(task_id, f"[{domain}] {msg}")
                                        task_manager.update(task_id, current=idx + 1,
                                                            progress=int((idx + 0.5) / total * 100),
                                                            message=f"[{idx + 1}/{total}] [{domain}] {msg}")
                                    def save_breakpoint(cs_val):
                                        _site_db.update_site(domain, {"main_data_cs": cs_val})
                                    def check_stop():
                                        return task_manager.is_stopped(task_id)
                                    result = operator.upload_data(domain, data_source, progress, start_cs=start_cs, breakpoint_callback=save_breakpoint, stop_callback=check_stop)
                                    final_cs = result.get("final_cs", "0")
                                    if result["success"]:
                                        return (domain, True, "", final_cs)
                                    return (domain, False, result["message"], final_cs)
                                elif action == "upload_extra":
                                    extra_source = site_info.get("extra_data_source_id", "")
                                    if not extra_source:
                                        return (domain, False, "未配置补充数据源ID", None)
                                    from qmds.utils.site_operator import SiteOperator
                                    try:
                                        extra_source = SiteOperator._normalize_data_source_ids(extra_source)
                                    except ValueError as e:
                                        return (domain, False, str(e), None)
                                    start_cs = site_info.get("extra_data_cs", "0")
                                    if start_cs and start_cs != "0":
                                        task_manager.add_log(task_id, f"[{domain}] 从断点 {start_cs} 继续上传")
                                    def progress(msg):
                                        task_manager.add_log(task_id, f"[{domain}] {msg}")
                                        task_manager.update(task_id, current=idx + 1,
                                                            progress=int((idx + 0.5) / total * 100),
                                                            message=f"[{idx + 1}/{total}] [{domain}] {msg}")
                                    def save_breakpoint(cs_val):
                                        _site_db.update_site(domain, {"extra_data_cs": cs_val})
                                    def check_stop():
                                        return task_manager.is_stopped(task_id)
                                    result = operator.upload_data(domain, extra_source, progress, start_cs=start_cs, breakpoint_callback=save_breakpoint, stop_callback=check_stop)
                                    final_cs = result.get("final_cs", "0")
                                    if result["success"]:
                                        return (domain, True, "", final_cs)
                                    return (domain, False, result["message"], final_cs)
                            except Exception as e:
                                return (domain, False, str(e), None)

                        with ThreadPoolExecutor(max_workers=10) as executor:
                            futures = {executor.submit(_worker, (i, d)): d for i, d in enumerate(domains)}
                            for future in as_completed(futures):
                                if task_manager.is_stopped(task_id):
                                    task_manager.update(task_id, status="stopped", message="任务已停止")
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    return
                                domain, ok, msg, final_cs = future.result()
                                completed += 1
                                if ok:
                                    if action == "upload_main":
                                        _site_db.update_site(domain, {"main_data_status": "已上传", "main_data_time": datetime.utcnow().isoformat(), "main_data_cs": "0"})
                                    elif action == "upload_extra":
                                        _site_db.update_site(domain, {"extra_data_status": "已上传", "extra_data_time": datetime.utcnow().isoformat(), "extra_data_cs": "0"})
                                    success += 1
                                    task_manager.add_log(task_id, f"[{domain}] ✓ 上传成功", "info")
                                    log.info(f"[{label}] [{domain}] ✓ 成功")
                                else:
                                    if final_cs and final_cs != "0":
                                        cs_field = "main_data_cs" if action == "upload_main" else "extra_data_cs"
                                        _site_db.update_site(domain, {cs_field: final_cs})
                                        task_manager.add_log(task_id, f"[{domain}] 断点已保存: {final_cs}", "warning")
                                    failed += 1
                                    errors.append(f"{domain}: {msg}")
                                    task_manager.add_log(task_id, f"[{domain}] ✗ 上传失败: {msg}", "error")
                                    log.error(f"[{label}] [{domain}] 失败: {msg}")
                                task_manager.update(task_id, current=completed,
                                                    progress=int(completed / total * 100),
                                                    message=f"[{completed}/{total}] [{domain}] {'成功' if ok else '失败'}")

                    elif action == "configure_sites":
                        for i, domain in enumerate(domains):
                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                return
                            current = i + 1
                            site_info = site_map.get(domain, {})
                            try:
                                from qmds.utils.site_operator import get_operator
                                operator = get_operator()
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.5) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 登录站点...")
                                login_result = operator.login(domain)
                                if not login_result["success"]:
                                    raise Exception(f"登录失败: {login_result['message']}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.4) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 配置WP Rocket...")
                                rocket_result = operator.process_rocket(domain)
                                log.info(f"[配置站点] [{domain}] WP Rocket: {rocket_result['message']}")
                                if not rocket_result["success"]:
                                    log.warning(f"[配置站点] [{domain}] WP Rocket配置失败: {rocket_result['message']}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.3) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 配置Yoast SEO...")
                                yoast_result = operator.process_yoast(domain)
                                log.info(f"[配置站点] [{domain}] Yoast: {yoast_result['message']}")
                                if not yoast_result["success"]:
                                    log.warning(f"[配置站点] [{domain}] Yoast配置失败: {yoast_result['message']}")
                                _site_db.update_site(domain, {"plugin_status": "已配置",
                                                              "plugin_time": datetime.utcnow().isoformat()})
                                log.info(f"[配置站点] [{domain}] ✓ 插件已配置")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 配置媒体...")
                                from qmds.config import settings as qmds_settings
                                media_root = str(qmds_settings.data_dir / "logos")
                                media_result = operator.configure_media(domain, media_root)
                                log.info(f"[配置站点] [{domain}] 媒体: {media_result['message']}")
                                _site_db.update_site(domain, {"media_status": "已配置",
                                                              "media_time": datetime.utcnow().isoformat()})
                                log.info(f"[配置站点] [{domain}] ✓ 媒体已配置")
                                success += 1
                                log.info(f"[配置站点] [{domain}] ✓ 成功")
                            except Exception as e:
                                failed += 1
                                errors.append(f"{domain}: {e}")
                                log.error(f"[配置站点] [{domain}] 失败: {e}")
                            task_manager.update(task_id, current=current,
                                                progress=int(current / total * 100),
                                                message=f"[{current}/{total}] [{domain}] 完成")

                    else:
                      for i, domain in enumerate(domains):
                        if task_manager.is_stopped(task_id):
                            task_manager.update(task_id, status="stopped", message="任务已停止")
                            return

                        current = i + 1
                        site_info = site_map.get(domain, {})

                        try:
                            task_manager.update(task_id, current=current,
                                                progress=int((current - 0.5) / total * 100),
                                                message=f"[{current}/{total}] [{domain}] 读取站点信息...")
                            template = site_info.get("template", "-")
                            server = site_info.get("server", "-")
                            log.info(f"[{label}] [{domain}] 模板={template}, 服务器={server}")

                            if task_manager.is_stopped(task_id):
                                task_manager.update(task_id, status="stopped", message="任务已停止")
                                return

                            task_manager.update(task_id, current=current,
                                                progress=int((current - 0.3) / total * 100),
                                                message=f"[{current}/{total}] [{domain}] 正在执行{label}...")

                            if action == "health_check":
                                log.info(f"[健康检查] [{domain}] 开始健康检查...")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 正在检查站点可访问性...")
                                from qmds.utils.site_operator import get_operator
                                operator = get_operator()
                                result = operator.health_check(domain)
                                if result["success"]:
                                    _site_db.update_site(domain, {"health_status": "正常"})
                                    log.info(f"[健康检查] [{domain}] ✓ 站点正常 (HTTP {result['status_code']})")
                                else:
                                    log.warning(f"[健康检查] [{domain}] ✗ {result['message']}")
                                    raise Exception(result["message"])

                            elif action == "set_main_category":
                                log.info(f"[设置主分类] [{domain}] 开始设置主分类...")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 读取分类配置...")
                                main_cat = site_info.get("main_category", "")
                                log.info(f"[设置主分类] [{domain}] 主分类: {main_cat}")
                                if not main_cat:
                                    raise Exception("未配置主分类")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.15) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 登录站点...")
                                from qmds.utils.site_operator import get_operator
                                operator = get_operator()
                                login_result = operator.login(domain)
                                if not login_result["success"]:
                                    raise Exception(f"登录失败: {login_result['message']}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.1) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 设置分类中...")
                                def main_cat_progress(msg):
                                    task_manager.update(task_id, current=current,
                                                        progress=int((current - 0.05) / total * 100),
                                                        message=f"[{current}/{total}] [{domain}] {msg}")
                                set_result = operator.set_main_category(domain, main_cat, main_cat_progress)
                                if not set_result["success"]:
                                    raise Exception(set_result["message"])
                                _site_db.update_site(domain, {"main_category_status": "已上传",
                                                              "main_category_time": datetime.utcnow().isoformat()})
                                log.info(f"[设置主分类] [{domain}] ✓ 主分类已设置")

                            elif action == "clear_cache":
                                log.info(f"[清理缓存] [{domain}] 开始清理缓存...")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.3) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 登录站点...")
                                from qmds.utils.site_operator import get_operator
                                operator = get_operator()
                                login_result = operator.login(domain)
                                if not login_result["success"]:
                                    raise Exception(f"登录失败: {login_result['message']}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 清理WP Rocket缓存...")
                                cache_result = operator.clear_cache(domain)
                                if not cache_result["success"]:
                                    raise Exception(cache_result["message"])
                                log.info(f"[清理缓存] [{domain}] ✓ 缓存已清理")

                            elif action == "configure_menu":
                                main_cat = site_info.get("main_category", "")
                                log.info(f"[设置菜单] [{domain}] 开始设置菜单, 主分类: {main_cat}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 读取菜单配置...")
                                wp_password = _site_db.get_setting("wp_password") or os.environ.get("WP_PASSWORD", "")
                                from qmds.utils.wp_menu_config import WpMenuConfigurator
                                configurator = WpMenuConfigurator(wp_password)
                                def menu_progress(msg):
                                    task_manager.update(task_id, current=current,
                                                        progress=int((current - 0.1) / total * 100),
                                                        message=f"[{current}/{total}] [{domain}] {msg}")
                                result = configurator.configure(domain, main_cat=main_cat, progress_callback=menu_progress)
                                if not result["success"]:
                                    raise Exception(result["message"])
                                _site_db.update_site(domain, {"auto_category_status": "已配置",
                                                              "auto_category_time": datetime.utcnow().isoformat()})
                                log.info(f"[设置菜单] [{domain}] ✓ {result['message']}")

                            elif action == "ai_configure_menu":
                                main_cat = site_info.get("main_category", "")
                                log.info(f"[AI构建菜单] [{domain}] 开始, 主分类: {main_cat}")
                                task_manager.update(task_id, current=current,
                                                    progress=int((current - 0.2) / total * 100),
                                                    message=f"[{current}/{total}] [{domain}] 读取菜单配置...")
                                wp_password = _site_db.get_setting("wp_password") or os.environ.get("WP_PASSWORD", "")
                                from qmds.utils.ai_menu_builder import AiMenuConfigurator
                                ai_configurator = AiMenuConfigurator(wp_password)
                                def ai_menu_progress(msg):
                                    task_manager.update(task_id, current=current,
                                                        progress=int((current - 0.1) / total * 100),
                                                        message=f"[{current}/{total}] [{domain}] {msg}")
                                result = ai_configurator.configure(domain, main_cat=main_cat, progress_callback=ai_menu_progress)
                                if not result["success"]:
                                    raise Exception(result["message"])
                                _site_db.update_site(domain, {"auto_category_status": "已配置",
                                                              "auto_category_time": datetime.utcnow().isoformat()})
                                log.info(f"[AI构建菜单] [{domain}] ✓ {result['message']}")

                            task_manager.update(task_id, current=current,
                                                progress=int(current / total * 100),
                                                message=f"[{current}/{total}] ✓ [{domain}] {label}完成")
                            success += 1

                        except Exception as e:
                            failed += 1
                            errors.append(f"{domain}: {e}")
                            log.error(f"[{label}] [{domain}] 失败: {e}")
                            task_manager.update(task_id, current=current,
                                                progress=int(current / total * 100),
                                                message=f"[{current}/{total}] ✗ [{domain}] {label}失败 - {e}")

                    summary = f"{label}完成: 成功 {success}, 失败 {failed}, 共 {total} 个站点"
                    task_manager.update(task_id, status="completed", message=summary,
                                        progress=100, current=total, total=total)
                    task_manager.add_log(task_id, summary, "info")

                    if errors:
                        log.warning(f"[{label}] 失败明细:")
                        for err in errors:
                            log.warning(f"  {err}")
                            task_manager.add_log(task_id, f"失败: {err}", "error")

                except Exception as e:
                    log.error(f"[{label}] 任务异常: {e}")
                    task_manager.update(task_id, status="failed", message=f"任务异常: {e}")
                    task_manager.add_log(task_id, f"任务异常: {e}", "error")
                finally:
                    _site_db.close()

            task_manager.start_task_thread(task_id, run_task)
            flash(f"{label}任务已启动: {len(selected_ids)} 个站点，可在任务页面查看进度", "success")
            return redirect(url_for("core.tasks"))

        result = site_db.list_built_sites(q, page=page, page_size=page_size)
        stats = site_db.get_stats()
        built_stats = site_db.get_built_stats()
        return render_template("site_built.html", sites=result["items"], stats=stats, built_stats=built_stats, q=q,
                               total=result["total"], page=result["page"], page_size=result["page_size"])
    except Exception as e:
        log.error(f"已建站页面错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_built.html", sites=[], stats={"built_sites": 0}, built_stats={}, q=q,
                               total=0, page=1, page_size=20)


@bp.route("/site-management/<site_id>/edit", methods=["GET", "POST"])
def site_edit(site_id):
    """编辑站点"""
    site_db = get_site_db()
    try:
        site = site_db.get_site_by_id(site_id)
        if not site:
            flash("站点不存在", "error")
            return redirect(url_for("site_management.site_management"))

        if request.method == "POST":
            updates = {
                "domain": request.form.get("domain", ""),
                "template": request.form.get("template", ""),
                "server": request.form.get("server", ""),
                "category": request.form.get("category", ""),
                "main_category": request.form.get("main_category", ""),
                "main_data_source_id": request.form.get("main_data_source_id", ""),
                "extra_data_source_id": request.form.get("extra_data_source_id", ""),
                "title": request.form.get("title", ""),
                "description": request.form.get("description", ""),
                "address": request.form.get("address", ""),
                "report_status": request.form.get("report_status", ""),
                "build_status": request.form.get("build_status", ""),
                "schedule_enabled": request.form.get("schedule_enabled", "0"),
                "schedule_time": request.form.get("schedule_time", ""),
            }
            site_db.update_site_by_id(site_id, updates)
            flash("站点信息已更新", "success")
            return redirect(url_for("site_management.site_edit", site_id=site_id))

        return render_template("site_edit.html", site=site)
    except Exception as e:
        log.error(f"编辑站点错误: {e}")
        flash(f"操作失败: {e}", "error")
        return redirect(url_for("site_management.site_management"))


@bp.route("/site-management/<site_id>/delete", methods=["POST"])
def site_delete(site_id):
    """删除站点"""
    site_db = get_site_db()
    try:
        site = site_db.get_site_by_id(site_id)
        if site:
            site_db.delete_site(site.get("domain", ""))
            flash("站点已删除", "success")
    except Exception as e:
        log.error(f"删除站点错误: {e}")
        flash(f"删除失败: {e}", "error")
    return redirect(url_for("site_management.site_management"))


@bp.route("/site-management/<site_id>/report", methods=["POST"])
def site_report(site_id):
    """将站点标记为已上报"""
    site_db = get_site_db()
    try:
        site_db.update_site_by_id(site_id, {"report_status": "已报"})
        flash("站点已标记为已上报", "success")
    except Exception as e:
        log.error(f"上报站点错误: {e}")
        flash(f"上报失败: {e}", "error")
    return redirect(url_for("site_management.site_local"))


@bp.route("/site-management/export-weekly", methods=["GET"])
def site_export_weekly():
    """导出本周已报域名Excel"""
    site_db = get_site_db()
    try:
        keyword = request.args.get("q", "").strip()
        export_data = site_db.export_reported_weekly(keyword)

        if not export_data:
            flash("本周没有可导出的已报数据", "error")
            return redirect(url_for("site_management.site_reported", q=keyword))

        from io import BytesIO
        import pandas as pd
        output = BytesIO()
        pd.DataFrame(export_data).to_excel(output, index=False)
        output.seek(0)

        from datetime import timedelta
        now = datetime.utcnow()
        week_start = now - timedelta(days=now.weekday())
        filename = f"weekly_report_{week_start.strftime('%Y%m%d')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        log.error(f"导出本周数据错误: {e}")
        flash(f"导出失败: {e}", "error")
        return redirect(url_for("site_management.site_reported"))


@bp.route("/site-management/batch-update", methods=["POST"])
def site_batch_update():
    """批量更新站点字段"""
    site_db = get_site_db()
    try:
        selected_ids = request.form.getlist("selected_ids")
        field = request.form.get("batch_field", "").strip()
        value = request.form.get("batch_value", "").strip()

        if not selected_ids:
            flash("请先勾选要更新的站点", "error")
            return redirect(url_for("site_management.site_local"))

        if not field:
            flash("请选择要更新的字段", "error")
            return redirect(url_for("site_management.site_local"))

        count = site_db.batch_update_fields(selected_ids, field, value)
        flash(f"已更新 {count} 个站点的{field}字段", "success")
    except Exception as e:
        log.error(f"批量更新错误: {e}")
        flash(f"更新失败: {e}", "error")
    return redirect(url_for("site_management.site_local"))


@bp.route("/site-management/<site_id>/detail", methods=["GET"])
def site_detail(site_id):
    """站点详情"""
    site_db = get_site_db()
    try:
        site = site_db.get_site_by_id(site_id)
        if not site:
            flash("站点不存在", "error")
            return redirect(url_for("site_management.site_management"))
        return render_template("site_detail.html", site=site)
    except Exception as e:
        log.error(f"获取站点详情错误: {e}")
        flash(f"获取详情失败: {e}", "error")
        return redirect(url_for("site_management.site_management"))


@bp.route("/site-management/<site_id>/open-menu", methods=["GET"])
def site_open_menu(site_id):
    """打开站点 WP 菜单后台

    从站群网址打开 Chrome 扩展移植的功能：根据域名推导用户名，
    渲染一个极简跳板页（加载后立即自动提交登录表单到 /bbwllogin/），
    WordPress 校验凭据后设置会话 Cookie 并跳转到
    /wp-admin/nav-menus.php 菜单页面。

    整个流程在用户浏览器中完成，会话 Cookie 直接保存在浏览器中，
    无需服务端预检，点击域名后新标签直接打开目标菜单页。
    """
    from qmds.utils.wp_menu_opener import (
        normalize_site, build_login_url, build_login_form_data, resolve_wp_password,
    )

    site_db = get_site_db()
    try:
        site = site_db.get_site_by_id(site_id)
        if not site:
            flash("站点不存在", "error")
            return redirect(url_for("site_management.site_management"))

        domain = (site.get("domain") or "").strip()
        if not domain:
            flash("该站点未配置域名", "error")
            return redirect(url_for("site_management.site_built"))

        try:
            site_info = normalize_site(domain)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("site_management.site_built"))

        wp_password = resolve_wp_password(site_db)

        login_url = build_login_url(site_info)
        form_data = build_login_form_data(site_info, wp_password)

        return render_template(
            "site_open_menu.html",
            domain=domain,
            login_url=login_url,
            form_data=form_data,
        )
    except Exception as e:
        log.error(f"打开站点菜单错误: {e}")
        flash(f"打开菜单失败: {e}", "error")
        return redirect(url_for("site_management.site_built"))
