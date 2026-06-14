import os
import random
import sys
from collections import Counter

import pandas as pd

CATEGORY_POOL = [
    "Best Seller",
    "Featured",
    "Accessories",
    "Other",
    "New Arrival",
    "Exclusive",
    "Limited Edition",
    "Hot Sale",
    "Most Popular",
    "Trending",
    "Special Offer",
    "Flash Sale",
]

COL = "Categories"


def main():
    file_path = input("请输入 Excel 文件路径: ").strip().strip('"')
    if not os.path.isfile(file_path):
        print(f"错误：文件不存在 - {file_path}")
        sys.exit(1)

    min_input = input("请输入最小出现次数阈值（默认 10）: ").strip()
    try:
        threshold = int(min_input) if min_input else 10
    except ValueError:
        print("错误：阈值必须为整数")
        sys.exit(1)

    print(f"\n读取文件: {file_path}")
    df = pd.read_excel(file_path)

    if COL not in df.columns:
        print(f"错误：Excel 中未找到列 '{COL}'，可用列: {list(df.columns)}")
        sys.exit(1)

    freq = Counter(df[COL].dropna().astype(str))

    print(f"\nCategories 分类统计（共 {len(freq)} 种）:\n")
    print(f"{'分类值':<40} {'出现次数':>8}")
    print("-" * 50)
    for cat, cnt in freq.most_common():
        marker = "  <- 将替换" if cnt < threshold else ""
        print(f"{cat:<40} {cnt:>8}{marker}")

    rare = {cat for cat, cnt in freq.items() if cnt < threshold}
    if not rare:
        print(f"\n所有分类出现次数均 >= {threshold}，无需替换")
        return

    replaced_count = 0
    for idx in df.index:
        value = str(df.at[idx, COL])
        if value in rare:
            df.at[idx, COL] = random.choice(CATEGORY_POOL)
            replaced_count += 1

    df.to_excel(file_path, index=False)

    print(f"\n完成：共 {len(df)} 行，替换了 {replaced_count} 行")
    print(f"已覆盖原文件：{file_path}")


if __name__ == "__main__":
    main()
