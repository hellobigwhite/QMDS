import pandas as pd
import os
import requests
import random
import string
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from tqdm import tqdm  # 使用 tqdm 显示整体下载进度

# 生成随机字符串的函数
def generate_random_string(length=6):
    return ''.join(random.choices(string.ascii_lowercase, k=length))

# 清理文件名中的非法字符
def sanitize_filename(filename):
    """移除文件名中的非法字符"""
    return ''.join(c for c in filename if c not in r'<>:"/\\|?*')

# 下载图片的函数（去掉单个文件下载时的进度显示和成功打印）
def download_image(url, download_folder, remove_params=True, timeout=20):
    try:
        response = requests.get(url, stream=True, timeout=timeout)
        if response.status_code == 200:
            if remove_params:
                original_file_name = url.split('/')[-1].split('?')[0]
            else:
                original_file_name = url.split('/')[-1]
            file_name, file_extension = os.path.splitext(original_file_name)
            file_name = sanitize_filename(file_name)

            # 生成新的文件名：时间戳在前面 + 随机6个字母
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            random_string = generate_random_string(6)
            unique_file_name = f"{timestamp}_{random_string}{file_extension}"
            file_path = os.path.join(download_folder, unique_file_name)

            # 直接下载并保存文件，不使用单个文件的进度条
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(1024):
                    if chunk:
                        f.write(chunk)
            return (url, unique_file_name)
        else:
            tqdm.write(f"下载失败，状态码: {response.status_code}，URL: {url}")
            return (url, None)
    except Exception as e:
        tqdm.write(f"下载过程中出现错误: {e}")
        return (url, None)

# 将已下载的链接和文件名记录到新的Excel表格
def save_downloaded_links(file_path, df, downloaded_records):
    tqdm.write(f"正在将已下载链接和文件名保存到Excel文件: {file_path}")
    df['本地图片'] = df['Images'].map(lambda x: dict(downloaded_records).get(x, None))
    df.to_excel(file_path, index=False)
    tqdm.write("保存完成！")

def run(input_excel, column_name='Images', max_workers=10, remove_params=True):
    # 检查文件是否存在
    if not os.path.exists(input_excel):
        print(f"错误：文件 {input_excel} 不存在！")
        return

    sheet_name = 0  # 读取第一个工作表
    download_folder_base = os.path.dirname(os.path.abspath(input_excel))
    input_file_name = os.path.splitext(os.path.basename(input_excel))[0]
    output_excel = os.path.join(download_folder_base, f"{input_file_name}_images.xlsx")

    start_time = time.time()

    # 创建下载文件夹（若不存在）
    download_folder = os.path.join(download_folder_base, "imgs")
    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    df = pd.read_excel(input_excel, sheet_name=sheet_name)
    if not isinstance(df, pd.DataFrame):
        print("读取的数据不是DataFrame，返回类型:", type(df))
        return

    df.columns = df.columns.str.strip()
    if column_name not in df.columns:
        print(f"错误：Excel文件中没有找到列 '{column_name}'")
        return
    image_links = df[column_name][1:].dropna().tolist()

    downloaded_records = []

    # 使用多线程下载图片，同时显示整体下载进度条
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_image, url, download_folder, remove_params, 10): url for url in image_links}
        for future in tqdm(as_completed(futures), total=len(futures), desc="总体下载进度"):
            url = futures[future]
            try:
                result = future.result()
                if result[1]:  # 下载成功则记录
                    downloaded_records.append(result)
            except Exception as e:
                tqdm.write(f"下载 {url} 过程中出现错误: {e}")

    save_downloaded_links(output_excel, df, downloaded_records)

    end_time = time.time()
    tqdm.write(f"所有下载任务已完成，耗时 {end_time - start_time:.2f} 秒，已保存下载记录。")
