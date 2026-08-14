"""网站收录分析路由"""

import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from qmds.modules.web.db_helpers import get_order_db, get_site_db
from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.seo")

bp = Blueprint("seo", __name__)


@bp.route("/seo-analysis")
def seo_analysis():
    order_db = get_order_db()
    try:
        if order_db:
            servers = order_db.get_servers()
            domains = sorted(set(s.get("domain", "") for s in servers if s.get("domain")))
        else:
            domains = []
    except Exception as e:
        log.error(f"获取域名列表失败: {e}")
        domains = []
    finally:
        if order_db:
            order_db.close()
    return render_template("seo_analysis.html", domains=domains)


@bp.route("/seo-analysis/query", methods=["POST"])
def seo_analysis_query():
    domains_text = request.form.get("domains", "").strip()
    interval = float(request.form.get("interval", 1.0))

    if not domains_text:
        flash("请输入域名列表", "error")
        return redirect(url_for("seo.seo_analysis"))

    domains = [d.strip() for d in domains_text.split("\n") if d.strip()]
    if not domains:
        flash("未找到有效域名", "error")
        return redirect(url_for("seo.seo_analysis"))

    task_id = f"seo_{int(time.time())}"
    task_manager.create(task_id, "seo_analysis", f"{len(domains)} 个域名")

    def run_task():
        try:
            from qmds.utils.seo_checker import SEOChecker
            checker = SEOChecker()
            task_manager.add_log(task_id, f"开始检查 {len(domains)} 个域名的 Google 收录状态", "info")

            results = []
            for i, domain in enumerate(domains, 1):
                if task_manager.is_stopped(task_id):
                    task_manager.update(task_id, status="stopped", message="任务已停止")
                    task_manager.add_log(task_id, "任务被用户停止", "warning")
                    checker.close()
                    return

                try:
                    result = checker.check_google_index(domain)
                    status = "已收录" if result.get("indexed") else "未收录"
                    results.append({"domain": domain, "indexed": result.get("indexed", False),
                                    "count": result.get("count", 0)})
                    task_manager.add_log(task_id, f"[{i}/{len(domains)}] {domain} → {status}", "info")
                except Exception as e:
                    results.append({"domain": domain, "indexed": False, "error": str(e)})
                    task_manager.add_log(task_id, f"[{i}/{len(domains)}] {domain} → 检查失败: {e}", "error")

                task_manager.update(task_id, progress=int(i / len(domains) * 100),
                                    current=i, total=len(domains),
                                    message=f"[{i}/{len(domains)}] {domain}")
                time.sleep(interval)

            checker.close()
            indexed_count = sum(1 for r in results if r.get("indexed"))
            summary = f"完成: {indexed_count}/{len(domains)} 个域名已收录"
            task_manager.update(task_id, status="completed", message=summary,
                                result={"results": results, "indexed": indexed_count, "total": len(domains)},
                                progress=100)
            task_manager.add_log(task_id, summary, "info")
        except Exception as e:
            log.error(f"SEO分析任务失败: {e}")
            task_manager.update(task_id, status="failed", message=f"失败: {e}")
            task_manager.add_log(task_id, f"任务失败: {e}", "error")

    task_manager.start_task_thread(task_id, run_task)
    flash(f"SEO分析任务已启动: {len(domains)} 个域名", "info")
    return redirect(url_for("seo.seo_analysis"))


@bp.route("/api/seo/domains")
def api_seo_domains():
    try:
        site_db = get_site_db()
        settings = site_db.get_all_settings()
        username = settings.get("report_username", "")
        password = settings.get("report_password", "")
        if not username or not password:
            return jsonify({"ok": False, "error": "请先在配置页面设置上报账号和密码"}), 400

        from qmds.utils.domain_reporter import DomainReporter, REPORT_API_BASE_URL
        reporter = DomainReporter(REPORT_API_BASE_URL, username, password)
        domains = reporter.fetch_all_domains()
        return jsonify({"ok": True, "data": domains})
    except Exception as e:
        log.error(f"获取域名列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
