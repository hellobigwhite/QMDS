import sqlite3

db_path = "station_clients.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 设置上报账号密码：liwei / 123456
cursor.execute(
    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
    ("report_username", "liwei")
)
cursor.execute(
    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
    ("report_password", "123456")
)

# 设置ERP账号密码：linwei / linwei123
cursor.execute(
    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
    ("erp_username", "linwei")
)
cursor.execute(
    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
    ("erp_password", "linwei123")
)

conn.commit()
conn.close()

print("账号密码已更新！")
print("- 上报账号: liwei / 123456")
print("- ERP账号: linwei / linwei123")
