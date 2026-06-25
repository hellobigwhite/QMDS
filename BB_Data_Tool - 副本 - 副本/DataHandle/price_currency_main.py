import pandas as pd
import os


# 先转换成美元，再转换成目标币种
def convert_price(df, currency_column, current_currency, target_currency, exchange_rates):
    if current_currency not in exchange_rates or target_currency not in exchange_rates:
        raise ValueError(f"未找到 {current_currency} 或 {target_currency} 的汇率信息")
    
    rate_from_usd = exchange_rates[current_currency]['exchange_rate_usd']
    rate_to_usd = exchange_rates[target_currency]['exchange_rate_usd']
    conversion_rate = rate_from_usd / rate_to_usd  # 修正后的公式

    nation = exchange_rates[target_currency]['nation']
    
    df[currency_column] = df[currency_column].apply(
        lambda x: adjust_price(round_price(x * conversion_rate))
    )
    
    df[currency_column] = df[currency_column].apply(
        lambda x: 0.01 if x == 0 else x
    )
    
    return df, nation

def round_price(price):
    """四舍五入到小数点后两位"""
    if pd.isna(price) or price < 0:
        return 0.0
    return round(price, 2)

def adjust_price(price):
    """电商价格优化策略"""
    if price < 0:
        return 0.0
    
    integer_part = int(price)
    decimal_part = price - integer_part
    
    if decimal_part >= 0.5:
        return integer_part + 0.99
    return integer_part + 0.00

def process_table(input_file, current_currency, target_currency, exchange_rates):
    # 读取数据文件
    try:
        df = pd.read_excel(input_file)
    except Exception as e:
        print(f"读取文件失败: {str(e)}")
        exit()
    
    # 自动检测价格列
    price_columns = ['价格', 'Regular price']
    found_columns = [col for col in price_columns if col in df.columns]
    
    if not found_columns:
        print("错误: 未找到有效的价格列（需要'价格'或'Regular price'）")
        exit()
    
    price_column = found_columns[0]
    
    # 执行价格转换
    try:
        df, nation = convert_price(df, price_column, current_currency, target_currency, exchange_rates)
    except Exception as e:
        print(f"价格转换失败: {str(e)}")
        exit()
    
    # 保存结果，文件名格式：原文件名_目标币种nation.xlsx
    output_file = os.path.join(
        os.path.dirname(input_file),
        f"{os.path.splitext(os.path.basename(input_file))[0]}_{nation}.xlsx"
    )
    
    try:
        df.to_excel(output_file, index=False)
        print(f"\n转换完成！文件已保存至: {output_file}")
    except Exception as e:
        print(f"文件保存失败: {str(e)}")

def run(input_file, current_currency, target_currency, exchange_rates):
    process_table(input_file, current_currency, target_currency, exchange_rates)
