from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
import pandas as pd
import os
from datastore import DataStore
from constants import (
    CATEGORY_ID_MAP,
    CATEGORY_OPTIONS,
    COLUMNS,
    DB_PATH,
    DOMAIN_STATUS_LABELS,
    EXCEL_COLS,
    EXTRA_COLUMNS,
    BUILD_STATUS_COL,
    BUILD_TIME_COL,
    MEDIA_STATUS_COL,
    MEDIA_TIME_COL,
    HEALTH_STATUS_COL,
    HEALTH_TIME_COL,
    PLUGIN_STATUS_COL,
    PLUGIN_TIME_COL,
    MAIN_DATA_STATUS_COL,
    MAIN_DATA_TIME_COL,
    AUTO_CATEGORY_STATUS_COL,
    AUTO_CATEGORY_TIME_COL,
    MAIN_CATEGORY_STATUS_COL,
    MAIN_CATEGORY_TIME_COL,
    EXTRA_DATA_STATUS_COL,
    EXTRA_DATA_TIME_COL,
    REPORT_STATUS_COL,
    SCHEDULE_ENABLED_COL,
    SCHEDULE_TIME_COL,
    TABLE_NAME,
)

app = Flask(__name__, template_folder='templates_v2', static_folder='static_v2')
app.secret_key = 'v2-super-secret-key-2025'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

UPLOAD_FOLDER = 'uploads_v2'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)

@app.route('/')
def index():
    return redirect(url_for('sites_list'))

@app.route('/sites')
def sites_list():
    sites = store.query_rows('')
    return render_template('sites.html', sites=sites)

@app.route('/import', methods=['GET', 'POST'])
def import_excel():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('没有选择文件', 'error')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('没有选择文件', 'error')
            return redirect(request.url)
        
        if file:
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            try:
                df = pd.read_excel(filepath)
                count = 0
                
                for _, row in df.iterrows():
                    domain = str(row.get('域名', '')).strip()
                    template = str(row.get('底板', '')).strip()
                    main_data_source_id = str(row.get('主分类数据码', '')).strip()
                    extra_data_source_id = str(row.get('站群数据码', '')).strip()
                    main_category = str(row.get('主分类', '')).strip()
                    category = str(row.get('大类', '')).strip()
                    title = str(row.get('SEO Title（最大58字符）', '')).strip()
                    description = str(row.get('Meta Description', '')).strip()
                    address = str(row.get('地址', '')).strip()
                    server = str(row.get('服务器', '')).strip()
                    store.add_row({
                        'domain': domain,
                        'template': template,
                        'main_data_source_id': main_data_source_id,
                        'extra_data_source_id': extra_data_source_id,
                        'main_category': main_category,
                        'category': category,
                        'title': title,
                        'description': description,
                        'address': address,
                        'server': server,
                    })
                    count += 1
                
                flash(f'成功导入 {count} 条记录', 'success')
            except Exception as e:
                import traceback
                flash(f'导入失败: {str(e)}', 'error')
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)
            return redirect(url_for('sites_list'))
    return render_template('import.html')

@app.route('/config', methods=['GET', 'POST'])
def config():
    if request.method == 'POST':
        report_username = request.form.get('report_username', '')
        report_password = request.form.get('report_password', '')
        erp_username = request.form.get('erp_username', '')
        erp_password = request.form.get('erp_password', '')
        media_root = request.form.get('media_root', '')
        
        store.set_setting('report_username', report_username)
        store.set_setting('report_password', report_password)
        store.set_setting('erp_username', erp_username)
        store.set_setting('erp_password', erp_password)
        store.set_setting('media_root', media_root)
        flash('配置已保存', 'success')
    
    settings = {}
    settings['report_username'] = store.get_setting('report_username', '')
    settings['report_password'] = store.get_setting('report_password', '')
    settings['erp_username'] = store.get_setting('erp_username', '')
    settings['erp_password'] = store.get_setting('erp_password', '')
    settings['media_root'] = store.get_setting('media_root', '')
    
    return render_template('config.html', settings=settings)

@app.route('/report/<int:site_id>')
def report_domain(site_id):
    flash(f'域名上报功能已触发 (站点ID: {site_id})', 'info')
    return redirect(url_for('sites_list'))

@app.route('/build/<int:site_id>')
def build_site(site_id):
    flash(f'建站功能已触发 (站点ID: {site_id})', 'info')
    return redirect(url_for('sites_list'))

@app.route('/health/<int:site_id>')
def health_check(site_id):
    flash(f'健康检查功能已触发 (站点ID: {site_id})', 'info')
    return redirect(url_for('sites_list'))

@app.route('/delete/<int:site_id>', methods=['POST'])
def delete_site(site_id):
    store.delete_rows([str(site_id)])
    flash('站点已删除', 'success')
    return redirect(url_for('sites_list'))

@app.route('/delete-selected', methods=['POST'])
def delete_selected():
    selected_ids = request.form.getlist('selected_ids')
    if not selected_ids:
        flash('请先勾选要删除的行', 'error')
        return redirect(url_for('sites_list'))
    
    store.delete_rows(selected_ids)
    flash(f'已删除 {len(selected_ids)} 条记录', 'success')
    return redirect(url_for('sites_list'))

@app.route('/reported')
def reported_list():
    all_sites = store.query_rows('')
    reported_sites = [r for r in all_sites if (r[REPORT_STATUS_COL] or '') == '已报']
    return render_template('reported.html', reported_sites=reported_sites)

@app.route('/built')
def built_list():
    all_sites = store.query_rows('')
    built_sites = [r for r in all_sites if (r[BUILD_STATUS_COL] or '') == '已建站']
    return render_template('built.html', built_sites=built_sites)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003, debug=True)
