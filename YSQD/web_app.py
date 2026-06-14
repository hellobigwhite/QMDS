import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from constants import CATEGORY_ID_MAP
from domain_reporter_client import DomainReporter
from erp_builder import ERPBuilder
from health_checker import healthcheck_domain

app = Flask(__name__)
app.secret_key = 'station_group_secret_key'
app.config['MEDIA_ROOT'] = r'E:\logo'
app.config['DB_PATH'] = 'station_clients.db'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

def get_db_connection():
    conn = sqlite3.connect(app.config['DB_PATH'])
    conn.row_factory = sqlite3.Row
    return conn

def get_settings():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    settings = {row['key']: row['value'] for row in cursor.fetchall()}
    conn.close()
    return settings

def init_settings():
    conn = get_db_connection()
    cursor = conn.cursor()
    default_settings = {
        "report_username": "liwei",
        "report_password": "123456",
        "erp_username": "linwei",
        "erp_password": "linwei123",
    }
    for key, value in default_settings.items():
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return redirect(url_for('sites_list'))

@app.route('/sites')
def sites_list():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sites ORDER BY id DESC")
    sites = cursor.fetchall()
    conn.close()
    return render_template('sites.html', sites=sites)

@app.route('/import', methods=['GET', 'POST'])
def import_excel():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('没有选择文件')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('没有选择文件')
            return redirect(request.url)
        
        if file:
            try:
                df = pd.read_excel(file)
                conn = get_db_connection()
                cursor = conn.cursor()
                count = 0
                
                for _, row in df.iterrows():
                    domain = str(row.get("域名", "")).strip()
                    template = str(row.get("底板", "")).strip()
                    main_data_source_id = str(row.get("主分类数据码", "")).strip()
                    extra_data_source_id = str(row.get("站群数据码", "")).strip()
                    main_category = str(row.get("主分类", "")).strip()
                    category = str(row.get("大类", "")).strip()
                    title = str(row.get("SEO Title（最大58字符）", "")).strip()
                    description = str(row.get("Meta Description", "")).strip()
                    address = str(row.get("地址", "")).strip()
                    server = str(row.get("服务器", "")).strip()
                    
                    cursor.execute(
                        """
                        INSERT INTO sites (
                            domain, template, main_data_source_id, extra_data_source_id,
                            main_category, category, title, description, address, server,
                            logo, banner, icon, report_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (domain, template, main_data_source_id, extra_data_source_id,
                         main_category, category, title, description, address, server,
                         "", "", "", "未报")
                    )
                    
                    last_id = cursor.lastrowid
                    
                    if domain:
                        logo_path = os.path.join(app.config['MEDIA_ROOT'], domain, "logo.png")
                        banner_path = os.path.join(app.config['MEDIA_ROOT'], domain, "banner.jpg")
                        icon_path = os.path.join(app.config['MEDIA_ROOT'], domain, "icon.png")
                        
                        logo_val = logo_path if os.path.exists(logo_path) else ""
                        banner_val = banner_path if os.path.exists(banner_path) else ""
                        icon_val = icon_path if os.path.exists(icon_path) else ""
                        
                        cursor.execute(
                            "UPDATE sites SET logo=?, banner=?, icon=? WHERE id=?",
                            (logo_val, banner_val, icon_val, last_id)
                        )
                    
                    count += 1
                
                conn.commit()
                conn.close()
                flash(f'成功导入 {count} 条数据！')
                return redirect(url_for('sites_list'))
                
            except Exception as e:
                flash(f'导入失败: {str(e)}')
                return redirect(request.url)
    
    return render_template('import.html')

@app.route('/config', methods=['GET', 'POST'])
def config():
    conn = get_db_connection()
    
    if request.method == 'POST':
        settings = [
            'report_username', 'report_password',
            'erp_username', 'erp_password',
            'erp_admin_id', 'wp_password',
            'main_data_concurrency'
        ]
        for key in settings:
            value = request.form.get(key, '')
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
        conn.commit()
        flash('配置已保存！')
        return redirect(url_for('config'))
    
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    settings = {row['key']: row['value'] for row in cursor.fetchall()}
    conn.close()
    
    return render_template('config.html', settings=settings)

@app.route('/site/<int:site_id>/delete', methods=['POST'])
def delete_site(site_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    conn.commit()
    conn.close()
    flash('站点已删除！')
    return redirect(url_for('sites_list'))

@app.route('/site/<int:site_id>/report', methods=['POST'])
def report_site(site_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    if not site:
        flash('站点不存在！')
        return redirect(url_for('sites_list'))
    
    settings = get_settings()
    username = settings.get('report_username', '')
    password = settings.get('report_password', '')
    
    if not username or not password:
        flash('请先在配置页面设置上报账号和密码！')
        return redirect(url_for('sites_list'))
    
    try:
        reporter = DomainReporter("http://123.60.135.93:8099", username, password)
        
        category_name = site['category'] or ''
        category_id = CATEGORY_ID_MAP.get(category_name)
        
        if not category_id:
            flash('分类无效！')
            return redirect(url_for('sites_list'))
        
        payload = {
            "name": site['domain'] or '',
            "serverip": site['server'] or '',
            "template": site['template'] or '',
            "category": category_id,
            "categoryTag": None,
            "language": None,
        }
        
        reporter.submit_domain(payload)
        
        conn.execute("UPDATE sites SET report_status = '已报' WHERE id = ?", (site_id,))
        
        try:
            info = reporter.fetch_domain_info(site['domain'] or '')
            conn.execute("UPDATE sites SET report_id = ?, domain_status = ? WHERE id = ?",
                        (str(info.get('id') or ''), str(info.get('status') or ''), site_id))
        except Exception:
            pass
        
        conn.commit()
        flash('域名上报成功！')
        
    except Exception as e:
        flash(f'上报失败: {str(e)}')
    
    conn.close()
    return redirect(url_for('sites_list'))

@app.route('/site/<int:site_id>/build', methods=['POST'])
def build_site(site_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    if not site:
        flash('站点不存在！')
        return redirect(url_for('sites_list'))
    
    settings = get_settings()
    erp_user = settings.get('erp_username', '')
    erp_pass = settings.get('erp_password', '')
    erp_admin_id = settings.get('erp_admin_id', '')
    
    if not erp_user or not erp_pass:
        flash('请先在配置页面设置ERP账号和密码！')
        return redirect(url_for('sites_list'))
    
    try:
        builder = ERPBuilder(erp_user, erp_pass, app.config['MEDIA_ROOT'], admin_id=erp_admin_id)
        builder.login()
        
        ok, resp = builder.build_site(
            domain=site['domain'] or '',
            server=site['server'] or '',
            template=site['template'] or '',
            title=site['title'] or '',
            description=site['description'] or '',
            address=site['address'] or '',
            category=site['category'] or '',
        )
        
        if ok:
            conn.execute("UPDATE sites SET build_status = '已建站', build_time = ? WHERE id = ?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site_id))
            flash('建站成功！')
        else:
            flash(f'建站失败: {resp}')
        
        conn.commit()
        
    except Exception as e:
        flash(f'建站失败: {str(e)}')
    
    conn.close()
    return redirect(url_for('sites_list'))

@app.route('/site/<int:site_id>/health', methods=['POST'])
def health_check(site_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    if not site:
        flash('站点不存在！')
        return redirect(url_for('sites_list'))
    
    domain = site['domain'] or ''
    if not domain:
        flash('域名不能为空！')
        return redirect(url_for('sites_list'))
    
    try:
        ok, status, info = healthcheck_domain(domain)
        if ok:
            conn.execute("UPDATE sites SET health_status = '正常', health_time = ? WHERE id = ?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site_id))
            msg = '健康检查成功'
            if info.get("has_redirect"):
                msg += f"，最终跳转到 {info['final_domain']}"
            page_title = info.get("page_title", "")
            if page_title:
                msg += f"，标题: {page_title[:50]}"
            flash(msg)
        else:
            conn.execute("UPDATE sites SET health_status = '异常', health_time = ? WHERE id = ?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site_id))
            msg = f'健康检查失败: {status}'
            if info.get("final_url"):
                msg += f"，最终URL: {info['final_url']}"
            flash(msg)
        
        conn.commit()
        
    except Exception as e:
        flash(f'健康检查异常: {str(e)}')
    
    conn.close()
    return redirect(url_for('sites_list'))

if __name__ == '__main__':
    init_settings()
    app.run(debug=True, host='127.0.0.1', port=5001)
