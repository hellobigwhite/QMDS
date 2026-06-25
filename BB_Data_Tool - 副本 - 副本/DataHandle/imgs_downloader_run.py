from DataHandle import imgs_downloader_main
import os

def print_header():
    header = f"""
======================================================
通用图片下载工具
======================================================

使用说明：
1. 下载完成后生成 xxx_images.xlsx。
2. 线程数量不要设置过大，否则可能因请求过于频繁导致服务器拒绝连接。
3. 可输入 Excel 文件路径 或 TXT 文件路径（TXT 文件每行一个 Excel 文件路径）。
备注：shopify的图片需要选择去掉图片参数才能下载！

======================================================
    """
    print(header)

def clean_file_path(file_path):
    """
    清理路径字符串，去除首尾多余的引号和空格
    """
    return file_path.strip().strip('"')

def get_file_list(file_input):
    """
    判断输入是单个 Excel 文件还是 TXT 文件
    - 如果是 Excel 文件路径，返回包含该文件的列表
    - 如果是 TXT 文件路径，读取所有行，并返回所有文件路径的列表
    """
    file_path = clean_file_path(file_input)
    
    if not os.path.exists(file_path):
        print(f"错误：文件 '{file_path}' 不存在！")
        exit()
    
    if file_path.lower().endswith(".txt"):
        with open(file_path, "r", encoding="utf-8") as f:
            file_list = [clean_file_path(line) for line in f.readlines() if line.strip()]
        if not file_list:
            print("错误：TXT 文件为空，请检查内容！")
            exit()
        return file_list
    
    elif file_path.lower().endswith((".xls", ".xlsx")):
        return [file_path]
    
    else:
        print("错误：请输入有效的 Excel 文件 (.xls/.xlsx) 或 TXT 文件 (.txt)！")
        exit()

def run():
    print_header()

    file_input = input("请输入采集数据表 Excel 或 TXT 文件路径[可直接拖入]: ").strip('"')
    file_list = get_file_list(file_input)

    column_name = input("请输入图像链接列的表头名称（默认 Images，直接回车使用默认值）: ")
    column_name = column_name if column_name else 'Images'

    max_workers_input = input("请输入下载线程数量（默认 10，直接回车使用默认值）: ")
    try:
        max_workers = int(max_workers_input) if max_workers_input else 10
    except ValueError:
        print("输入的线程数量无效，使用默认值 10")
        max_workers = 10

    remove_params_input = input("是否去掉图片链接的参数部分？(y/n，默认 y): ")
    remove_params = remove_params_input.lower() == 'y' if remove_params_input else True

    for file_path in file_list:
        print(f"处理文件：{file_path}")
        try:
            imgs_downloader_main.run(file_path, column_name=column_name, max_workers=max_workers, remove_params=remove_params)
        except Exception as e:
            print(f"处理文件 {file_path} 时发生错误：{e}")

if __name__ == '__main__':
    run()
