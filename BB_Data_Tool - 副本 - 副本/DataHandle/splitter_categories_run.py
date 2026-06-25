import pandas as pd
import numpy as np
import os
import warnings
import re
from difflib import SequenceMatcher


def split_table_by_category(input_file, num_parts, output_dir, shuffle, mode='normal', specified_categories_list=None, max_rows=5000):
    # 忽略 pandas 的 FutureWarning
    warnings.simplefilter(action='ignore', category=FutureWarning)

    # 读取原始表格
    try:
        df = pd.read_excel(input_file)
    except Exception as e:
        print(f"\u2718 错误：无法读取输入文件 {input_file}，错误信息：{str(e)}")
        return

    # 打印列名以便调试
    print(f"表格列名：{list(df.columns)}")

    # 检查是否存在"原站域名"列
    domain_column = '原站域名'
    if domain_column not in df.columns:
        possible_columns = [col for col in df.columns if '原站域名' in col]
        if possible_columns:
            print(f"\u2718 警告：未找到精确匹配的 '{domain_column}' 列，但找到类似列名：{possible_columns}")
            print("请确认正确列名并修改脚本中的 domain_column 变量。")
        else:
            print(f"\u2718 错误：表格中未找到 '{domain_column}' 列，请检查列名。")
        return

    # 检查"原站域名"列是否为空
    if df[domain_column].isna().all():
        print(f"\u2718 错误：'{domain_column}' 列所有值均为空，请检查输入数据。")
        return

    # 打印原始"原站域名"列和"Categories"列示例
    print(f"原始 '{domain_column}' 列示例：\n{df[domain_column].head().to_string()}")
    print(f"原始 'Categories' 列示例：\n{df['Categories'].head().to_string()}")

    # 创建输出目录
    base_name = os.path.splitext(os.path.basename(input_file))[0]
    output_subdir = os.path.join(output_dir, f"{base_name}_split")
    os.makedirs(output_subdir, exist_ok=True)

    # 1. 单独保存无分类（Categories 为 NaN）的行
    uncategorized = df[df['Categories'].isna()]
    if not uncategorized.empty:
        uncategorized = uncategorized.copy()
        uncategorized[domain_column] = uncategorized[domain_column].astype(str).str.strip() + '_uncategorized'
        unc_path = os.path.join(output_subdir, 'uncategorized.xlsx')
        uncategorized.to_excel(unc_path, index=False)
        print(f"\u2714 无分类数据已保存：{unc_path}，共 {len(uncategorized)} 行。")
        print(f"无分类数据修改后 '{domain_column}' 列示例：\n{uncategorized[domain_column].head().to_string()}")
    else:
        print("\u2714 无分类数据为空，跳过无分类数据操作。")

    # 2. 只保留有分类的行（原始数据池）
    categorized_pool = df[df['Categories'].notna()].copy()
    print(f"原始数据池（有分类数据）共 {len(categorized_pool)} 行。")
    # 打印分类分布
    print("分类分布：")
    for cat, count in categorized_pool['Categories'].value_counts().items():
        print(f"  {cat}: {count} 行")

    if mode == 'normal':
        # 原有逻辑：按 Categories 分组并均匀拆分
        grouped = categorized_pool.groupby('Categories')
        parts = [pd.DataFrame() for _ in range(num_parts)]

        for category, group in grouped:
            print(f"分类 '{category}' 共 {len(group)} 行。")
            if shuffle:
                group = group.sample(frac=1, random_state=42).reset_index(drop=True)
            else:
                group = group.reset_index(drop=True)
            splits = np.array_split(group, num_parts)
            for i, split_df in enumerate(splits):
                parts[i] = pd.concat([parts[i], split_df], ignore_index=True)

        # 保存每一份
        for i, part_df in enumerate(parts, start=1):
            if not part_df.empty:
                part_df = part_df.copy()
                part_df[domain_column] = part_df[domain_column].astype(str).str.strip() + f'_part{i}'
                out_path = os.path.join(output_subdir, f'{base_name}_part{i}.xlsx')
                part_df.to_excel(out_path, index=False)
                print(f"\u2714 第 {i} 份保存：{out_path}，共 {len(part_df)} 行。")
                print(f"第 {i} 份修改后 '{domain_column}' 列示例：\n{part_df[domain_column].head().to_string()}")
            else:
                print(f"\u2714 第 {i} 份为空，跳过保存。")

    elif mode == 'specified':
        # 指定分类不均分模式：每个分类单独作为一个任务
        if not specified_categories_list:
            print("\u2718 错误：在指定模式下，必须提供至少一个分类。")
            return

        # 遍历每个指定分类（每个分类一个任务）
        for task_idx, category in enumerate(specified_categories_list, start=1):
            # 清理分类名以用于文件名（替换非法字符）
            safe_category = re.sub(r'[^\w\-]', '_', category)
            print(f"\n--- 处理指定任务 {task_idx}：分类 '{category}' (安全文件名：{safe_category}) ---")

            # 检查分类名是否有效
            valid_categories = set(categorized_pool['Categories'].unique())
            if category not in valid_categories:
                print(f"\u2718 错误：分类 '{category}' 在数据中不存在，跳过此任务。")
                continue

            # 从原始数据池收集指定分类数据（独立副本）
            selected = categorized_pool[categorized_pool['Categories'] == category].copy()
            other = categorized_pool[categorized_pool['Categories'] != category].copy()
            total_selected = len(selected)
            print(f"任务 {task_idx} 指定分类 '{category}' 数据共 {total_selected} 行。")

            # 填充到5000行（如果需要）
            if total_selected < max_rows:
                need = max_rows - total_selected
                print(f"需要填充 {need} 行到指定分类数据。")
                if not other.empty:
                    other_grouped = other.groupby('Categories')
                    num_other_cats = len(other_grouped)
                    total_other_rows = len(other)
                    print(f"其他分类共 {num_other_cats} 个，数据量：{total_other_rows} 行。")
                    print("其他分类分布：")
                    for cat, count in other['Categories'].value_counts().items():
                        print(f"  {cat}: {count} 行")
                    if num_other_cats > 0:
                        # 优化填充：尽量使用所有可用数据
                        fill_df = pd.DataFrame()
                        remaining_need = need
                        for cat, group in other_grouped:
                            available = len(group)
                            take = min(remaining_need, available)
                            if take > 0:
                                print(f"从分类 '{cat}' 抽取 {take} 行（可用 {available} 行）。")
                                sampled = group.sample(n=take, random_state=42) if shuffle else group.head(take)
                                fill_df = pd.concat([fill_df, sampled], ignore_index=True)
                                remaining_need -= take
                            if remaining_need <= 0:
                                break
                        if len(fill_df) < need:
                            print(f"\u2718 警告：其他分类数据不足，仅填充 {len(fill_df)} 行，少于所需 {need} 行。")
                            print(f"建议：数据池总行数为 {len(categorized_pool)}，其他分类可用 {total_other_rows} 行，考虑增加数据或设置 max_rows <= {total_other_rows + total_selected}。")
                        selected = pd.concat([selected, fill_df], ignore_index=True)
                    else:
                        print("\u2718 警告：无其他分类，无法填充。")
                else:
                    print("\u2718 警告：无其他数据，无法填充。")
            else:
                print(f"指定分类数据已达或超过 {max_rows} 行，无需填充。")

            # 如果 shuffle，对 selected 打乱
            if shuffle:
                selected = selected.sample(frac=1, random_state=42).reset_index(drop=True)

            # 拆分指定分类数据：每份恰好5000行（除最后一份）
            total_now = len(selected)
            print(f"任务 {task_idx} 填充后指定分类数据共 {total_now} 行。")
            num_parts_task = (total_now + max_rows - 1) // max_rows
            parts = []
            for i in range(num_parts_task):
                start_idx = i * max_rows
                part_df = selected[start_idx:start_idx + max_rows]
                if not part_df.empty:
                    parts.append(part_df)

            # 保存指定分类的拆分部分，使用分类名
            for i, part_df in enumerate(parts, start=1):
                if not part_df.empty:
                    part_df = part_df.copy()
                    part_df[domain_column] = part_df[domain_column].astype(str).str.strip() + f'_specified_{safe_category}_part{i}'
                    out_path = os.path.join(output_subdir, f'{base_name}_specified_{safe_category}_part{i}.xlsx')
                    part_df.to_excel(out_path, index=False)
                    print(f"\u2714 任务 {task_idx} 指定第 {i} 份保存：{out_path}，共 {len(part_df)} 行。")
                    print(f"任务 {task_idx} 指定第 {i} 份修改后 '{domain_column}' 列示例：\n{part_df[domain_column].head().to_string()}")
                else:
                    print(f"\u2714 任务 {task_idx} 指定第 {i} 份为空，跳过保存。")

    elif mode == 'interval':
        # 间隔分类拆分模式：只提取指定的分类，每个分类单独保存为一个文件
        if not specified_categories_list:
            print("\u2718 错误：在间隔模式下，必须提供至少一个分类。")
            return

        # 遍历每个指定分类（每个分类单独一个文件）
        print(f"\n--- 提取指定分类数据 ---")
        print(f"指定的分类列表：{specified_categories_list}")

        # 检查分类名是否有效
        valid_categories = set(categorized_pool['Categories'].unique())
        invalid_categories = [cat for cat in specified_categories_list if cat not in valid_categories]
        if invalid_categories:
            print(f"\u2718 警告：以下分类在数据中不存在，将被跳过：{invalid_categories}")
        
        valid_specified_categories = [cat for cat in specified_categories_list if cat in valid_categories]
        if not valid_specified_categories:
            print("\u2718 错误：没有有效的分类可以提取。")
            return

        print(f"有效的分类：{valid_specified_categories}")

        # 逐个处理每个分类
        for task_idx, target_category in enumerate(valid_specified_categories, start=1):
            # 清理分类名以用于文件名
            safe_category = re.sub(r'[^\w\-]', '_', target_category)
            print(f"\n--- 处理分类 {task_idx}/{len(valid_specified_categories)}：{target_category} ---")

            # 提取该分类的数据
            part_data = categorized_pool[categorized_pool['Categories'] == target_category].copy()
            count = len(part_data)
            
            if part_data.empty:
                print(f"  分类 '{target_category}' 没有数据，跳过。")
                continue
            
            print(f"  数据量：{count} 行")

            # 打乱顺序（如果需要）
            if shuffle:
                part_data = part_data.sample(frac=1, random_state=42 + task_idx).reset_index(drop=True)
                print("  已打乱数据顺序。")

            # 保存文件
            part_data = part_data.copy()
            part_data[domain_column] = part_data[domain_column].astype(str).str.strip() + f'_interval_{safe_category}'
            out_path = os.path.join(output_subdir, f'{base_name}_interval_{safe_category}.xlsx')
            part_data.to_excel(out_path, index=False)
            print(f"  \u2714 保存：{out_path}，共 {len(part_data)} 行。")
            print(f"  修改后 '{domain_column}' 列示例：\n{part_data[domain_column].head().to_string()}")


