import requests
import json
from datetime import datetime
import os

# ========== 配置区 ==========

# 获取当前脚本所在目录及项目主目录
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)

# 配置目录和账号配置文件路径
# config_dir = os.path.join(project_root, '配置')
# cred_config_path = os.path.join(config_dir, '域名管理系统配置.json')

# ========== 全局变量 ==========
TOKEN = None
BASE_URL = "http://123.60.135.93:8099"
USERNAME = "admin"
PASSWORD = "admin3696903"
API_SECRET = "235bc9573863156a829a5f0c7771e611"

# ========== 初始化配置 ==========
# def init_config():
#     """初始化配置，设置 BASE_URL、USERNAME、PASSWORD"""
#     global BASE_URL, USERNAME, PASSWORD
#     try:
#         with open(cred_config_path, "r", encoding="utf-8") as f:
#             config = json.load(f)
#             BASE_URL = config["base_url"]
#             USERNAME = config["username"]
#             PASSWORD = config["password"]
#     except Exception as e:
#         raise RuntimeError(f"无法加载账号配置文件: {e}")

# ========== 加载账号密码 ==========
# def load_credentials():
#     """返回全局变量中存储的账号和密码"""
#     return USERNAME, PASSWORD

# ========== 登录 ==========
def login():
    """登录获取新的 token 并更新全局变量 TOKEN"""
    global TOKEN
    # username, password = load_credentials()
    url = f"{BASE_URL}/login"
    data = {
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }
    try:
        resp = requests.post(url, data=data)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError("未能获取 access_token，请检查返回值：" + resp.text)
        TOKEN = token
        print(f"[{datetime.now().isoformat()}] 登录成功，获取新 token")
    except Exception as e:
        raise RuntimeError(f"登录失败: {e}")

# ========== API 调用函数 ==========
def add_domainseo(domain, count):
    """新增域名 SEO 信息"""
    url = f"{BASE_URL}/system/domainseo"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"domainName": domain, "collectionCount": count}
    resp = requests.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()

def get_domain_id(domain_name, page_num=1, page_size=10):
    """通过域名查询对应的 ID"""
    url = f"{BASE_URL}/system/domainmanage/list"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    params = {"name": domain_name, "pageNum": page_num, "pageSize": page_size}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    if data.get("total", 0) == 0:
        return None
    return data["rows"][0].get("id")

def update_access_test(domain_id=None, domain_name=None, access_time=None, access_status=None):
    """更新域名访问测试结果"""
    if domain_name and not domain_id:
        domain_id = get_domain_id(domain_name)
        if not domain_id:
            raise ValueError(f"未找到域名 '{domain_name}' 对应的 ID")
    if not domain_id:
        raise ValueError("必须提供域名 ID 或域名")
    url = f"{BASE_URL}/system/domainmanage"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "id": domain_id,
        "name": domain_name,
        "accessTime": access_time or datetime.now().isoformat(),
        "accessStatus": str(access_status) if access_status is not None else "unknown",
    }
    resp = requests.put(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()

def update_rating(domain_id=None, domain_name=None, rating=None, rating_pc=None):
    """更新域名评分"""
    if domain_name and not domain_id:
        domain_id = get_domain_id(domain_name)
        if not domain_id:
            raise ValueError(f"未找到域名 '{domain_name}' 对应的 ID")
    if not domain_id:
        raise ValueError("必须提供域名 ID 或域名")
    url = f"{BASE_URL}/system/domainmanage"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "id": domain_id,
        "name": domain_name,
        "rating": str(rating) if rating is not None else "unknown",
        "ratingPc": str(rating_pc) if rating_pc is not None else "unknown",
    }
    resp = requests.put(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()

def api_call_with_retry(func, *args, **kwargs):
    """封装 API 调用，遇到 401 自动重新登录并重试"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if TOKEN is None:
                login()
            return func(*args, **kwargs)
        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                print("Token 失效，重新登录获取...")
                login()
            else:
                print(f"API 调用失败: {e}")
                break
        except Exception as e:
            print(f"调用异常: {e}")
            break
    print("操作失败，已达到最大重试次数或发生错误")
    return None

def api_add_google_count(domain, count):
    # post_domain = 'theme99.com'
    post_domain = '123.60.135.93'
    url = f"http://{post_domain}/api/search/add"
    headers = {
        "Authorization": f"Bearer {API_SECRET}",
        "Content-Type": "application/json",
    }
    payload = {"domainName": domain, "collectionCount": count}
    resp = requests.post(url, json = payload, headers = headers)
    resp.raise_for_status()
    return resp.json()

# ========== 示例使用 ==========
if __name__ == "__main__":
    # init_config()  # 加载配置
    # login()        # 获取 Token

    domain = "buyaquariumfish.com"
    result = api_add_google_count(domain, 15300)
    print(result)
    # 示例：更新访问测试结果
    # result = api_call_with_retry(update_access_test, domain_name=domain, access_status=1)
    # print("更新访问测试结果成功:", result)

    # # 示例：更新评分
    # result = api_call_with_retry(update_rating, domain_name=domain, rating=88, rating_pc=88)
    # print("更新评分成功:", result)

    # # 示例：添加 SEO 信息
    # result = api_call_with_retry(add_domainseo, domain, 3000)
    # print("添加域名 SEO 信息成功:", result)
