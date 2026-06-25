import os
import pandas as pd
from tqdm import tqdm

def load_excel(file_path: str) -> pd.DataFrame:
    """
    加载 Excel 文件并返回 DataFrame
    :param file_path: Excel 文件路径
    :return: 加载后的 DataFrame
    """
    try:
        df = pd.read_excel(file_path)
        print(f"文件 {file_path} 加载成功！")
        return df
    except Exception as e:
        raise ValueError(f"无法加载文件 {file_path}：{e}")

def check_column_format(df: pd.DataFrame, column_name: str) -> pd.DataFrame:
    """
    检查指定列的数据格式，无效的行将会删除。
    使用 tqdm 显示进度条，清洗阶段只打印清洗的行数。
    :param df: 待处理的 DataFrame
    :param column_name: 要检查的列名
    :return: 处理后的 DataFrame
    """
    if column_name in df.columns:
        invalid_rows = []
        # 获取非空数据，并使用 tqdm 显示进度
        series = df[column_name].dropna()
        for index, value in tqdm(series.items(), total=len(series), desc="检查数据进度"):
            value = str(value).strip()
            parts = value.split("|||")  # 按 "|||" 分隔变体

            if len(parts) > 2:  # 超过两个变体直接判定无效
                invalid_rows.append(index)
                continue

            # 检查每个变体的格式是否正确
            is_valid = True
            for part in parts:
                if "^" not in part:  # 必须包含 "^"
                    is_valid = False
                    break
                attribute, *values = part.split("^", 1)
                if not attribute or len(values) != 1:  # 检查属性名和值部分
                    is_valid = False
                    break
                value_list = values[0].split("#")  # 按 "#" 分隔值
                if not all(value_list):  # 确保值部分非空
                    is_valid = False
                    break

            if not is_valid:
                invalid_rows.append(index)

        # 删除无效内容行，并打印清洗数量
        count_removed = len(invalid_rows)
        df.drop(index=invalid_rows, inplace=True)
        print(f"清洗了 {count_removed} 行数据。")
        print(f"开始保存数据文件，请等待完成提示。")
    else:
        print(f"列 '{column_name}' 在文件中不存在！")
    return df

def save_to_file(df: pd.DataFrame, original_file_path: str):
    """
    保存校验后的文件，文件名后添加 "_checked"
    :param df: 待保存的 DataFrame
    :param original_file_path: 原始文件路径
    """
    dir_name, file_name = os.path.split(original_file_path)
    name, ext = os.path.splitext(file_name)
    new_file_path = os.path.join(dir_name, f"{name}_checked{ext}")
    df.to_excel(new_file_path, index=False)
    print(f"处理后的文件已保存到：{new_file_path}")

def run(file_path: str):
    """
    处理单个 Excel 文件：加载、校验指定列格式、保存处理后的文件
    :param file_path: Excel 文件路径
    """
    df = load_excel(file_path)
    df = check_column_format(df, "cf_opingts")
    save_to_file(df, file_path)

# 示例调用
if __name__ == '__main__':
    # 请将下面的路径替换为实际的 Excel 文件路径
    run(r"C:\Users\wenhu\Desktop\example.xlsx")
