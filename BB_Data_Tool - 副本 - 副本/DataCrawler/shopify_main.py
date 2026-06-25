"""Shopify 数据采集核心逻辑。"""

import glob
import os
import time

import pandas as pd
import requests

from DataCrawler.crawlbase_client import CrawlbaseClient
from DataCrawler.shopify_extract_data import extract_data_new
from DataCrawler.shopify_merge_csv import merge_csv_to_excel


def normalize_shop_url(url: str) -> str:
    cleaned = str(url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    if not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def build_products_json_url(url: str, page: int, limit: int = 200) -> str:
    base_url = normalize_shop_url(url)
    return f"{base_url}products.json?limit={int(limit)}&page={int(page)}"


def get_json(url, page, max_retries=5, retry_delay=5, client=None):
    """获取 Shopify 店铺指定页的 products.json 数据。"""
    request_url = build_products_json_url(url, page)
    retries = 0

    while retries <= max_retries:
        try:
            print(f"\r正在采集页码: {page}  产品数量: 200", end="")
            if client and client.enabled:
                return client.get_json(request_url)

            response = requests.get(
                request_url,
                timeout=200,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/133.0.0.0 Safari/537.36"
                    )
                },
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as error_http:
            print(f"HTTP 错误: {error_http}")
        except requests.exceptions.ConnectionError as error_connection:
            print(f"连接错误: {error_connection}")
        except requests.exceptions.Timeout as error_timeout:
            print(f"超时错误: {error_timeout}")
        except requests.exceptions.RequestException as error:
            print(f"请求异常: {error}")
        except ValueError as error:
            print(f"JSON 解析失败: {error}")
        except Exception as error:
            print(f"采集异常: {error}")

        retries += 1
        if retries <= max_retries:
            print(f"正在重试 ({retries}/{max_retries})，{retry_delay} 秒后继续...")
            time.sleep(retry_delay)
        else:
            print("已达到最大重试次数，请求失败。")

    return None


def to_df(products_json):
    """将 products.json 数据转换为 Pandas DataFrame。"""
    try:
        products_data = products_json.get("products", [])
        if not products_data:
            print("当前页没有产品数据。")
            return pd.DataFrame()
        return pd.DataFrame(products_data)
    except Exception as error:
        print("转换错误:", error)
        return pd.DataFrame()


def run(url, output_dir, encoding="utf-8-sig", max_empty_pages=3):
    """采集 Shopify 店铺产品并输出为 Excel。"""
    client = CrawlbaseClient.from_default_config()
    if client.enabled:
        print("已启用 Crawlbase 代理采集。")
    else:
        print("未启用 Crawlbase，当前使用直连 requests 采集。")

    page = 1
    chunk_count = 1
    empty_page_count = 0
    total_products = 0

    while True:
        products_json = get_json(
            url,
            page,
            max_retries=client.max_retries if client.enabled else 5,
            retry_delay=client.retry_delay if client.enabled else 5,
            client=client,
        )

        if not products_json:
            print(f"第 {page} 页没有数据，停止抓取。")
            break

        df = to_df(products_json)
        if not df.empty:
            output_file = os.path.join(output_dir, f"{chunk_count}.csv")
            df.to_csv(
                output_file,
                mode="w" if chunk_count == 1 else "a",
                index=False,
                header=True,
                encoding=encoding,
            )
            chunk_count += 1
            total_products += len(df)
            empty_page_count = 0
        else:
            print(f"第 {page} 页没有产品数据，跳过。")
            empty_page_count += 1

        if empty_page_count >= max_empty_pages:
            print(f"连续 {max_empty_pages} 页没有数据，停止抓取。")
            break

        page += 1

    print(f"总共采集到 {total_products} 条产品数据。")
    time.sleep(3)

    merge_csv_to_excel(output_dir, f"{output_dir}/data_original.xlsx")

    csv_files = glob.glob(os.path.join(output_dir, "*.csv"))
    for file in csv_files:
        try:
            os.remove(file)
            print(f"已删除文件: {file}", end="\r")
        except Exception as error:
            print(f"删除文件 {file} 时出错: {error}")

    time.sleep(3)

    merged_file_path = f"{output_dir}/data_original.xlsx"
    try:
        processed_df = extract_data_new(merged_file_path)
    except Exception as error:
        print(f"提取原始数据时发生错误: {error}")
        processed_df = None

    time.sleep(3)
    processed_file_path = f"{output_dir}/data.xlsx"

    try:
        if processed_df is not None:
            processed_df.to_excel(processed_file_path, index=False)
            print(f"提取数据已保存到 {processed_file_path}")
            return True
        print("处理后的数据为空，无法保存到 Excel 文件。")
    except Exception as error:
        print(f"保存数据时发生错误: {error}")
    return False