"""
从原始数据中提取信息并且转换成 统一表头文件 ['SKU', '标题', '描述', '子描述', '图片', '原价', '折扣价', '变体', '分类']
"""

import pandas as pd
import re
import ast


# 读取并处理 Excel 文件的函数
def extract_data_new(file_path):
    """
    读取 Excel 文件并提取需要的字段，返回一个处理后的 DataFrame。

    参数:
        file_path (str): Excel 文件的路径。

    返回:
        pd.DataFrame: 包含提取数据的 DataFrame。
    """
    # 读取原始 Excel 文件
    df = pd.read_excel(file_path)

    # 创建一个新的 DataFrame 用于存储处理后的数据
    processed_data = []

    # 遍历每一行，提取需要的内容
    for _, row in df.iterrows():
        sku = ''  # SKU 默认为空
        title = row['title']
        description = row['body_html']
        sub_description = ''  # 子描述默认为空
        images = extract_images(row['images'])
        original_price, discount_price = extract_prices(row['variants'])
        variants = extract_variants(row['options'])
        category = row['product_type']

        # 将提取的数据加入 processed_data 列表
        processed_data.append(
            [sku, title, description, sub_description, images, original_price, discount_price, variants, category])

    # 将处理后的数据转换为 DataFrame
    processed_df = pd.DataFrame(processed_data,
                                columns=['SKU', '标题', '描述', '子描述', '图片', '原价', '折扣价', '变体', '分类'])

    return processed_df




# 提取图片字段中的第一个 'src' 值
def extract_images(images_column):
    try:
        # 使用正则表达式提取第一个 src
        match = re.search(r"'src'\s*:\s*'([^']+)'", str(images_column))
        if match:
            return match.group(1)
    except Exception as e:
        print(f"图片提取出错: {e}")
    return ''


# 提取价格字段中的原价和折扣价
def extract_prices(variants_column):
    try:
        # 使用正则表达式提取价格
        match_original = re.search(r"'compare_at_price'\s*:\s*'([^']+)'", str(variants_column))
        match_discount = re.search(r"'price'\s*:\s*'([^']+)'", str(variants_column))

        original_price = match_original.group(1) if match_original else ''
        discount_price = match_discount.group(1) if match_discount else ''

        return original_price, discount_price
    except Exception as e:
        print(f"价格提取出错: {e}")
    return '', ''


# 提取变体信息并处理成需要的格式  [原代码会报json解析错误]
# def extract_variants(options_column):
#     try:
#         # 将单引号替换成双引号，解决解析问题
#         options_str = str(options_column)
#         options_str = options_str.replace("'", '"')

#         # 解析为 JSON 对象
#         options_json = json.loads(options_str)

#         variant_str = ''

#         # 遍历每个变体，提取 name 和 values
#         for option in options_json:
#             name = option.get('name')
#             if name == 'Title' or not name:  # 跳过无效变体
#                 continue

#             values = option.get('values', [])
#             if values:
#                 variant_str += f"{name}^{'#'.join(values)}" + "|||"

#         return variant_str.strip("|||")

#     except json.JSONDecodeError as e:
#         print(f"JSON 解析错误: {e}")
#     except Exception as e:
#         print(f"变体提取出错: {e}")

#     return ''

# 提取变体信息并处理成需要的格式
def extract_variants(options_column):
    try:
        # 直接解析 Python 格式的字符串（兼容单引号）
        options_list = ast.literal_eval(str(options_column))
        
        variant_str = ''
        for option in options_list:
            name = option.get('name')
            if name == 'Title' or not name:
                continue

            values = option.get('values', [])
            if values:
                variant_str += f"{name}^{'#'.join(values)}" + "|||"

        return variant_str.strip("|||")
    
    except (SyntaxError, ValueError) as e:
        print(f"解析变体数据失败: {e}")
        return ''
    except Exception as e:
        print(f"变体提取出错: {e}")
        return ''


if __name__ == "__main__":
    # 测试文件路径
    file_path = r"C:\Users\Administrator\Desktop\测试\www.naturalbabyshower.co.uk.xlsx"

    # 调用封装的方法处理 Excel 文件并获取结果
    processed_df = extract_data_new(file_path)

    # 输出结果，可以保存到新的 Excel 文件中
    processed_df.to_excel('processed_data.xlsx', index=False)

