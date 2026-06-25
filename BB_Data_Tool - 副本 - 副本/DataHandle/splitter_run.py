from DataHandle import splitter_main
import os

def print_header():
    header = f"""
======================================================
批量拆表工具
======================================================

使用说明：

支持两种输入方式:
1. 单个 Excel 文件（.xlsx）。
2. TXT 文件（每行一个 Excel 文件路径）。

1. 输入单个 Excel 文件路径或 TXT 文件路径（每行一个 Excel 文件路径）；
2. 设置每个拆分文件的最大行数（默认 5000）；
3. 程序会自动完成拆分。

======================================================
    """
    print(header)

def clean_file_path(file_path):
    """
    清理路径字符串，去除首尾多余的引号和空格
    """
    return file_path.strip().strip('"')

def get_file_list(path_input):
    """
    解析输入路径：
    - 若为 Excel 文件，直接返回列表；
    - 若为 TXT 文件，读取所有 Excel 文件路径并返回列表。
    """
    path = clean_file_path(path_input)

    if not os.path.exists(path):
        print(f"错误：路径 '{path}' 不存在！")
        exit()

    if os.path.isfile(path):
        if path.lower().endswith(".txt"):
            with open(path, "r", encoding="utf-8") as f:
                file_list = [clean_file_path(line) for line in f.readlines() if line.strip()]
            if not file_list:
                print("错误：TXT 文件为空，请检查内容！")
                exit()
            return file_list

        elif path.lower().endswith(".xlsx"):
            return [path]

        else:
            print("错误：请输入有效的 Excel 文件 (.xlsx) 或 TXT 文件 (.txt)！")
            exit()

    else:
        print("错误：无效的输入路径！")
        exit()

def run():
    print_header()

    path_input = input("请输入 Excel 文件路径或 TXT 文件路径[可直接拖入]: ").strip('"')
    file_list = get_file_list(path_input)

    rows_per_file = input("请输入每个文件的最大行数（默认 5000）直接回车: ")
    try:
        rows_per_file = int(rows_per_file) if rows_per_file else 5000
    except ValueError:
        print("输入的最大行数无效，使用默认值 5000")
        rows_per_file = 5000

    print("请选择后缀模式：")
    print("1. 不处理后缀")
    print("2. 使用自定义后缀[自动加下横线_]")
    print("3. 按分卷自动添加不同后缀 _part1 _part2 ...")

    suffix_mode_input = input("请输入编号（1/2/3）：").strip()

    if suffix_mode_input == "1":
        suffix_mode = "none"
        custom_suffix = None
    elif suffix_mode_input == "2":
        suffix_mode = "custom"
        custom_suffix = input("请输入自定义后缀：")
    elif suffix_mode_input == "3":
        suffix_mode = "part"
        custom_suffix = None
    else:
        print("无效的选择，默认使用不处理后缀")
        suffix_mode = "none"
        custom_suffix = None

    for file_path in file_list:
        print(f"处理文件：{file_path}")
        try:
            splitter_main.run(file_path, rows_per_file, suffix_mode, custom_suffix)
        except Exception as e:
            print(f"处理文件 {file_path} 时发生错误：{e}")

if __name__ == '__main__':
    run()
