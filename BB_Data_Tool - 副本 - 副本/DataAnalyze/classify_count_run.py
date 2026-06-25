# classify_count_run.py
from DataAnalyze import classify_count_main
import os

def print_header():
    header = """
======================================================
Excel Categories 统计工具
======================================================

使用说明：

支持三种输入方式:
1. 单个 Excel 文件（.xlsx）。
2. TXT 文件（每行一个 Excel 文件路径）。
3. 文件夹路径（自动识别 .xlsx 文件并批量处理）。

请输入文件路径后，程序会统计 'Categories' 列的计数。
统计结果都会保存到 数据分析 目录下的新 Excel 文件中，文件名格式为 [原文件名]_category_count.xlsx。

======================================================
    """
    print(header)

def get_file_list(path_input):
    """
    解析输入路径，获取所有 Excel 文件列表。
    """
    path = path_input.strip().strip('"')

    if not os.path.exists(path):
        print(f"错误：路径 '{path}' 不存在！")
        exit()

    if os.path.isfile(path):
        if path.lower().endswith(".xlsx"):
            return [path]
        elif path.lower().endswith(".txt"):
            with open(path, "r", encoding="utf-8") as f:
                file_list = [line.strip().strip('"') for line in f.readlines() if line.strip()]
            return file_list
        else:
            print("错误：请输入有效的 Excel 文件 (.xlsx) 或 TXT 文件 (.txt)！")
            exit()
    
    elif os.path.isdir(path):
        file_list = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".xlsx")]
        if not file_list:
            print("错误：文件夹中没有 Excel 文件！")
            exit()
        return file_list
    
    else:
        print("错误：无效的输入路径！")
        exit()

def run():
    print_header()
    path_input = input("请输入 Excel 文件路径、TXT 文件路径或文件夹路径: ").strip('"')
    file_list = get_file_list(path_input)
    
    for file_path in file_list:
        print(f"统计文件: {file_path}")
        category_counts = classify_count_main.run(file_path)
        
        # if category_counts:
        #     print("统计结果:")
        #     for category, count in category_counts.items():
        #         print(f"{category}: {count}")
        #     print("======================================================")

if __name__ == '__main__':
    run()
