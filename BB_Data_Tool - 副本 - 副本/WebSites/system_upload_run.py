import os
from WebSites import system_upload_main

def print_header():
    header = f"""
======================================================
数据表上传站群系统辅助工具
======================================================

使用说明：
1. 本工具用于批量上传 Excel 数据表到站群管理系统；
2. 需提供包含 .xlsx 文件的文件夹路径，程序会自动处理所有符合条件的文件；
3. 可输入单个文件夹路径，或提供 TXT 文件路径（每行一个文件夹路径）；
4. 上传过程中，程序会记录日志，存储到 运行日志\站群系统上传日志_log.txt 方便后续排查问题。

======================================================
    """
    print(header)

def clean_path(path):
    """去除路径首尾的引号及空白字符"""
    return path.strip().strip('"').strip("'")

def get_folders(input_path):
    """
    解析输入路径：
    - 若为文件夹，则直接返回该路径；
    - 若为 TXT 文件，则解析其中的**文件夹路径**（忽略单个 Excel 文件）。
    """
    path = clean_path(input_path)

    if not os.path.exists(path):
        print(f"❌ 错误：路径 '{path}' 不存在！")
        exit(1)

    if os.path.isdir(path):
        return [path]  # 直接返回单个文件夹路径

    elif os.path.isfile(path) and path.lower().endswith(".txt"):
        # 读取 TXT 文件，获取所有**文件夹路径**
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        folder_list = [clean_path(line) for line in lines if os.path.isdir(clean_path(line))]

        if not folder_list:
            print("❌ 错误：TXT 文件中没有有效的文件夹路径！")
            exit(1)

        return folder_list

    else:
        print("❌ 错误：请输入有效的文件夹路径或 TXT 文件路径！")
        exit(1)

def run():
    print_header()
    
    input_path = input("请输入待上传表格的文件夹路径或 TXT 文件路径[可直接拖入]: ").strip()
    folder_list = get_folders(input_path)

    # 选择运行模式
    # mode_input = input("是否启用无头模式（Y/N，默认 Y）: ").strip().lower()
    # headless_mode = mode_input != "n"  # 默认为 True，输入 "n" 才切换到 False

    # print(f"\n🚀 任务开始，{'启用无头模式' if headless_mode else '启用显性模式'}...\n")

    for folder in folder_list:
        print(f"\n📂 处理文件夹：{folder}")
        try:
            #result = system_upload_main.run(folder, headless_mode)
            result = system_upload_main.run(folder)
            if result:
                print(f"✅ 文件夹 {folder} 上传完成！")
            else:
                print(f"⚠️ 文件夹 {folder} 上传过程中可能有异常！")
        except Exception as e:
            print(f"❌ 处理文件夹 {folder} 时发生错误：{e}")

    print("\n🎉 所有任务执行完毕！")

if __name__ == '__main__':
    run()
