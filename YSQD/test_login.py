import sys
import os
import traceback

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_category import AutoCategoryConfigurator

def test_login():
    """测试登录功能"""
    try:
        domain = "shifnora.com"
        password = "f!XsS$J2WneOkMyUgQ"
        
        print(f"测试登录: {domain}")
        
        configurator = AutoCategoryConfigurator(password)
        
        # 测试登录
        print("开始登录...")
        result = configurator._login(domain)
        print(f"登录成功: {result}")
        
        # 测试获取菜单参数
        print("获取菜单参数...")
        params = configurator._get_menu_params(domain)
        print(f"获取参数成功: {params.keys()}")
        
    except Exception as e:
        print(f"错误: {e}")
        print("详细错误:")
        traceback.print_exc()

if __name__ == "__main__":
    test_login()