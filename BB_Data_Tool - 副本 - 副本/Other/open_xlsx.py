import pandas as pd
import time
from pathlib import Path

def run(file_path):
    """
    通用读取Excel文件的函数，只读取默认的第一个sheet，并计算耗时

    参数:
        file_path: Excel文件的路径，支持.xlsx和.xls格式

    返回:
        DataFrame对象，包含Excel文件第一个sheet的内容；
        如果发生错误，则返回None。
    """
    start_time = time.time()  # 记录开始时间
    try:
        print(f"正在读取文件: {file_path}")  # 打印读取的文件路径
        file_path = Path(file_path)
        # 默认读取第一个sheet
        data = pd.read_excel(file_path)
        end_time = time.time()  # 记录结束时间
        elapsed_time = end_time - start_time  # 计算耗时
        row_count = data.shape[0]  # 获取行数（不包括表头）
        print(f"文件读取完成: {file_path}，耗时: {elapsed_time:.4f} 秒，内容行数: {row_count} 行")  # 打印文件路径、耗时和行数
        return data
    except Exception as e:
        print(f"读取Excel文件时发生错误: {e}")
        return None

# 示例用法：
if __name__ == '__main__':
    file_path = r'C:\Users\huhu\Desktop\紫伟\汽配hypercat.com.xlsx'  # Excel文件路径
    df = run(file_path)
    if df is not None:
        print("读取第一个sheet的内容：")
        print(df.head()) 
