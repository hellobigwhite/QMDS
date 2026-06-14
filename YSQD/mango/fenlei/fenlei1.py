import pandas as pd
import os
import random
from rapidfuzz import process, fuzz

# ==========================
# 配置部分（已替换为你的路径）
# ==========================
input_folder = r"C:\Users\Administrator\Desktop\主分类分类\输入"  # 商品 Excel 文件夹
taxonomy_file = r"C:\Users\Administrator\Desktop\主分类分类\谷歌文档\google_taxonomy.txt"  # 谷歌分类原始文档（带 ID）
output_dir = r"C:\Users\Administrator\Desktop\主分类分类\输出"  # 输出文件夹
MATCH_THRESHOLD = 80  # 模糊匹配阈值（百分比）
# ==========================

# 创建输出文件夹
os.makedirs(output_dir, exist_ok=True)

# 1. 读取并处理谷歌分类文档（带 ID）
google_categories = []
with open(taxonomy_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        # 分割 ID 和路径
        parts = line.split(' - ', 1)
        if len(parts) == 2:
            category_path = parts[1].strip()
            google_categories.append(category_path)

# 2. 准备顶级类目字典
top_category_dict = {}
other_items = []

# 3. 遍历文件夹里的所有 Excel 文件
for file_name in os.listdir(input_folder):
    if file_name.endswith('.xlsx'):
        file_path = os.path.join(input_folder, file_name)
        df = pd.read_excel(file_path)

        for _, row in df.iterrows():
            product_category = str(row.get('分类', '')).strip()

            if product_category:
                # 模糊匹配谷歌分类
                match, score, _ = process.extractOne(
                    product_category, google_categories, scorer=fuzz.token_sort_ratio
                )
                if score >= MATCH_THRESHOLD:
                    final_category = match
                else:
                    final_category = 'other'
            else:
                final_category = 'other'

            # 提取顶级类目
            top_category = final_category.split('>')[0].strip() if final_category != 'other' else 'other'

            if top_category == 'other':
                other_items.append(row)
            else:
                if top_category not in top_category_dict:
                    top_category_dict[top_category] = []
                top_category_dict[top_category].append(row)

# 4. 随机分配 other 商品
existing_categories = list(top_category_dict.keys())
for row in other_items:
    random_category = random.choice(existing_categories)
    top_category_dict[random_category].append(row)

# 5. 批量写入 Excel 文件，并在文件名显示商品数量
for top_category, items in top_category_dict.items():
    df_out = pd.DataFrame(items)
    count = len(df_out)
    # 文件名安全处理
    safe_category_name = "".join(c for c in top_category if c not in r'\/:*?"<>|')
    category_file = os.path.join(output_dir, f"{safe_category_name}_{count}.xlsx")
    df_out.to_excel(category_file, index=False)

print("所有商品分类完成！已生成按顶级类目分类的 Excel 文件，文件名显示商品数量。")
