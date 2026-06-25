"""

"""
import pandas as pd
from pathlib import Path
import random
import string
import re
from bs4 import BeautifulSoup
import warnings
import traceback
from urllib.parse import urljoin
from urllib.parse import urlparse
from w3lib.html import replace_entities
from w3lib.encoding import html_to_unicode
from tqdm import tqdm
import unicodedata

# Excel 写入前兜底清洗：移除 openpyxl 不允许写入工作表的控制字符
ILLEGAL_CHARACTERS_RE = re.compile(r'[\000-\010]|[\013-\014]|[\016-\037]')


def clean_excel_illegal_chars(value):
    """
    清理 Excel/openpyxl 不允许写入的非法控制字符。
    例如商品标题中隐藏的 \x05 会导致 IllegalCharacterError。
    """
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub('', value)
        value = value.replace('\ufffd', '')
    return value


# SKU处理类
class SKUGenerator:
    def __init__(self, reference_sku):
        # 提取参考SKU的前缀和数字部分
        match = re.match(r'([a-zA-Z]+)(\d+)', reference_sku)
        if match:
            self.prefix = match.group(1)  # 字母前缀
            self.current_num = int(match.group(2))  # 数字部分
        else:
            raise ValueError("参考SKU格式无效，请确保格式为字母+数字（例如AWD324324543）")

    def generate_sku(self):
        # 生成并递增SKU
        sku = f"{self.prefix}{self.current_num}"
        self.current_num += 1  # 每次调用后递增
        return sku

    def reset_counter(self, reference_sku):
        # 如果需要重新设置计数器，可以使用此方法
        match = re.match(r'([a-zA-Z]+)(\d+)', reference_sku)
        if match:
            self.prefix = match.group(1)
            self.current_num = int(match.group(2))


def contain_black_list(text, blacklist):
    text = text.lower()
    return any(keyword.lower() in text for keyword in blacklist)


def clean_site_name(text, name):
    """
    处理文本中的站点名称。
    :param text: 要处理的文本（标题或描述）
    :param name: 站点名称（例如 example）
    :return: 处理后的文本
    """
    brands = ['Hermes', 'Chanel', 'Givenchy', 'Prada', 'Gucci', ' LV ', 'YSL', 'Delvaux', 'Marni', 'Melberry', 'Dior',
              'Chloe', 'Loewe', 'Fendi', 'Proenza', 'McQueen', 'Vetements', 'Balenciaga', 'MOSCHINO', 'Issey Miyake',
              'Canada Goose', 'Celine', 'KENZO', 'COMME DES GAR?ONS', 'Supreme', 'Phillip Lim', 'Y-3', 'Thom Browne',
              'Coach', 'Michael Kors', 'Kate Spade', 'Under Armour', 'Tory Burch', 'Marc Jacobs', 'Armani Exchange',
              'Nike', 'Adidas', 'Louis Vuitton', 'Patek Philippe', 'Audermars Piguet', 'Vacheron Constantin',
              'Vacherron Constantin', 'A. Lange&Sohne', 'Breguet', 'Roger Dubius', 'Parmigiani', 'Blancpain',
              'Ulysse Nardin', 'Franck Muller', 'Glashutte Original', 'Gurard-Perregaux', 'Rolex', 'IWC',
              'Jaeger-LeCoultre', 'Cartier', 'Chopard', 'Piaget', 'OMEGA', 'Chrond', 'Corum', 'Zenith', 'Movado',
              'Longiness', 'Tissot', 'Seiko', 'Citizen', 'Casio', 'Bulova', 'Swatch', 'Coach', 'Michael Kors', 'Lego',
              'Armani Exchange', 'Daneil Wellington', 'lego', 'disney', 'nintendo', 'LEGO', 'DISNEY', 'Disney',
              'Hello Kitty',
              'Cannabis', 'Marijuana', 'Roach clip', 'Hydroponic', 'Indica', 'Sativa', ' strain ', 'Medical',
              'Louis Vuitton', 'Gucci', 'Chanel', 'Hermès', 'Hermes', 'Prada', 'Fendi', 'Céline', 'Celine',
              'Balenciaga', 'Bvlgari', 'Miu Miu',
              'Christian Dior', 'Givenchy', 'Loewe', 'Saint Laurent', 'Chloé', 'Chloe', 'Bottega Veneta', 'Valentino',
              'Alexander McQueen',
              'Moncler', 'Lacoste', 'Coach', 'Kate Spade', 'Ralph Lauren', 'Hugo Boss', 'Off-White', 'The Row',
              'Acne Studios',
              'Jil Sander', 'Dries Van Noten', 'Furla', 'Rimowa', 'Goyard', 'Sophie Hulme', 'Marc Jacobs', "Tod's",
              'Brunello Cucinelli',
              'Philipp Plein', 'Balmain', 'Marni', 'Stella McCartney', 'Isabel Marant', 'Kenzo', 'Comme des Garçons',
              'Rei Kawakubo']

    if contain_black_list(text, brands):
        return ''

    if isinstance(text, str) and name:  # 确保 text 是字符串类型且有站点名称
        name_pattern = rf"\b{name}\b"
        text = re.sub(name_pattern, '', text, flags=re.IGNORECASE)
    elif isinstance(text, (int, float)):
        text = str(text)  # 转换为字符串
    return text


