#!/usr/bin/env python3
"""
同步YSQD SQLite站点数据到QMDS MongoDB
"""
import sqlite3
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from qmds.config import settings
from qmds.db.site_db import SiteDBClient


def main():
    # 连接SQLite
    sqlite_path = Path(__file__).parent / "YSQD" / "station_clients.db"
    if not sqlite_path.exists():
        print(f"SQLite数据库不存在: {sqlite_path}")
        return
    
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    
    # 连接MongoDB
    mongo = SiteDBClient()
    
    try:
        # 获取所有SQLite站点
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sites")
        rows = cursor.fetchall()
        
        print(f"从SQLite读取到 {len(rows)} 个站点")
        
        # 同步每个站点
        synced = 0
        updated = 0
        created = 0
        
        for row in rows:
            domain = row["domain"]
            if not domain:
                continue
            
            # 准备站点数据
            site_data = {
                "domain": domain,
                "template": row["template"] or "",
                "main_data_source_id": row["main_data_source_id"] or "",
                "extra_data_source_id": row["extra_data_source_id"] or "",
                "main_category": row["main_category"] or "",
                "category": row["category"] or "",
                "title": row["title"] or "",
                "description": row["description"] or "",
                "address": row["address"] or "",
                "store_pf": row["store_pf"] or "",
                "server": row["server"] or "",
                "logo": row["logo"] or "",
                "banner": row["banner"] or "",
                "icon": row["icon"] or "",
                "classification": row["classification"] or "",
                "build_flag": row["build_flag"] or "",
                "title_translation": row["title_translation"] or "",
                "description_translation": row["description_translation"] or "",
                "main_keyword": row["main_keyword"] or "",
                "long_tail_keywords": row["long_tail_keywords"] or "",
                "report_id": row["report_id"] or "",
                "domain_status": row["domain_status"] or "",
                "login_path": row["login_path"] or "",
                "report_time": row["report_time"] or "",
                "domain_resolved_time": row["domain_resolved_time"] or "",
                "schedule_enabled": row["schedule_enabled"] or "0",
                "schedule_time": row["schedule_time"] or "",
                "domain_number": row["domain_number"] or "",
                "build_status": row["build_status"] or "",
                "build_time": row["build_time"] or "",
                "media_status": row["media_status"] or "",
                "media_time": row["media_time"] or "",
                "health_status": row["health_status"] or "",
                "health_time": row["health_time"] or "",
                "plugin_status": row["plugin_status"] or "",
                "plugin_time": row["plugin_time"] or "",
                "main_data_status": row["main_data_status"] or "",
                "main_data_time": row["main_data_time"] or "",
                "auto_category_status": row["auto_category_status"] or "",
                "auto_category_time": row["auto_category_time"] or "",
                "main_category_status": row["main_category_status"] or "",
                "main_category_time": row["main_category_time"] or "",
                "extra_data_status": row["extra_data_status"] or "",
                "extra_data_time": row["extra_data_time"] or "",
                "auto_workflow_enabled": row["auto_workflow_enabled"] or "",
                "auto_workflow_step": row["auto_workflow_step"] or "",
                "auto_workflow_status": row["auto_workflow_status"] or "",
                "auto_workflow_retry_count": row["auto_workflow_retry_count"] or "",
                "auto_workflow_max_retry": row["auto_workflow_max_retry"] or "",
                "report_status": row["report_status"] or "未报",
            }
            
            # 检查MongoDB中是否存在该站点
            existing = mongo.get_site(domain)
            if existing:
                # 更新现有站点
                mongo.update_site(domain, site_data)
                updated += 1
            else:
                # 添加新站点
                mongo.add_site(site_data)
                created += 1
            
            synced += 1
        
        print(f"同步完成: 总计 {synced}, 新增 {created}, 更新 {updated}")
        
    except Exception as e:
        print(f"同步失败: {e}")
    finally:
        conn.close()
        mongo.close()


if __name__ == "__main__":
    main()