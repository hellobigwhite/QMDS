import sys
import os

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_category import AutoCategoryConfigurator

def test_main_category():
    try:
        # 测试参数
        domain = "your-test-domain.com"  # 替换为实际测试域名
        main_category = "Clothing|||Men|||Shirts"  # 替换为实际的主分类
        password = "your-wp-password"  # 替换为实际的WP密码
        
        print(f"测试主分类设置: {domain}")
        print(f"主分类: {main_category}")
        
        configurator = AutoCategoryConfigurator(password)
        result = configurator.set_main_category(domain, main_category)
        
        print(f"成功: {result}")
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_main_category()