def clean_site_domain(text, domain):
    """
    处理文本中的站点域名。
    :param text: 要处理的文本（标题或描述）
    :param domain: 站点的域名（例如 xxx.xxx）
    :return: 处理后的文本
    """
    if isinstance(text, str) and domain:  # 确保 text 是字符串类型且有域名
        # 生成可能的站点域名格式（针对域名的不同形式）
        domain_patterns = [
            re.escape(domain),  # xxx.xxx
            f"www.{re.escape(domain)}",  # www.xxx.xxx
            f"http://{re.escape(domain)}",  # http://xxx.xxx
            f"https://{re.escape(domain)}",  # https://xxx.xxx
        ]
        # 去除域名格式
        for pattern in domain_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text


def clean_cf_name(variant_name):
    """
    清理变体名，将其规范化为清晰的格式，统一为首字母大写的格式。

    参数:
        variant_name: 原始的变体名内容。

    返回:
        清理后的变体名字符串。
    """
    if not isinstance(variant_name, str):
        return ""  # 如果不是字符串类型，返回空字符串

    # 去掉多余的空格，并确保变体名以正常的格式出现
    variant_name = variant_name.strip()

    # 转换为首字母大写，其余小写
    return variant_name.capitalize()


def clean_cf_opingts(content):
    """
    清理变体值，将其规范为 '###' 分隔的格式。

    参数:
        content: 原始的变体值内容。

    返回:
        清理后的变体值字符串，多个内容用 '###' 分隔，格式符合要求。
    """
    if not isinstance(content, str):
        return ""  # 如果不是字符串类型，返回空字符串

    # 如果内容以 '#' 或 '###' 开头，去掉开头的所有 '#'
    content = content.lstrip('#')

    # 将连续的多个 '#' 替换为 '#'
    content = re.sub(r'#+', '#', content)

    return content.strip()


def clean_images(image_urls):
    """
    清理图片链接，仅对链接进行前后空白符的处理。

    参数:
        image_urls: 图片链接字符串，可能包含多个链接。

    返回:
        处理后的图片链接字符串，仅保留第一个链接，且去除前后空白符。
    """
    # 确保 image_urls 是字符串类型，且为非空
    if not isinstance(image_urls, str):
        return ''  # 如果不是字符串类型，则返回空字符串

    # 处理多个图片链接，保留第一个
    image_list = image_urls.split(',') if ',' in image_urls else [image_urls]

    # 清除第一个图片链接的前后空白符
    first_image = image_list[0].strip() if image_list else ''

    return first_image


