import sqlite3

# 连接到数据库
conn = sqlite3.connect('station_clients.db')
cursor = conn.cursor()

# 检查sites表是否存在并获取记录数
try:
    cursor.execute("SELECT COUNT(*) FROM sites")
    count = cursor.fetchone()[0]
    print(f"sites表中的记录数: {count}")
    
    # 获取前10条记录的域名
    cursor.execute("SELECT domain FROM sites LIMIT 10")
    domains = cursor.fetchall()
    print("前10条记录的域名:")
    for domain in domains:
        print(f"- {domain[0]}")
        
except sqlite3.Error as e:
    print(f"数据库错误: {e}")
finally:
    conn.close()
