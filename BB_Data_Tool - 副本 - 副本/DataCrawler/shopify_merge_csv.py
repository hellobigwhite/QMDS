import os
import pandas as pd
import re

def clean_illegal_characters(text):
    """
    清理文本中的非法字符，这些字符在Excel中不能使用。
    """
    # 定义一个正则表达式，匹配所有不可打印的字符（ASCII范围控制字符）
    if isinstance(text, str):
        # 替换非法字符为空字符
        return re.sub(r'[^\x20-\x7E\x0A\x0D]', '', text)  # 保留基本的可打印字符和换行符、回车符
    return text

def merge_csv_to_excel(csv_folder, output_file):
    """
    将指定文件夹中的所有CSV文件合并为一个Excel文件。

    参数:
        csv_folder (str): 存放CSV文件的文件夹路径。
        output_file (str): 输出的Excel文件路径。
    """
    # 检查文件夹路径是否存在
    if not os.path.isdir(csv_folder):
        print(f"错误：文件夹路径 {csv_folder} 不存在。")
        return

    # 获取指定文件夹中所有CSV文件
    csv_files = [f for f in os.listdir(csv_folder) if f.endswith('.csv')]

    # 如果没有CSV文件，给出提示
    if not csv_files:
        print(f"警告：文件夹 {csv_folder} 中没有CSV文件。")
        return

    # 用于存储合并后的数据
    merged_data = []

    # 定义需要的列
    required_columns = ['title', 'body_html', 'product_type', 'variants', 'images', 'options']

    # 遍历每个CSV文件，读取并提取所需列
    for csv_file in csv_files:
        csv_path = os.path.join(csv_folder, csv_file)
        try:
            # 读取CSV文件
            df = pd.read_csv(csv_path)

            # 确保所有需要的列都存在，不存在的列填充为NaN
            for col in required_columns:
                if col not in df.columns:
                    df[col] = pd.NA

            # 提取指定的列
            df = df[required_columns]

            # 清理所有字符串字段中的非法字符
            for col in required_columns:
                if df[col].dtype == 'object':  # 只处理字符串列
                    df[col] = df[col].apply(clean_illegal_characters)

            # 将当前CSV文件的数据合并到最终数据集
            merged_data.append(df)

        except Exception as e:
            print(f"读取文件 {csv_file} 时发生错误: {e}")
            continue

    # 合并所有数据
    merged_df = pd.concat(merged_data, ignore_index=True)

    # 保存为Excel文件
    try:
        merged_df.to_excel(output_file, index=False)
        print(f"合并完成，保存为 {output_file}")
    except Exception as e:
        print(f"保存Excel文件时发生错误: {e}")
