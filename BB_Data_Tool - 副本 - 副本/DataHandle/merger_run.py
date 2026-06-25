import os
from DataHandle import merger_main

def print_header():
    header = """
======================================================
批量合并表工具
======================================================

使用说明：
1. 输入文件夹路径，合并该文件夹内所有 xlsx 文件；
2. 或者输入txt文件路径，读取txt中的每一行作为文件夹路径进行合并；
备注：只支持 xlsx 格式，自动识别表头是否统一

======================================================
    """
    print(header)

def try_open_file(txt_path):
    """自动兼容不同编码格式打开txt文件"""
    encodings = ['utf-8', 'gbk', 'ISO-8859-1']
    for encoding in encodings:
        try:
            with open(txt_path, 'r', encoding=encoding) as f:
                return f.readlines()
        except UnicodeDecodeError:
            continue
    # 如果所有编码都失败，抛出异常
    raise UnicodeDecodeError(f"无法解码txt文件：{txt_path}，请检查文件编码。")

def process_txt_file(txt_path):
    """读取txt文件，逐行处理每个文件夹路径"""
    if not os.path.exists(txt_path):
        print(f"错误：txt文件不存在：{txt_path}")
        return
    
    try:
        lines = try_open_file(txt_path)
    except UnicodeDecodeError as e:
        print(e)
        return

    # 遍历每一行路径
    for line in lines:
        folder_path = line.strip().strip('"').strip("'")
        if os.path.isdir(folder_path):
            print(f"正在处理文件夹: {folder_path}")
            merger_main.run(folder_path)
        else:
            print(f"警告：路径无效或不是文件夹: {folder_path}")

def run():
    print_header()
    
    choice = input("请选择输入方式:\n1. 输入文件夹路径\n2. 输入txt文件路径\n请选择 (1/2): ").strip()
    
    if choice == '1':
        input_path = input("请输入文件夹路径: ").strip().strip('"').strip("'")
        if os.path.isdir(input_path):
            merger_main.run(input_path)
        else:
            print(f"错误：路径无效或不是文件夹: {input_path}")
    
    elif choice == '2':
        txt_path = input("请输入txt文件路径: ").strip().strip('"').strip("'")
        process_txt_file(txt_path)
    
    else:
        print("无效的选择，程序退出。")

if __name__ == '__main__':
    run()
