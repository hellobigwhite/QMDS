import sys
import os
import traceback

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_category import AutoCategoryConfigurator

def test_single_domain():
    """测试单个域名的主分类设置"""
    try:
        # 替换为实际的测试数据
        domain = "your-domain.com"  # 替换为失败的域名
        main_category = "Your|||Main|||Category"  # 替换为实际的主分类
        password = "your-wp-password"  # 替换为实际的WP密码
        
        print(f"测试域名: {domain}")
        print(f"主分类: {main_category}")
        
        configurator = AutoCategoryConfigurator(password)
        result = configurator.set_main_category(domain, main_category)
        
        print(f"成功: {result}")
        
    except Exception as e:
        print(f"错误: {e}")
        print("详细错误:")
        traceback.print_exc()

if __name__ == "__main__":
    test_single_domain()