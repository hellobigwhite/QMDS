import pandas as pd
import time
from pathlib import Path

def read_csv_try_encodings(file_path, encodings=None):
    """
    尝试使用不同编码读取CSV文件，统一展示待测试的编码列表，
    读取时如果失败不打印错误信息，最终仅输出成功使用的编码，并打印耗时和内容行数。
    
    参数:
        file_path: CSV文件的路径
        encodings: 要尝试的编码列表，默认尝试 ["utf-8", "gbk", "gb2312", "latin1", "utf-16"]
    
    返回:
        成功读取的DataFrame对象；如果所有编码均失败则返回None
    """
    if encodings is None:
        encodings = ["utf-8", "gbk", "gb2312", "latin1", "utf-16"]
    
    start_time = time.time()  # 记录开始时间
    successful_encoding = None
    df = None

    print(f"正在读取文件: {file_path}")  # 打印读取的文件路径

    file_path = Path(file_path)

    for encoding in encodings:
        try:
            df = pd.read_csv(file_path, encoding=encoding)
            successful_encoding = encoding
            break
        except Exception:
            continue

    end_time = time.time()  # 记录结束时间
    elapsed_time = end_time - start_time  # 计算耗时

    if successful_encoding:
        row_count = df.shape[0]  # 获取行数（不包括表头）
        print(f"文件读取完成: {file_path}，编码: {successful_encoding}，耗时: {elapsed_time:.4f} 秒，内容行数: {row_count} 行")
        return df
    else:
        print("所有编码尝试均失败。")
        return None

# 示例用法：
if __name__ == '__main__':
    file_path = r'C:\Users\huhu\Desktop\汽配hypercat.com.csv'  # 请替换为你的CSV文件路径
    df = read_csv_try_encodings(file_path)
    if df is not None:
        print("CSV文件内容预览：")
        print(df.head())