def clean_price(value):
    """
    清理价格字段，仅保留数字和小数点。
    参数:
        value (str|float|int): 价格的原始值。
    返回:
        float: 转换后的价格值（保留两位小数），解析失败返回 0.0
    """
    if pd.isna(value) or value == '':
        return 0.0

    if isinstance(value, (int, float)):
        return round(float(value), 2)

    if isinstance(value, str):
        # 替换逗号为小数点 + 常见符号
        value = value.replace(',', '.').replace("'", ".").replace('￥', '').replace('$', '')
        value = value.replace('元', '').replace('€', '').replace('£', '')
        # 只保留数字和小数点
        value = ''.join(re.findall(r'[0-9.]', value))

    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return 0.0


def clean_description(description):
    """
    清理 Description 列内容，保留指定 HTML 标签，并移除多余属性和不需要的内容。

    参数:
        description (str): 待处理的描述内容。

    返回:
        str: 清理后的描述内容。
    """
    if not isinstance(description, str) or description.strip() == "":
        return description

    # 定义需要替换的转义字符
    replacements = {
        r'&lt;': '<',
        r'&gt;': '>',
        r'&amp;': '&',
        r'&quot;': '"',
        r'&apos;': "'",
        r'\/': '/',
    }
    for entity, char in replacements.items():
        description = description.replace(entity, char)

    # 允许保留的 HTML 标签
    allowed_tags = [
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'ol', 'li',
        'strong', 'b', 'em', 'i', 'blockquote', 'span', 'br',
        'table', 'tr', 'td', 'th'
    ]

    # 删除 HTML 注释和 <style> 标签内容
    description = re.sub(r'<!--.*?-->', '', description, flags=re.DOTALL)
    description = re.sub(r'<style>.*?</style>', '', description, flags=re.DOTALL)

    # 捕获并打印所有警告
    with warnings.catch_warnings(record=True) as W:
        warnings.simplefilter("always", category=UserWarning)

        try:
            # 使用 BeautifulSoup 解析 HTML
            soup = BeautifulSoup(description, 'lxml')

            # 移除 <a> 标签并保留内容
            for a in soup.find_all('a'):
                a.unwrap()

            # 移除不需要的标签及属性
            for tag in soup.find_all(True):
                tag.attrs = {}  # 清空标签的所有属性
                if tag.name not in allowed_tags:
                    tag.unwrap()  # 移除不在允许列表中的标签

            # 返回处理后的 HTML 内容，移除多余空白
            return str(soup).strip()

        except Exception as e:
            # 捕获并打印异常的详细信息
            print(f"解析描述时发生异常: {e}")
            traceback.print_exc()
            return description  # 如果解析失败，返回原始描述

        # 打印所有捕获到的警告信息
        # for warn in W:
        #     print(f"警告: {warn.message}")


def clean_name(name):
    # 确保输入是字符串类型
    if not isinstance(name, str):
        return name  # 如果不是字符串，直接返回原值

    name = name.replace('  ', ' ').replace(',', '').replace('，', '')
    name = re.sub(r'\s+', ' ', name)

    # 判断是否是HTML内容，若是，则清除HTML标签
    if re.search(r'<.*?>', name):
        soup = BeautifulSoup(name, "html.parser")
        return soup.get_text()
    return name


def clean_text(text):
    """
    清理文本，移除不可见字符、非法字符、转义符、汉字，保留字母、数字和常用标点符号。

    参数:
        text (str): 待清理的文本。

    返回:
        str: 清理后的文本。
    """
    if not isinstance(text, str):
        return text

    # print(text)

    # 先尝试转换转义字符为正常字符 将Unicode转义字符（\uXXXX）转为对应的字符
    if isinstance(text, str):
        try:
            # 尝试处理转义字符
            text = text.encode('utf-8').decode('unicode_escape')
        except UnicodeDecodeError:
            # 处理失败继续下面的处理
            pass

    # 定义允许的字符：字母（大小写）、数字和常用标点符号
    allowed_chars_pattern = r'[^a-zA-Z0-9!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~]'

    # 移除控制字符 (ASCII 和 Unicode 控制字符)
    text = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', text)

    # 移除 Unicode 转义字符 (\uXXXX 格式)
    text = re.sub(r'\\u[0-9a-fA-F]{4}', '', text)

    # 移除汉字（Unicode 范围 4E00-9FFF）
    text = re.sub(r'[\u4e00-\u9fa5]', '', text)

    # 移除所有不在允许字符集中的字符，包括特殊符号如✘、♥等
    text = re.sub(r'[^\x00-\x7F]+', '', text)  # 移除非 ASCII 字符

    # 替换掉不在允许字符集中的字符
    text = re.sub(allowed_chars_pattern, '', text)

    # 去除前后空白
    text = text.strip()

    return text


