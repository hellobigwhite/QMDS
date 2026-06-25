import os
import pandas as pd





def run(input_folder):
    """
    批量合并用户指定文件夹中的所有xlsx文件，并保存到该文件夹。
    自动检测所有表的表头是否一致，如果一致则合并。
    """
    

    # 验证输入路径是否有效
    if not os.path.isdir(input_folder):
        print("输入的文件夹路径无效，请检查后重试。")
        return

    all_dataframes = []
    skipped_files = []
    column_headers = None

    # 遍历文件夹中的文件
    for filename in os.listdir(input_folder):
        if filename.endswith(".xlsx"):
            filepath = os.path.join(input_folder, filename)
            try:
                # 读取Excel文件
                df = pd.read_excel(filepath, engine='openpyxl')

                # 获取当前文件的列名
                current_columns = list(df.columns)

                # 检查列头是否一致
                if column_headers is None:
                    column_headers = current_columns
                elif column_headers != current_columns:
                    skipped_files.append(filename)
                    continue  # 如果列头不一致，则跳过该文件

                # 添加符合要求的文件内容
                all_dataframes.append(df)
            except Exception as e:
                print(f"无法处理文件 {filename}: {e}")
                skipped_files.append(filename)

    # 合并所有有效的DataFrame
    if all_dataframes:
        merged_df = pd.concat(all_dataframes, ignore_index=True)
        output_file = os.path.join(input_folder, "merged_file.xlsx")
        # 导出结果到文件
        merged_df.to_excel(output_file, index=False, engine='openpyxl')
        print(f"合并完成，结果已保存到 {output_file}")
        return True
    else:
        print("没有找到可以合并的文件。")


    # 打印跳过的文件
    if skipped_files:
        print("以下文件的表头不一致，被跳过: ")
        for file in skipped_files:
            print(f"- {file}")


# 示例使用
if __name__ == "__main__":
    input_folder = ""
    run(input_folder)
