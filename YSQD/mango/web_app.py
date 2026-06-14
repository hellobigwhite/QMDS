#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shopify 管理工具网页端
"""

import os
import sys
import subprocess
from flask import Flask, render_template, request, jsonify
from pymongo import MongoClient

app = Flask(__name__)

# MongoDB 配置
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "shopify_data_new"

# 脚本目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_mongo_collections():
    """获取 MongoDB 集合列表"""
    try:
        client = MongoClient(MONGO_URI)
        db = client[DB_NAME]
        collections = [name for name in db.list_collection_names() if not name.startswith("system.")]
        return sorted(collections)
    except Exception as e:
        return []


def run_script(script, args=None):
    """运行脚本"""
    path = os.path.join(SCRIPT_DIR, script)
    if not os.path.exists(path):
        return f"脚本不存在: {script}"

    args = args or []

    if sys.platform.startswith("win"):
        cmd = f'{sys.executable} "{path}" {" ".join(args)}'
        subprocess.Popen(f'start cmd /k "{cmd}"', shell=True)
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["gnome-terminal", "--", sys.executable, path] + args)
    elif sys.platform.startswith("darwin"):
        cmd = f'{sys.executable} "{path}" {" ".join(args)}'
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script "{cmd}"'
        ])

    return f"正在运行: {script}"


@app.route('/')
def index():
    """首页"""
    collections = get_mongo_collections()
    return render_template('index.html', collections=collections)


@app.route('/run', methods=['POST'])
def run():
    """运行脚本"""
    data = request.json
    script = data.get('script')
    args = data.get('args', [])
    result = run_script(script, args)
    return jsonify({'result': result})


@app.route('/collections', methods=['GET'])
def collections():
    """获取集合列表"""
    collections = get_mongo_collections()
    return jsonify({'collections': collections})


if __name__ == '__main__':
    # 创建 templates 目录
    templates_dir = os.path.join(SCRIPT_DIR, 'templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
    
    # 创建 index.html
    index_html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Shopify 管理工具</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        :root {
            --primary: #4f46e5;
            --primary-dark: #4338ca;
            --success: #10b981;
            --info: #06b6d4;
            --warning: #f59e0b;
            --danger: #ef4444;
        }
        
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', system-ui, sans-serif;
        }
        
        .main-container {
            max-width: 1400px;
            padding: 40px 20px;
        }
        
        .header-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15);
            backdrop-filter: blur(10px);
        }
        
        .main-title {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 800;
            font-size: 2.5rem;
            margin-bottom: 10px;
        }
        
        .section-title {
            color: #1f2937;
            font-weight: 700;
            font-size: 1.3rem;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .section-title i {
            color: #667eea;
        }
        
        .card {
            border: none;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            transition: all 0.3s ease;
            height: 100%;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.12);
        }
        
        .card-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-weight: 600;
            padding: 15px 20px;
            border: none;
        }
        
        .card-body {
            background: white;
            padding: 20px;
        }
        
        .btn {
            border: none;
            border-radius: 12px;
            padding: 12px 24px;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.15);
        }
        
        .btn-success {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        
        .btn-info {
            background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%);
        }
        
        .form-select {
            border-radius: 12px;
            border: 2px solid #e5e7eb;
            padding: 12px 16px;
            transition: all 0.3s ease;
        }
        
        .form-select:focus {
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.2);
        }
        
        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 9999;
            max-width: 400px;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from {
                transform: translateX(100%);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        
        @keyframes slideOut {
            from {
                transform: translateX(0);
                opacity: 1;
            }
            to {
                transform: translateX(100%);
                opacity: 0;
            }
        }
        
        .notification.hide {
            animation: slideOut 0.3s ease forwards;
        }
        
        .alert {
            border: none;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.15);
        }
        
        .alert-success {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
        }
        
        .alert-info {
            background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%);
            color: white;
        }
        
        .alert-warning {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            color: white;
        }
        
        .alert-danger {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
        }
        
        .grid-section {
            margin-bottom: 40px;
        }
        
        .footer-text {
            color: rgba(255, 255, 255, 0.8);
            text-align: center;
            margin-top: 40px;
            font-size: 0.95rem;
        }
        
        .btn-icon {
            margin-right: 8px;
        }
        
        .collection-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 16px;
            padding: 25px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
        }
    </style>
</head>
<body>
    <!-- 通知区域 -->
    <div id="notification-container"></div>
    
    <div class="container main-container">
        <!-- 头部 -->
        <div class="header-card text-center">
            <h1 class="main-title">
                <i class="bi bi-shop-window me-3"></i>
                Shopify 管理工具
            </h1>
            <p class="text-muted mb-0">高效、便捷、一站式数据管理平台</p>
        </div>
        
        <!-- 数据采集 -->
        <div class="grid-section">
            <h2 class="section-title">
                <i class="bi bi-download"></i>
                数据采集
            </h2>
            <div class="row g-4">
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-link-45deg btn-icon"></i>爬取 URL
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('google_url.py', '🔗 爬取 URL')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-brain btn-icon"></i>自动分类 / 过滤 URL
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('shopify_category_v3.py', '🧠 自动分类 / 过滤 URL')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-box-seam btn-icon"></i>抓取数据
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('Crawling_data_version2.py', '📦 抓取数据')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 分类处理 -->
        <div class="grid-section">
            <h2 class="section-title">
                <i class="bi bi-folder2"></i>
                分类处理
            </h2>
            <div class="row g-4">
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-arrow-repeat btn-icon"></i>优化分类 / 移动回收站
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('dbCategorySet.py', '♻️ 优化分类 / 移动回收站')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-puzzle btn-icon"></i>主类分类
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('mainfenle.py', '🧩 主类分类')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-arrow-left-right btn-icon"></i>分类替换（Excel）
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('分类替换.py', '🔁 分类替换（Excel）')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 数据清理 -->
        <div class="grid-section">
            <h2 class="section-title">
                <i class="bi bi-broom"></i>
                数据清理
            </h2>
            <div class="row g-4">
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-soap btn-icon"></i>清理数据
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('dbCleaning.py', '🧼 清理数据')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-image btn-icon"></i>清理图片异常
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('dbImageCleaning.py', '🖼 清理图片异常')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-header">
                            <i class="bi bi-trash btn-icon"></i>删除 Crontab 任务
                        </div>
                        <div class="card-body">
                            <button class="btn btn-success w-100" onclick="runScript('crontab 1.py', '🗑️ 删除 Crontab 任务')">
                                <i class="bi bi-play-circle btn-icon"></i>运行
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 导出前去重 -->
        <div class="grid-section">
            <h2 class="section-title">
                <i class="bi bi-stars"></i>
                导出前去重（重要）
            </h2>
            <div class="collection-card">
                <div class="mb-4">
                    <label for="collection" class="form-label fw-bold">
                        <i class="bi bi-collection me-2"></i>选择集合
                    </label>
                    <select id="collection" class="form-select form-select-lg">
                        {% for coll in collections %}
                            <option value="{{ coll }}">{{ coll }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="row g-3">
                    <div class="col-md-6">
                        <button class="btn btn-primary w-100 btn-lg" onclick="runDedup()">
                            <i class="bi bi-rocket-takeoff btn-icon"></i>执行去重
                        </button>
                    </div>
                    <div class="col-md-6">
                        <button class="btn btn-primary w-100 btn-lg" onclick="runExport()">
                            <i class="bi bi-upload btn-icon"></i>导出并备份
                        </button>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 扩展功能 -->
        <div class="grid-section">
            <h2 class="section-title">
                <i class="bi bi-rocket"></i>
                扩展功能
            </h2>
            <div class="row g-4">
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('batchdeal.py', '📤 上传数据')">
                                <i class="bi bi-cloud-upload btn-icon"></i>上传数据
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('set.py', '🌐 SEO与WP设置')">
                                <i class="bi bi-globe btn-icon"></i>SEO与WP设置
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('transter.py', '🔄 数据转移')">
                                <i class="bi bi-arrow-left-right btn-icon"></i>数据转移
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('建站.py', '🏗️ 一键建站')">
                                <i class="bi bi-buildings btn-icon"></i>一键建站
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('client.py', '🖼️ 下载图片')">
                                <i class="bi bi-image btn-icon"></i>下载图片
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('收录查询.py', '🖼️ 收录查询')">
                                <i class="bi bi-search btn-icon"></i>收录查询
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('txt爬取.py', '📋 TXT爬取')">
                                <i class="bi bi-file-text btn-icon"></i>TXT爬取
                            </button>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body text-center">
                            <button class="btn btn-info w-100" onclick="runScript('URL导出.py', '📁 URL导出')">
                                <i class="bi bi-file-earmark-arrow-down btn-icon"></i>URL导出
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 底部说明 -->
        <div class="footer-text">
            <p>
                <i class="bi bi-lightbulb me-2"></i>所有脚本将在独立终端中运行
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <i class="bi bi-database me-2"></i>集合列表来自 MongoDB，可刷新页面更新
            </p>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function showNotification(message, type = 'info') {
            const container = document.getElementById('notification-container');
            const notification = document.createElement('div');
            notification.className = 'notification';
            
            const icon = {
                'success': 'bi-check-circle',
                'info': 'bi-info-circle',
                'warning': 'bi-exclamation-triangle',
                'danger': 'bi-x-circle'
            }[type] || 'bi-info-circle';
            
            notification.innerHTML = `
                <div class="alert alert-${type} alert-dismissible fade show" role="alert">
                    <i class="bi ${icon} me-2"></i>
                    ${message}
                    <button type="button" class="btn-close btn-close-white" onclick="closeNotification(this)"></button>
                </div>
            `;
            
            container.appendChild(notification);
            
            setTimeout(() => {
                notification.classList.add('hide');
                setTimeout(() => notification.remove(), 300);
            }, 4000);
        }
        
        function closeNotification(btn) {
            const notification = btn.closest('.notification');
            notification.classList.add('hide');
            setTimeout(() => notification.remove(), 300);
        }
        
        function runScript(script, displayName) {
            showNotification(`🚀 正在启动：${displayName}...`, 'info');
            
            fetch('/run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ script, args: [] })
            })
            .then(response => response.json())
            .then(data => {
                showNotification(data.result, 'success');
            })
            .catch(error => {
                showNotification('❌ 启动失败：' + error, 'danger');
            });
        }
        
        function runDedup() {
            const collection = document.getElementById('collection').value;
            showNotification(`🚀 正在启动：执行去重（${collection}）...`, 'info');
            
            fetch('/run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ script: 'qvchng.py', args: ['--collection', collection] })
            })
            .then(response => response.json())
            .then(data => {
                showNotification(data.result, 'success');
            })
            .catch(error => {
                showNotification('❌ 启动失败：' + error, 'danger');
            });
        }
        
        function runExport() {
            const collection = document.getElementById('collection').value;
            showNotification(`🚀 正在启动：导出并备份（${collection}）...`, 'info');
            
            fetch('/run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ script: 'mongodb_export_data_version4.py', args: ['--collection', collection] })
            })
            .then(response => response.json())
            .then(data => {
                showNotification(data.result, 'success');
            })
            .catch(error => {
                showNotification('❌ 启动失败：' + error, 'danger');
            });
        }
    </script>
</body>
</html>
    """
    
    with open(os.path.join(templates_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(index_html)
    
    print("启动网页端...")
    print("访问地址: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