# 新增w3lib库处理转义符和网页编码问题
def process_html_content(html_content):
    if pd.isna(html_content):
        return html_content
    # 将 HTML 实体转换为对应的字符
    content_with_entities_replaced = replace_entities(html_content)
    # 将 HTML 内容转换为 Unicode
    _, unicode_content = html_to_unicode(None, content_with_entities_replaced.encode('utf-8'))
    return unicode_content


# ---------------------------------------------------------------------------------------
def process_table(file_path, categories=None, custom_category=None, site_domain=None, site_name=None,
                  reference_sku="ABC123456789", process_variants=3, site_identifier=0, language="en"):
    """
    处理表格数据，并输出清洗后的结果到Excel文件中。
    改进点：在各个主要步骤间增加了全局进度条，便于跟踪数据处理进度。
    """
    # 设置总步数（共22个主要步骤，可根据需要调整）
    total_steps = 21
    pbar = tqdm(total=total_steps, desc="总体处理进度")

    # 1. 读取数据表格
    df = pd.read_excel(file_path)
    pbar.update(1)

    # 2. 初始化SKU生成器
    sku_generator = SKUGenerator(reference_sku)
    pbar.update(1)

    # 3. 清洗全表的文本数据
    df = df.apply(
        lambda col: col.map(lambda x: clean_text(x) if isinstance(x, str) else x)
        if col.dtype == 'object' else col
    )
    pbar.update(1)

    # 4. 新建一个空的DataFrame，按照要求的列名称
    processed_df = pd.DataFrame(
        columns=['SKU', 'Name', 'Description', 'Regular price', 'Categories', 'Images',
                 'cf_opingts', '自定义分类', '原站域名']
    )
    pbar.update(1)

    # 5. 对SKU为空值的行生成递增的SKU
    def fill_sku(row):
        if pd.isna(row['SKU']):
            return sku_generator.generate_sku()  # 使用生成器生成递增的SKU
        return row['SKU']

    df['SKU'] = df.apply(fill_sku, axis=1)
    pbar.update(1)

    # 6. 处理 Name 列：调用 clean_name 清洗并删除反斜杠
    processed_df['SKU'] = df['SKU']  # 保留原SKU
    # 直接清洗
    processed_df['Name'] = df['标题'].apply(clean_name)
    # 转为字符串后删除反斜杠（兼容可能为 int 的情况）
    processed_df['Name'] = df['标题'].apply(
        lambda x: str(clean_name(x)).replace('\\', '') if pd.notna(x) else x
    )
    pbar.update(1)

    # 7. 合并描述和子描述，分别调用清洗函数处理html
    processed_df['Description'] = df.apply(
        lambda row: (
            str(clean_description(row['描述'])) + '<br>' + str(clean_description(row['子描述']))
            if pd.notna(row['描述']) and pd.notna(row['子描述'])
            else str(clean_description(row['描述']) if pd.notna(row['描述']) else '') +
                 str(clean_description(row['子描述']) if pd.notna(row['子描述']) else '')
        ),
        axis=1
    )
    pbar.update(1)

    # 8. 处理标题和描述中的站点名称
    if site_name:
        processed_df['Name'] = processed_df['Name'].apply(
            lambda x: clean_site_name(x, site_name) if pd.notna(x) else x
        )
        processed_df['Description'] = processed_df['Description'].apply(
            lambda x: clean_site_name(x, site_name) if pd.notna(x) else x
        )
    pbar.update(1)

    # 9. 处理标题和描述中的站点域名
    if site_domain:
        processed_df['Name'] = processed_df['Name'].apply(
            lambda x: clean_site_domain(x, site_domain) if pd.notna(x) else x
        )
        processed_df['Description'] = processed_df['Description'].apply(
            lambda x: clean_site_domain(x, site_domain) if pd.notna(x) else x
        )
    pbar.update(1)

    # 10. 处理价格：取原价与折扣价中的较大值
    processed_df['Regular price'] = df.apply(
        lambda row: max(
            clean_price(row['原价']) or 0.0,
            clean_price(row['折扣价']) or 0.0
        ),
        axis=1
    )
    pbar.update(1)

    # 11. 处理 Categories 列：保留原始分类数据，若为空则填充用户输入的分类
    processed_df['Categories'] = df['分类'].apply(
        lambda x: x if pd.notna(x) and x != '' else (categories if categories else None)
    )
    pbar.update(1)

    # 12. 使用 clean_images 函数处理图片链接
    processed_df['Images'] = df['图片'].apply(lambda x: clean_images(x))
    pbar.update(1)

    # 13. 处理变体名和变体值
    if process_variants == 1:
        if '变体名' in df.columns and '变体值' in df.columns:
            processed_df['cf_opingts'] = df.apply(
                lambda row: (
                    f"{clean_cf_name(row['变体名'])}^{clean_cf_opingts(row['变体值'])}"
                    if pd.notna(row['变体名']) and pd.notna(row['变体值'])
                    else ""
                ),
                axis=1
            )
        else:
            print("提示：找不到'变体名'或'变体值'列，无法处理变体数据")
    elif process_variants == 2:
        if '变体' in df.columns:
            processed_df['cf_opingts'] = df['变体'].apply(
                lambda x: '|||'.join(x.split('|||')[:2])
                if isinstance(x, str) and '|||' in x else x
            )
        else:
            print("提示：找不到'变体'列，无法处理变体数据")
    else:
        # 此处只保留原始数据，不对缺失列赋值
        if '变体名' in df.columns:
            processed_df['原变体名'] = df['变体名']
        else:
            print("提示：找不到'变体名'列，无法保留原始变体名")
        if '变体值' in df.columns:
            processed_df['原变体值'] = df['变体值']
        else:
            print("提示：找不到'变体值'列，无法保留原始变体值")
    pbar.update(1)

    # 14. 处理自定义分类和原站域名
    processed_df['自定义分类'] = custom_category if custom_category else None
    processed_df['原站域名'] = site_domain if site_domain else None
    pbar.update(1)

    # 15. 删除 Name 列为空的行（将空字符串或空格转为 NaN 后删除）
    processed_df['Name'] = processed_df['Name'].replace(r'^\s*$', pd.NA, regex=True)
    processed_df.dropna(subset=['Name'], how='all', inplace=True)
    pbar.update(1)

    # 16. 删除 Regular price 列为空的行，并删除价格为 0 的数据（先转换为数值类型）
    processed_df['Regular price'] = pd.to_numeric(processed_df['Regular price'], errors='coerce')
    processed_df.dropna(subset=['Regular price'], how='all', inplace=True)
    processed_df = processed_df[processed_df['Regular price'] > 0]
    pbar.update(1)

    # 17. 删除 Images 列为空的行（将空字符串或空格转为 NaN 后删除）
    processed_df['Images'] = processed_df['Images'].replace(r'^\s*$', pd.NA, regex=True)
    processed_df.dropna(subset=['Images'], how='all', inplace=True)
    pbar.update(1)

    # 18. 清除前后空白字符
    processed_df = processed_df.apply(
        lambda col: col.str.strip() if col.dtype == 'object' else col
    )
    pbar.update(1)

    # 19. 使用 w3lib 库处理转义符和网页编码问题（针对 Name 和 Description 列）
    processed_df['Name'] = processed_df['Name'].apply(process_html_content)
    processed_df['Description'] = processed_df['Description'].apply(process_html_content)
    processed_df['Categories'] = processed_df['Categories'].apply(process_html_content)
    processed_df['cf_opingts'] = processed_df['cf_opingts'].apply(process_html_content)
    pbar.update(1)

    # 20. 新增“分布网站识别”和“语言”两个列
    processed_df['分布网站识别'] = site_identifier
    processed_df['语言'] = language
    pbar.update(1)

    # 22. 保存处理后的结果到新Excel文件
    # 写入 Excel 前再次兜底清洗，防止 HTML 解析、转义处理后重新带入非法控制字符
    try:
        processed_df = processed_df.map(clean_excel_illegal_chars)
    except AttributeError:
        # 兼容旧版 pandas
        processed_df = processed_df.applymap(clean_excel_illegal_chars)

    output_file = file_path.parent / f"{file_path.stem}_clean.xlsx"
    processed_df.to_excel(output_file, index=False)
    pbar.update(1)

    pbar.close()
    print(f"处理完成，保存到: {output_file}")
    return True


