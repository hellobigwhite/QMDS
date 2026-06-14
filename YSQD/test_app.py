import tkinter as tk
from app import StationApp

# 测试应用程序运行
try:
    app = StationApp()
    print("应用程序初始化成功！")
    print("尝试加载数据...")
    app._load_rows()
    print("本地站点管理数据加载成功！")
    app._load_reported()
    print("已报域名数据加载成功！")
    app._load_built()
    print("已建站数据加载成功！")
    app.destroy()
    print("测试完成，所有数据加载成功！")
except Exception as e:
    print(f"应用程序运行错误: {e}")
    import traceback
    traceback.print_exc()
