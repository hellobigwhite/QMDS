#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析所有Python文件的依赖关系
"""

import os
import re
import sys
from pathlib import Path

def extract_imports(file_path):
    """提取文件中的导入语句"""
    imports = set()
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # 匹配 import xxx
    pattern1 = re.compile(r'^import (\w+)', re.MULTILINE)
    matches1 = pattern1.findall(content)
    imports.update(matches1)
    
    # 匹配 from xxx import yyy
    pattern2 = re.compile(r'^from (\w+)', re.MULTILINE)
    matches2 = pattern2.findall(content)
    imports.update(matches2)
    
    return imports

def main():
    # 标准库模块列表（简化版）
    stdlib = {
        'os', 'sys', 're', 'json', 'time', 'random', 'datetime', 'logging',
        'subprocess', 'threading', 'queue', 'concurrent', 'functools',
        'pathlib', 'glob', 'signal', 'html', 'urllib3', 'urllib',
        'ast', 'io', 'collections', 'itertools', 'warnings', 'math'
    }
    
    # 第三方库依赖映射
    third_party = {
        'pymongo': 'pymongo',
        'flask': 'flask',
        'requests': 'requests',
        'playwright': 'playwright',
        'bs4': 'beautifulsoup4',
        'openpyxl': 'openpyxl',
        'pandas': 'pandas',
        'tqdm': 'tqdm',
        'numpy': 'numpy'
    }
    
    all_imports = set()
    files = list(Path('.').glob('*.py'))
    
    print("=== 分析依赖关系 ===\n")
    for file_path in files:
        if file_path.name == 'analyze_dependencies.py':
            continue
        try:
            imports = extract_imports(file_path)
            if imports:
                all_imports.update(imports)
                print(f"{file_path.name}: {', '.join(sorted(imports))}")
        except Exception as e:
            print(f"{file_path.name}: 分析失败 - {e}")
    
    print("\n=== 所有导入的模块 ===\n")
    print(sorted(all_imports))
    
    print("\n=== 可能需要安装的第三方库 ===\n")
    needs_install = []
    for imp in all_imports:
        if imp in stdlib:
            continue
        if imp in third_party:
            needs_install.append(third_party[imp])
        else:
            # 可能是相对导入或自定义模块
            if not Path(f'{imp}.py').exists() and not Path(imp).is_dir():
                needs_install.append(imp)
    
    print(f"需要安装: {', '.join(sorted(set(needs_install)))}")
    
    print("\n=== 安装命令 ===\n")
    print(f"pip install {' '.join(sorted(set(needs_install)))}")

if __name__ == "__main__":
    main()
