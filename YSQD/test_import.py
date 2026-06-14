import os
import pandas as pd

def test_excel_read():
    print("=== 测试Excel导入功能 ===\n")
    
    excel_path = filedialog.askopenfilename(
        title="选择Excel文件测试",
        filetypes=[("Excel文件", "*.xlsx *.xls")],
    )
    
    if not excel_path:
        print("未选择文件")
        return
    
    print(f"读取文件: {excel_path}")
    df = pd.read_excel(excel_path)
    print(f"\n列名: {list(df.columns)}")
    print(f"\n行数: {len(df)}")
    
    if len(df) > 0:
        print("\n=== 第一行数据 ===")
        first_row = df.iloc[0]
        for col in df.columns:
            print(f"{col}: {first_row[col]}")
        
        domain = str(first_row.get("域名", "")).strip()
        print(f"\n=== 检查媒体文件 ===")
        print(f"域名: {domain}")
        
        media_root = r"D:\logo"
        if domain:
            logo_path = os.path.join(media_root, domain, "logo.png")
            banner_path = os.path.join(media_root, domain, "banner.jpg")
            icon_path = os.path.join(media_root, domain, "icon.png")
            
            print(f"Logo路径: {logo_path} - {'存在' if os.path.exists(logo_path) else '不存在'}")
            print(f"Banner路径: {banner_path} - {'存在' if os.path.exists(banner_path) else '不存在'}")
            print(f"Icon路径: {icon_path} - {'存在' if os.path.exists(icon_path) else '不存在'}")
            
            folder_path = os.path.join(media_root, domain)
            if os.path.exists(folder_path):
                print(f"\n文件夹内容: {os.listdir(folder_path)}")

if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    test_excel_read()
    input("\n按回车键退出...")
