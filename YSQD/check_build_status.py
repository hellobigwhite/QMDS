import sqlite3

# 连接到数据库
conn = sqlite3.connect('station_clients.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# 检查sites表中的build_status值
try:
    cursor.execute("SELECT id, domain, build_status FROM sites")
    rows = cursor.fetchall()
    print("sites表中的build_status值:")
    for row in rows:
        print(f"ID: {row['id']}, 域名: {row['domain']}, build_status: '{row['build_status']}'")
    
except sqlite3.Error as e:
    print(f"数据库错误: {e}")
finally:
    conn.close()
