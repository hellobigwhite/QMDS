import sqlite3

# 连接到数据库
conn = sqlite3.connect('station_clients.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# 检查sites表的所有列
try:
    cursor.execute("PRAGMA table_info(sites)")
    columns = cursor.fetchall()
    print("sites表的所有列:")
    for col in columns:
        print(f"列名: {col[1]}, 类型: {col[2]}, 默认值: {col[4]}")
    
except sqlite3.Error as e:
    print(f"数据库错误: {e}")
finally:
    conn.close()