#
def process_single_file(file_path, **kwargs):
    """
    处理单个文件，.xlsx文件。
    :param file_path: 单个文件路径
    :param default_table_name: 默认尝试读取的表名
    """
    file_path = Path(file_path)
    if not file_path.exists():
        print("输入路径无效，请输入有效的文件路径。")
        return

    if file_path.suffix == ".xlsx":
        print(f"正在处理 Excel 文件: {file_path}")
        process_table(file_path, **kwargs)
    else:
        print("输入文件类型不支持，请输入 .xlsx文件路径。")


def generate_reference_sku():
    """
    生成开头随机1-4位的大写字母+拼接随机8-12位的随机数字
    """
    prefix = ''.join(random.choices(string.ascii_uppercase, k=random.randint(1, 4)))
    suffix = ''.join(random.choices(string.digits, k=random.randint(8, 12)))
    return prefix + suffix


# --------------------------------------------------------------
def run(input_path, custom_category, category_name, site_name, domain, process_variants, site_identifier=0,
        language='en'):
    """
    数据清洗入口主函数，负责启动数据清洗流程，检查输入路径，生成参考 SKU，并调用数据处理函数。

    :param input_path: 输入文件路径
    :param custom_category: 自定义分类，用于填充 Categories 列的空白内容(英文)。
    :param category_name: 自定义分类名称，用于填充自定义分类列的空白内容（中文）。
    :param site_name: 站点名称，用于数据清洗时的站点信息填充。
    :param domain: 站点域名，用于数据清洗时的站点信息填充。
    :param site_identifier: 分布网站识别标识，默认为 0。
    :param language: 语言代码，默认为 'en'。
    """
    print(f"开始清洗数据")

    # 将 input_path 转换为 Path 对象，方便后续路径操作
    input_path = Path(input_path)

    # 检查路径是否存在，如果不存在则终止流程并返回 False
    if not input_path.exists():
        print("路径不存在")
        return False

    print(f"路径存在: {input_path}")

    # 调用新增的函数生成参考 SKU，用于后续数据处理中的 SKU 填充
    reference_sku = generate_reference_sku()
    print(f"生成的参考 SKU: {reference_sku}，处理文件: {input_path}")

    # 调用数据处理主函数，传入必要的参数，完成数据清洗
    process_single_file(
        input_path,
        categories=custom_category,  # Categories 列空白内容填充(英文)
        custom_category=category_name,  # 自定义分类列空白内容填充（中文）
        site_domain=domain,  # 站点域名
        site_name=site_name,  # 站点名称
        reference_sku=reference_sku,  # SKU 起始编号，用于填充空白列
        process_variants=process_variants,  # True=处理变体，False=不处理变体，"auto"=自动检测变体列
        site_identifier=site_identifier,  # 分布网站识别标识
        language=language  # 语言代码
    )

    print("清洗流程结束")
    return True
