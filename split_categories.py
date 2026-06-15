import re
import os

def clean_filename(name):
    # 替换Windows文件名中的非法字符
    illegal_chars = r'[<>:"/\\|?*]'
    return re.sub(illegal_chars, '_', name)

def main():
    input_file = 'google_category.txt'
    if not os.path.exists(input_file):
        print(f"错误：找不到文件 {input_file}")
        return
    
    current_category = None
    current_file = None
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            # 跳过注释行
            if line.startswith('#'):
                continue
            # 删除开头的数字+" - "
            cleaned_line = re.sub(r'^\d+ - ', '', line)
            # 判断是否为一级分类（没有">"）
            if '>' not in cleaned_line:
                # 这是一级分类
                category_name = cleaned_line.strip()
                if not category_name:
                    continue
                # 清理文件名
                filename = clean_filename(category_name) + '.txt'
                # 关闭之前的文件
                if current_file:
                    current_file.close()
                # 打开新文件
                current_file = open(filename, 'w', encoding='utf-8')
                current_category = category_name
                # 写入当前行（已删除序号）
                current_file.write(cleaned_line + '\n')
            else:
                # 子分类行，写入当前文件
                if current_file:
                    current_file.write(cleaned_line + '\n')
    
    if current_file:
        current_file.close()
    print("拆分完成！")

if __name__ == '__main__':
    main()