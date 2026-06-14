import sqlite3
from datastore import DataStore
from constants import COLUMNS, DB_PATH, TABLE_NAME, REPORT_STATUS_COL, EXTRA_COLUMNS

# 测试数据加载流程
def test_data_load():
    print("测试数据加载流程...")
    
    # 创建DataStore实例
    store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)
    
    # 测试query_rows方法
    print("\n1. 测试query_rows方法:")
    rows = store.query_rows("")
    print(f"   共查询到 {len(rows)} 条记录")
    
    # 打印前5条记录
    print("\n2. 前5条记录:")
    for i, row in enumerate(rows[:5]):
        print(f"   记录 {i+1}:")
        for name, _ in COLUMNS:
            value = row.get(name, "") or ""
            print(f"     {name}: {value}")
    
    # 检查列是否存在
    print("\n3. 检查列是否存在:")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({TABLE_NAME})")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"   表中存在的列: {columns}")
    
    # 检查必要的列是否存在
    required_columns = [name for name, _ in COLUMNS] + [REPORT_STATUS_COL] + EXTRA_COLUMNS
    print("\n4. 检查必要列是否存在:")
    for col in required_columns:
        if col in columns:
            print(f"   ✓ {col} 存在")
        else:
            print(f"   ✗ {col} 不存在")
    
    conn.close()
    store.close()
    print("\n测试完成！")

if __name__ == "__main__":
    test_data_load()
