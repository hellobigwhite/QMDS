"""配置管理路由"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from qmds.config import settings
from qmds.config.llm_models import list_llm_models
from qmds.modules.web.db_helpers import get_site_db
from qmds.utils.logger import get_logger

log = get_logger("web.config")

bp = Blueprint("config", __name__)

# 极速AI API Key 配置文件路径
_JISUAI_KEYS_FILE = settings.project_root / "jisuai_api_keys.txt"

# AI菜单 API Key 配置文件路径
_MENU_AI_KEYS_FILE = settings.project_root / "menu_ai_api_keys.txt"


def _read_jisuai_keys_file() -> str:
    """读取 jisuai_api_keys.txt 的完整内容（含注释行）"""
    if not _JISUAI_KEYS_FILE.exists():
        return ""
    return _JISUAI_KEYS_FILE.read_text(encoding="utf-8")


def _write_jisuai_keys_file(content: str) -> None:
    """写入 jisuai_api_keys.txt"""
    _JISUAI_KEYS_FILE.write_text(content, encoding="utf-8")


def _read_menu_ai_keys_file() -> str:
    """读取 menu_ai_api_keys.txt 的完整内容（含注释行）"""
    if not _MENU_AI_KEYS_FILE.exists():
        return ""
    return _MENU_AI_KEYS_FILE.read_text(encoding="utf-8")


def _write_menu_ai_keys_file(content: str) -> None:
    """写入 menu_ai_api_keys.txt"""
    _MENU_AI_KEYS_FILE.write_text(content, encoding="utf-8")


@bp.route("/config", methods=["GET", "POST"])
def site_config():
    site_db = get_site_db()
    try:
        if request.method == "POST":
            settings_to_save = {
                "report_username": request.form.get("report_username", ""),
                "report_password": request.form.get("report_password", ""),
                "erp_username": request.form.get("erp_username", ""),
                "erp_password": request.form.get("erp_password", ""),
                "wp_password": request.form.get("wp_password", ""),
                "media_root": request.form.get("media_root", ""),
                "seo_proxy": request.form.get("seo_proxy", ""),
                "seo_api_key": request.form.get("seo_api_key", ""),
                "ark_api_key": request.form.get("ark_api_key", ""),
                "llm_model": request.form.get("llm_model", ""),
                "rocket_cleanup_frequency": request.form.get("rocket_cleanup_frequency", "daily"),
                "rocket_preload_links": request.form.get("rocket_preload_links", "1"),
                "rocket_minify_css": request.form.get("rocket_minify_css", "1"),
                "rocket_minify_js": request.form.get("rocket_minify_js", "1"),
                "rocket_lazyload": request.form.get("rocket_lazyload", "1"),
                "rocket_remove_unused_css": request.form.get("rocket_remove_unused_css", "1"),
            }
            for key, value in settings_to_save.items():
                # 密码框掩码（全为.表示未修改，跳过）；textarea 明文字段不跳过
                if key not in ("media_root", "seo_proxy", "seo_api_key", "ark_api_key",
                               "report_username", "erp_username", "rocket_cleanup_frequency",
                               "rocket_preload_links", "rocket_minify_css", "rocket_minify_js",
                               "rocket_lazyload", "rocket_remove_unused_css"):
                    if value and all(c == '.' for c in value):
                        continue
                site_db.set_setting(key, value)

            # 极速AI API Key 写入配置文件（保留注释行）
            jisuai_keys_content = request.form.get("jisuai_api_keys", "")
            try:
                _write_jisuai_keys_file(jisuai_keys_content)
            except Exception as e:
                log.error(f"保存极速AI API Key 文件失败: {e}")
                flash(f"保存极速AI API Key 文件失败: {e}", "error")

            # AI菜单 API Key 写入配置文件（保留注释行）
            menu_ai_keys_content = request.form.get("menu_ai_api_keys", "")
            try:
                _write_menu_ai_keys_file(menu_ai_keys_content)
            except Exception as e:
                log.error(f"保存AI菜单 API Key 文件失败: {e}")
                flash(f"保存AI菜单 API Key 文件失败: {e}", "error")

            # LLM 模型或 Ark Key 变更时重置缓存的 LLM 客户端，确保下次调用立即生效
            try:
                from qmds.modules.data_scraper.ai_classifier import reset_glm_client
                reset_glm_client()
            except Exception as e:
                log.warning(f"重置 LLM 客户端失败: {e}")

            flash("配置已保存", "success")
            return redirect(url_for("config.site_config"))

        current_settings = site_db.get_all_settings()
        current_settings["jisuai_api_keys"] = _read_jisuai_keys_file()
        current_settings["menu_ai_api_keys"] = _read_menu_ai_keys_file()
        current_settings["llm_models"] = list_llm_models()
        current_settings["current_llm_model"] = current_settings.get("llm_model", "") or settings.llm_model
        return render_template("site_config.html", settings=current_settings)
    except Exception as e:
        log.error(f"配置页面错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_config.html", settings={})


@bp.route("/config/templates", methods=["GET", "POST"])
def config_templates():
    site_db = get_site_db()
    try:
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "add":
                name = request.form.get("name", "").strip()
                if name:
                    if site_db.add_template_option(name):
                        flash(f"模板 '{name}' 已添加", "success")
                    else:
                        flash(f"模板 '{name}' 已存在", "error")
            elif action == "delete":
                name = request.form.get("name", "").strip()
                if name:
                    site_db.delete_template_option(name)
                    flash(f"模板 '{name}' 已删除", "success")
            return redirect(url_for("config.config_templates"))

        templates = site_db.get_template_options()
        return render_template("site_options.html", option_type="模板", options=templates)
    except Exception as e:
        log.error(f"模板选项错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_options.html", option_type="模板", options=[])


@bp.route("/config/servers", methods=["GET", "POST"])
def config_servers():
    site_db = get_site_db()
    try:
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "add":
                name = request.form.get("name", "").strip()
                if name:
                    if site_db.add_server_option(name):
                        flash(f"服务器 '{name}' 已添加", "success")
                    else:
                        flash(f"服务器 '{name}' 已存在", "error")
            elif action == "delete":
                name = request.form.get("name", "").strip()
                if name:
                    site_db.delete_server_option(name)
                    flash(f"服务器 '{name}' 已删除", "success")
            return redirect(url_for("config.config_servers"))

        servers = site_db.get_server_options()
        return render_template("site_options.html", option_type="服务器", options=servers)
    except Exception as e:
        log.error(f"服务器选项错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_options.html", option_type="服务器", options=[])


@bp.route("/config/categories", methods=["GET", "POST"])
def config_categories():
    site_db = get_site_db()
    try:
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "add":
                name = request.form.get("name", "").strip()
                if name:
                    if site_db.add_main_category_option(name):
                        flash(f"主分类 '{name}' 已添加", "success")
                    else:
                        flash(f"主分类 '{name}' 已存在", "error")
            elif action == "delete":
                name = request.form.get("name", "").strip()
                if name:
                    site_db.delete_main_category_option(name)
                    flash(f"主分类 '{name}' 已删除", "success")
            return redirect(url_for("config.config_categories"))

        categories = site_db.get_main_category_options()
        return render_template("site_options.html", option_type="主分类", options=[c["name"] for c in categories])
    except Exception as e:
        log.error(f"主分类选项错误: {e}")
        flash(f"操作失败: {e}", "error")
        return render_template("site_options.html", option_type="主分类", options=[])
