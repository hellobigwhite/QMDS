import sys
import os
import traceback
import ssl
import requests

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 禁用所有SSL验证
requests.packages.urllib3.disable_warnings()

# 尝试使用不同的SSL设置
def test_ssl_connection(domain):
    """测试SSL连接"""
    print(f"测试 SSL 连接: {domain}")
    
    # 尝试1: 默认设置
    try:
        print("尝试1: 默认设置...")
        r = requests.get(f"https://www.{domain}", verify=False, timeout=10)
        print(f"成功: status={r.status_code}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    # 尝试2: 不同的SSL版本
    try:
        print("尝试2: 使用 TLSv1.2...")
        from requests.adapters import HTTPAdapter
        from urllib3.poolmanager import PoolManager
        
        class TLSAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                context = ssl.create_default_context()
                context.min_version = ssl.TLSVersion.TLSv1_2
                kwargs['ssl_context'] = context
                return PoolManager(*args, **kwargs)
        
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        r = session.get(f"https://www.{domain}", verify=False, timeout=10)
        print(f"成功: status={r.status_code}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    # 尝试3: 完全禁用SSL验证
    try:
        print("尝试3: 完全禁用SSL验证...")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        session = requests.Session()
        session.verify = False
        r = session.get(f"https://www.{domain}", timeout=10)
        print(f"成功: status={r.status_code}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    return False

def test_login_debug(domain, password):
    """测试登录"""
    print(f"\n测试登录: {domain}")
    
    try:
        from auto_category import AutoCategoryConfigurator
        
        configurator = AutoCategoryConfigurator(password)
        
        # 测试登录
        print("开始登录...")
        result = configurator._login(domain)
        print(f"登录成功: {result}")
        return True
        
    except Exception as e:
        print(f"错误: {e}")
        print("详细错误:")
        traceback.print_exc()
        return False

if __name__ == "__main__":
    domain = "shifnora.com"
    password = "f!XsS$J2WneOkMyUgQ"
    
    print(f"=== 测试 {domain} ===")
    
    # 测试SSL连接
    ssl_ok = test_ssl_connection(domain)
    print(f"SSL连接测试: {'成功' if ssl_ok else '失败'}")
    
    if ssl_ok:
        # 测试登录
        login_ok = test_login_debug(domain, password)
        print(f"登录测试: {'成功' if login_ok else '失败'}")
    else:
        print("SSL连接失败，无法继续测试登录")