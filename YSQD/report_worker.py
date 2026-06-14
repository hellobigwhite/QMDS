
import sys
import json
from datetime import datetime
import os
import sys

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datastore import DataStore
from domain_reporter_client import DomainReporter
from constants import (
    CATEGORY_ID_MAP,
    COLUMNS,
    DB_PATH,
    TABLE_NAME,
    REPORT_STATUS_COL,
    REPORT_TIME_COL,
    DOMAIN_RESOLVED_TIME_COL,
    SCHEDULE_ENABLED_COL,
    DOMAIN_NUMBER_COL,
    EXTRA_COLUMNS,
)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"参数错误：需要3个参数，实际收到{len(sys.argv)-1}个")
        print(f"参数列表：{sys.argv}")
        input("按回车键退出...")
        sys.exit(1)
    
    try:
        ids = json.loads(sys.argv[1])
        username = sys.argv[2]
        password = sys.argv[3]
        
        print(f"接收到的参数：")
        print(f"  站点ID数量：{len(ids)}")
        print(f"  用户名：{username}")
        print(f"  密码长度：{len(password)}")
        
        store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)
        
        # 获取当前最大域名编号
        cur = store._db.execute("SELECT MAX(domain_number) FROM sites WHERE report_status = '已报'")
        max_number = cur.fetchone()[0] or 0
        # 确保max_number是整数类型
        try:
            max_number = int(max_number)
        except (ValueError, TypeError):
            max_number = 0
        current_number = max_number + 1
        print(f"当前最大域名编号：{max_number}，下一个编号：{current_number}")

        reporter = DomainReporter("http://123.60.135.93:8099", username, password)

        success = 0
        failed = []
        for row_id in ids:
            # 直接从数据库获取行数据，确保即使搜索条件改变也能获取到
            row = store.get_row(row_id)
            if not row:
                failed.append((row_id, "数据不存在"))
                continue
            missing = []
            for col, _title in COLUMNS:
                if not (row[col] or "").strip():
                    missing.append(col)
            if missing:
                failed.append((row_id, f"字段缺失: {', '.join(missing)}"))
                continue

            domain = (row["domain"] or "").strip()
            server = (row["server"] or "").strip()
            template = (row["template"] or "").strip()
            category_name = (row["category"] or "").strip()
            category_id = CATEGORY_ID_MAP.get(category_name)
            if not category_id:
                failed.append((row_id, f"分类无效: {category_name}"))
                continue

            payload = {
                "name": domain,
                "serverip": server,
                "template": template,
                "category": category_id,
                "categoryTag": None,
                "language": None,
            }
            print(f"开始上报域名：{domain}")
            try:
                reporter.submit_domain(payload)
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 分配域名编号
                update_values = {
                    DOMAIN_NUMBER_COL: str(current_number),
                    REPORT_STATUS_COL: "\u5df2\u62a5",
                    REPORT_TIME_COL: now,
                    SCHEDULE_ENABLED_COL: "0",
                }
                current_number += 1
                try:
                    info = reporter.fetch_domain_info(domain)
                    status_val = info.get("status")
                    update_values["report_id"] = str(info.get("id") or "")
                    update_values["domain_status"] = str(status_val) if status_val is not None else ""
                    # 记录已解析时间
                    if status_val in {3, "3"}:
                        update_values[DOMAIN_RESOLVED_TIME_COL] = now
                except Exception as e:
                    print(f"获取域名信息失败: {e}")
                    update_values["report_id"] = ""
                    update_values["domain_status"] = ""
                store.update_fields(row_id, update_values)
                success += 1
                print(f"成功上报域名: {domain}")
            except Exception as exc:
                failed.append((row_id, str(exc)))
                print(f"上报失败: {domain}, 错误: {exc}")

        store.close()
        
        print(f"\n上报完成：成功 {success} 行，失败 {len(failed)} 行")
        if failed:
            print("失败详情:")
            for row_id, error in failed:
                print(f"  ID: {row_id}, 错误: {error}")
    except Exception as e:
        import traceback
        print(f"执行过程中出错: {e}")
        print(traceback.format_exc())
    input("按回车键退出...")
