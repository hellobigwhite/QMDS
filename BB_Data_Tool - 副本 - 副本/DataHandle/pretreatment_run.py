import json
from DataHandle import pretreatment_main
from pathlib import Path

def print_header():
    """
    打印命令行界面的固定头部说明
    """
    red_text = "\033[31m4. [变体处理模式说明]： 如果变体情况复杂，建议先选择不处理变体，然后手动处理变体\033[0m"  # 让文本变红色
    header = f"""
======================================================
通用数据清洗工具
======================================================

使用说明：

1. 请输入目标文件路径，该工具将对 xlsx 文件进行数据清洗。
2. 你可以输入一个 .txt 文件，其中每行包含一个 xlsx 文件路径。
3. 文件表头必须是 ['SKU', '标题', '描述', '子描述', '图片', '原价', '折扣价', '变体名', '变体值', '分类']。

变体提示： 变体只要采集一个变体名和变体值即可。比如服装，因为只用一张图。不需要颜色的属性。只要必要的尺码属性！！
分类提示： 不要太多分类 最多3级 即：AAA|||BBB|||CCC 

{red_text}

   - 模式1 ：当变体名和变体值内容同时存在时，组合为"清洗后的变体名^清洗后的变体值"格式；
             仅变体值存在时，其他情况返回空值  
             采集数据示例[火车头循环匹配]： 变体名= size 变体值= ###XL###XXXL###XXXXL  处理结果是： size^XL#XXXL#XXXXL

   - 模式2 ：注意：此模式仅适用[一键SP采集工具]！!!

   - 模式3 ：不处理变体，保留原始变体名和变体值到新列，cf_opingts设为空，用于后续手动处理

5. 清洗完成后，数据将保存到输入文件路径同目录，生成文件xxx_clean.xlsx 文件

后续更新对变体名和变体值以及分类的转义符处理
======================================================
"""
    print(header)

def load_categories():
    """
    从 JSON 文件加载分类选项
    """
    categories_file = Path("配置\\主分类配置.json")
    if not categories_file.exists():
        print("分类文件不存在，请检查路径或创建文件。")
        return []
    
    with open(categories_file, "r", encoding="utf-8") as file:
        return json.load(file)

def get_custom_category():
    """
    让用户选择自定义分类
    """
    categories = load_categories()
    if not categories:
        print("分类数据为空，无法选择分类。")
        return ""
    
    print("请选择自定义分类（请输入对应的数字）：")
    for idx, category in enumerate(categories, start=1):
        print(f"{idx}. {category}")
    
    while True:
        try:
            choice = int(input("请输入选项对应的数字: "))
            if 1 <= choice <= len(categories):
                return categories[choice - 1]
            else:
                print("输入无效，请重新选择。")
        except ValueError:
            print("请输入一个数字。")

def get_file_list(input_path):
    """
    处理输入路径，支持 .xlsx 文件或包含多个路径的 .txt 文件
    """
    path = Path(input_path)
    if path.suffix == ".txt":
        if not path.exists():
            print("TXT 文件不存在，请检查路径！")
            return []
        with open(path, "r", encoding="utf-8") as file:
            file_list = [line.strip().strip('"') for line in file.readlines() if line.strip()]
        return file_list
    elif path.suffix == ".xlsx":
        return [str(path)]
    else:
        print("不支持的文件格式，请输入 .xlsx 或 .txt 文件路径！")
        return []

def run():
    print_header()

    # 获取用户输入的文件路径（单个 xlsx 或 txt 文件）
    print("-" * 50)
    input_path = input("请输入目标文件路径或包含文件路径的txt文件[可直接拖入]: ")

    # 解析输入路径，获取文件列表
    file_list = get_file_list(input_path)
    if not file_list:
        print("未找到有效的文件，请检查输入路径！")
        return

    print("-" * 50)
    # 获取用户输入的 Categories 列的空白填充内容
    custom_category = input("Categories 列的空白内容填充（英文）: ")

    print("-" * 50)
    # 获取用户输入的自定义分类名称（中文）
    category_name = get_custom_category()
    print(f"您选择的自定义分类是：{category_name}")

    print("-" * 50)
    # 获取用户输入的站点名称
    site_name = input("请输入站点名称[用于清理]: ")

    print("-" * 50)
    # 获取用户输入的站点域名
    domain = input("请输入站点域名[用于清理和填充来源站]: ")

    print("-" * 50)
    # 获取用户输入的分布网站识别标识，默认为 0
    site_identifier_input = input("请输入分布网站识别标识（默认为0，直接回车使用默认值）: ")
    site_identifier = int(site_identifier_input) if site_identifier_input else 0

    print("-" * 50)
    # 获取用户输入的语言代码，默认为 'en'
    language_input = input("请输入语言代码（默认为'en'，直接回车使用默认值）: ")
    language = language_input if language_input else 'en'

    print("-" * 50)
    # 获取用户选择的变体处理方式，默认值 3
    print("\n请选择变体处理方式（输入对应的数字，默认为3）：")
    print("1. 组合清洗后的变体名和变体值")
    print("2. 提取前两个用|连接的变体值（适用于SP采集工具）")
    print("3. 保留原始变体名和变体值（默认）")

    variant_mode_input = input("请输入变体处理模式（1/2/3）：")
    process_variants = int(variant_mode_input) if variant_mode_input in ["1", "2"] else 3

    print("-" * 50)
    # 处理所有文件
    for file in file_list:
        print(f"正在处理文件: {file} ...")
        pretreatment_main.run(file, custom_category, category_name, site_name, domain, process_variants, site_identifier, language)

    print("-" * 50)
    print("所有文件的数据清洗并保存完成。")

if __name__ == "__main__":
    run()

