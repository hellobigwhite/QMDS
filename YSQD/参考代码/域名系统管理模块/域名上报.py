# 文件名建议：add_domain.py
import requests
import json
from datetime import datetime

# ========== 配置区 ==========
BASE_URL = "http://123.60.135.93:8099"
USERNAME = "俊杰"
PASSWORD = "junjie666"


# ========== 全局变量 ==========
TOKEN = None


def login():
    """登录获取 token"""
    global TOKEN
    url = f"{BASE_URL}/login"
    data = {
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }

    try:
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError("登录响应中未找到 token")

        TOKEN = token
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 登录成功")
        return True
    except Exception as e:
        print(f"登录失败: {e}")
        print("响应内容:", resp.text if 'resp' in locals() else "无响应")
        return False


def add_domain(domain_data):
    """提交新增域名请求"""
    if not TOKEN:
        if not login():
            return None

    url = f"{BASE_URL}/system/domainmanage"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=domain_data, headers=headers, timeout=15)
        resp.raise_for_status()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 添加域名成功")
        return resp.json()
    except requests.HTTPError as e:
        print(f"添加失败 HTTP {e.response.status_code}")
        print("响应内容:", e.response.text)
        # 401 可能是 token 失效，可尝试一次重新登录后重试
        if e.response.status_code == 401:
            print("检测到 401，尝试重新登录...")
            if login():
                return add_domain(domain_data)  # 递归重试一次
        return None
    except Exception as e:
        print(f"请求异常: {e}")
        return None


# ========== 主程序 ==========
if __name__ == "__main__":
    # 要添加的域名信息
    domain_info = {
        "name": "electricrckit.com",
        "serverip": "198.175.125.74",
        "template": "zh01",
        "category": "1",
        "categoryTag": None,
        "language": None
    }

    print("准备添加域名：", domain_info["name"])
    result = add_domain(domain_info)

    if result:
        print("返回结果：")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("添加域名失败")