from DataCrawler import shopify_main
import os


def print_header():
    """
    打印命令行界面的固定头部说明
    """
    header = f"""
======================================================
Shopify 自动采集工具
======================================================

使用说明：
1. 输入 Shopify 商店的 URL（例如：）。支持采集分类链接
2. 或输入包含多个 URL 的TXT文本文件路径，每行一个 URL
3. 程序将自动清理 URL（去除协议头、www. 和尾部斜杠）。
4. 采集结果将存储在当前脚本目录下的 "Data" 文件夹中, data_original.xlsx[原始数据文件] data.xlsx[统一采集表头的数据文件]
5. 每个商店的采集结果将根据域名创建独立的子文件夹。
6. 请确保网络连接正常。

======================================================
"""
    print(header)


def clean_domain(url):
    """
    清理输入的 URL，去除协议头（http/https）、www. 和尾部的斜杠（/）
    """
    # 去除协议头
    if url.startswith("http://") or url.startswith("https://"):
        url = url.split("://")[-1]
    # 去除 www.
    if url.startswith("www."):
        url = url[4:]
    # 去除尾部的斜杠
    url = url.rstrip("/")
    return url


def process_url(url):
    """
    处理单个 URL 进行数据采集
    """
    cleaned_url = clean_domain(url)
    print(f"开始采集 {cleaned_url} 的数据...")

    # 创建保存目录：当前脚本目录下的 DATA 文件夹，再根据完整域名创建子文件夹
    base_output_dir = os.path.join(os.getcwd(), "Data/studentsupplies")  # 当前脚本目录下的Data文件夹
    output_dir = os.path.join(base_output_dir, cleaned_url)  # 根据完整域名创建子文件夹

    # 确保目录存在
    if not os.path.exists(base_output_dir):
        os.makedirs(base_output_dir)
        print(f"目录 {base_output_dir} 已创建。")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"目录 {output_dir} 已创建。")

    # 调用采集核心函数
    shopify_main.run(cleaned_url, output_dir)
    print(f"采集完成，数据已保存到 {output_dir}")


def run():
    print_header()

    # 获取用户输入
    user_input = input("输入URL 或 .txt 文件路径[可直接拖入文件]: ").strip()

    # 判断输入是 URL 还是 TXT 文件路径
    if user_input.lower().endswith(".txt") and os.path.exists(user_input):
        print(f"检测到文件输入，开始读取 {user_input} 中的链接...")
        with open(user_input, "r", encoding="utf-8") as file:
            urls = [line.strip() for line in file if line.strip()]

        if not urls:
            print("文件为空或未找到有效 URL！")
            return

        for url in urls:
            process_url(url)
    else:
        # 直接处理输入的 URL
        process_url(user_input)


if __name__ == "__main__":
    run()
