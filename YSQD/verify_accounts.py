import sqlite3

db_path = "station_clients.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("SELECT key, value FROM settings WHERE key IN ('report_username', 'report_password', 'erp_username', 'erp_password')")
results = cursor.fetchall()

print("当前账号配置：")
for key, value in results:
    print(f"  {key}: {value}")

conn.close()
