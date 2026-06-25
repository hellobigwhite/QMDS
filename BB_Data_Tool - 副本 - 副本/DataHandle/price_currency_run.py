from DataHandle import price_currency_main
import os
import json

def load_exchange_rates():
    """
    本地加载汇率文件，并将列表转换为以币种代码为键的字典
    """
    exchange_rate_path = "配置\\币种汇率配置.json"
    if not os.path.exists(exchange_rate_path):
        print("错误：汇率文件不存在！")
        exit()
    
    try:
        with open(exchange_rate_path, "r", encoding="utf-8") as f:
            rates_list = json.load(f)
            rates = {item.get("nation"): item for item in rates_list if item.get("nation")}
            return rates
    except json.JSONDecodeError:
        print("错误：汇率文件格式不正确！")
        exit()
    except Exception as e:
        print(f"错误：读取汇率文件失败: {str(e)}")
        exit()

def print_header():
    """
    打印命令行界面的固定头部说明
    """
    header = f"""
======================================================
币种转换工具
======================================================

使用说明：
1. 输入 Excel 文件完整路径，或将 Excel 文件拖入窗口。
2. 输入 TXT 文件路径（如果需要批量处理，TXT 文件每行一个 Excel 文件路径）。
3. 按提示输入当前币种代码（例如：CNY、USD）。
4. 按提示输入目标币种代码（例如：CNY、USD）。

======================================================
    """
    print(header)

def print_available_currencies(exchange_rates):
    """
    打印可用的币种代码及对应名称
    """
    print("可用币种：")
    for code, info in exchange_rates.items():
        print(f"{code}: {info.get('name', '')}")
    print("")

def select_currency(prompt, exchange_rates):
    """
    根据提示获取币种代码，并校验输入是否合法
    """
    while True:
        currency = input(prompt).strip()
        if currency in exchange_rates:
            return currency
        else:
            print("输入的币种代码无效，请重新输入。")

def clean_file_path(file_path):
    """
    清理路径字符串，去除首尾多余的引号和空格
    """
    return file_path.strip().strip('"')

def get_file_list(file_input):
    """
    判断输入是单个 Excel 文件还是 TXT 文件
    - 如果是 Excel 文件路径，返回包含该文件的列表
    - 如果是 TXT 文件路径，读取所有行，并返回所有文件路径的列表
    """
    file_path = clean_file_path(file_input)
    
    if not os.path.exists(file_path):
        print(f"错误：文件 '{file_path}' 不存在！")
        exit()
    
    if file_path.lower().endswith(".txt"):
        with open(file_path, "r", encoding="utf-8") as f:
            file_list = [clean_file_path(line) for line in f.readlines() if line.strip()]
        if not file_list:
            print("错误：TXT 文件为空，请检查内容！")
            exit()
        return file_list
    
    elif file_path.lower().endswith((".xls", ".xlsx")):
        return [file_path]
    
    else:
        print("错误：请输入有效的 Excel 文件 (.xls/.xlsx) 或 TXT 文件 (.txt)！")
        exit()

def run():
    print_header()
    exchange_rates = load_exchange_rates()
    print_available_currencies(exchange_rates)

    file_input = input("请输入要处理的 Excel 或 TXT 文件路径[可直接拖入]：").strip('"')
    file_list = get_file_list(file_input)

    current_currency = select_currency("请输入当前币种代码（例如：CNY、USD）：", exchange_rates)
    target_currency = select_currency("请输入目标币种代码（例如：CNY、USD）：", exchange_rates)

    for file_path in file_list:
        print(f"处理文件：{file_path}")
        price_currency_main.run(file_path, current_currency, target_currency, exchange_rates)

if __name__ == '__main__':
    run()
