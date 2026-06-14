import sys
import os
import traceback

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_category import AutoCategoryConfigurator

def test_main_category_simple():
    """简单测试主分类设置"""
    try:
        # 手动输入测试数据
        domain = "shifnora.com"
        main_category = "Toys & Games|||Outdoor Play|||Trampolines"
        password = "f!XsS$J2WneOkMyUgQ"  # 默认密码
        
        print(f"测试域名: {domain}")
        print(f"主分类: {main_category}")
        print(f"密码: {'***' if password else '空'}")
        
        if not domain or not main_category or not password:
            print("错误: 测试数据不完整")
            return
        
        configurator = AutoCategoryConfigurator(password)
        
        # 测试主分类设置
        print("开始设置主分类...")
        result = configurator.set_main_category(domain, main_category)
        print(f"设置成功: {result}")
        
    except Exception as e:
        print(f"错误: {e}")
        print("详细错误:")
        traceback.print_exc()

if __name__ == "__main__":
    test_main_category_simple()