if __name__ == "__main__":
    # 打印脚本说明头部
    print("=============================================")
    print("脚本：按 Categories 列拆分 Excel 表格")
    print("模式1：均匀拆分 - 按用户输入的份数均分有分类行")
    print("模式2：指定分类不均分 - 每个分类作为一个任务（从文本文件逐行读取分类名），独立处理（数据池不变），自动拆分（每份最多5000行），若不足5000从其他分类填充")
    print("模式3：间隔分类拆分 - 只提取指定的分类，每个分类单独保存")
    print("        - 从 categories.txt 中读取要提取的分类列表")
    print("        - 只保留这些指定分类的数据")
    print("        - 不添加任何其他分类的数据")
    print("        - 每个指定分类单独保存为一个文件")
    print("输出：uncategorized.xlsx + 原文件名_interval_{分类名}.xlsx（存放在以原文件名命名的子文件夹中）")
    print("功能：拆分后的文件中原站域名列会添加 _interval_{分类名} 后缀")
    print("注意：拆分后的数据表如果要直接上传总台，请在使用[常规拆分工具]过一遍，防止上传总台报表错的问题！！！")
    print("=============================================")

    input_file = input("请输入原始表格文件路径（例如：original_data.xlsx）：").strip()

    mode_input = input("选择模式：1（均匀拆分） 或 2（指定分类不均分） 或 3（间隔分类拆分） (默认：1)：").strip() or '1'
    mode = 'specified' if mode_input == '2' else 'interval' if mode_input == '3' else 'normal'

    specified_categories_list = None
    num_parts = None
    if mode == 'normal':
        try:
            num_parts = int(input("请输入要拆分的份数（例如：3）：").strip())
            if num_parts <= 0:
                raise ValueError("拆分份数必须为正整数")
        except ValueError as e:
            print(f"\u2718 错误：请输入有效的拆分份数（正整数），错误信息：{str(e)}")
            exit(1)
    else:
        categories_file = input("请输入分类名文本文件路径（例如：categories.txt，每行一个分类名）：").strip()
        try:
            with open(categories_file, 'r', encoding='utf-8') as f:
                specified_categories_list = [line.strip() for line in f if line.strip()]
            if not specified_categories_list:
                print("\u2718 错误：分类名文本文件为空或无有效分类名。")
                exit(1)
            print(f"从文件 {categories_file} 读取到 {len(specified_categories_list)} 个分类任务：{specified_categories_list}")
        except Exception as e:
            print(f"\u2718 错误：无法读取分类名文件 {categories_file}，错误信息：{str(e)}")
            exit(1)

    shuffle_input = input("是否打乱每个分类中数据的顺序？(y/n)：").strip().lower()
    shuffle = shuffle_input == 'y'

    output_dir = os.path.dirname(input_file) or '.'
    split_table_by_category(input_file, num_parts, output_dir, shuffle, mode, specified_categories_list)
