#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量查询 Google site:域名 收录数量，并真实提交到后台
- 使用 DrissionPage 控制 Chrome
- 遇到验证码时，直接按回车继续（需手动在浏览器完成验证）
- 真实提交使用 api_add_google_count 接口
- 建议间隔 10~20 秒，避免风控
"""

import re
import time
import sys
import requests
from urllib.parse import urlparse
from datetime import datetime

try:
    from DrissionPage import Chromium, ChromiumOptions
except ImportError:
    print("错误：未安装 DrissionPage")
    print("请执行： pip install DrissionPage")
    sys.exit(1)

# ====================== 配置 ======================
DEFAULT_INTERVAL = 12.0       # 建议 10~20 秒
PAGE_TIMEOUT = 40             # 页面加载超时（秒）
RETRY_TIMES = 2               # get 重试次数

# API 配置
TOKEN = None
BASE_URL = "http://123.60.135.93:8099"
USERNAME = "admin"
PASSWORD = "admin3696903"
API_SECRET = "235bc9573863156a829a5f0c7771e611"


# ========== API 相关函数 ==========

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
        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError("未能获取 token")
        TOKEN = token
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 登录成功")
    except Exception as e:
        print(f"登录失败: {e}")
        raise


def api_add_google_count(domain, count):
    """提交 Google 收录数量"""
    url = "http://123.60.135.93/api/search/add"
    headers = {
        "Authorization": f"Bearer {API_SECRET}",
        "Content-Type": "application/json",
    }
    payload = {
        "domainName": domain,
        "collectionCount": count
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def api_call_with_retry(func, *args, max_retries=3, **kwargs):
    """API 调用封装，自动处理 401 重新登录"""
    for attempt in range(1, max_retries + 1):
        try:
            if TOKEN is None:
                login()
            return func(*args, **kwargs)
        except requests.HTTPError as e:
            if e.response and e.response.status_code == 401:
                print(f"Token 失效，正在重新登录... (第 {attempt} 次尝试)")
                login()
                continue
            else:
                print(f"API HTTP 错误: {e.response.status_code} - {e.response.text}")
                break
        except Exception as e:
            print(f"API 调用异常: {e.__class__.__name__} - {e}")
            break
    print("达到最大重试次数，提交失败")
    return None


# ====================== 域名处理 ======================

def clean_domain(raw: str) -> str:
    """清理成纯域名"""
    s = raw.strip().lower()
    if not s:
        return ""

    if s.startswith(('http://', 'https://')):
        s = s.split('://', 1)[-1]

    if '://' not in s:
        s = 'http://' + s

    parsed = urlparse(s)
    domain = parsed.netloc or parsed.path
    if domain.startswith('www.'):
        domain = domain[4:]
    domain = domain.split(':', 1)[0]    # 去端口
    domain = domain.split('/', 1)[0]    # 去路径

    return domain


class GoogleSiteChecker:
    def __init__(self):
        options = ChromiumOptions().auto_port()
        # options.set_proxy('http://127.0.0.1:7890')  # 如需代理可打开
        self.browser = Chromium(options)
        self.tab = None

    def _extract_count(self) -> int | None:
        ele = self.tab.ele('@id=result-stats', timeout=10)
        if not ele:
            return None

        text = ele.text
        match = re.search(r'(?:约有|About)\s*([\d,]+)\s*(?:条|results)', text, re.I) \
                or re.search(r'(\d{1,3}(?:,\d{3})*)', text)

        if match:
            num_str = match.group(1).replace(',', '')
            try:
                return int(num_str)
            except ValueError:
                pass
        return None

    def check_one(self, domain_raw: str) -> tuple[bool, int | None]:
        domain = clean_domain(domain_raw)
        if not domain:
            print(f"  × 无效域名：{domain_raw}")
            return False, None

        print(f"  → site:{domain}")

        self.tab = self.browser.new_tab()
        success = False
        count = None

        try:
            url = f"https://www.google.com/search?q=site:{domain}"
            self.tab.get(url, retry=RETRY_TIMES, interval=4, timeout=PAGE_TIMEOUT)

            count = self._extract_count()
            if count is not None:
                print(f"  ✓ 约 {count:,} 条")
                self._submit_to_api(domain, count)
                success = True
            else:
                print("  ? 未读取到数量，可能出现验证码或异常页")
                print("    请在浏览器手动完成验证/同意条款")
                print("    完成后在此终端直接按回车（Enter）继续...")

                while True:
                    inp = input("    按回车继续 > ")
                    if inp.strip() == "":
                        break
                    print("    请直接按回车（不要输入其他内容）")

                self.tab.wait(3)
                count = self._extract_count()
                if count is not None:
                    print(f"  ✓ 人工干预后获取：约 {count:,} 条")
                    self._submit_to_api(domain, count)
                    success = True
                else:
                    print("  × 仍未获取到数量，跳过此域名")

        except Exception as e:
            print(f"  执行异常：{e.__class__.__name__} - {e}")

        finally:
            if self.tab:
                try:
                    self.tab.close()
                except:
                    pass

        return success, count

    def _submit_to_api(self, domain: str, count: int):
        """真实提交 Google 收录数量"""
        rounded = int(count)

        def wrapped():
            return api_add_google_count(domain, rounded)

        print(f"  [提交] {domain:30} → {rounded:>10,} 条 ... ", end="", flush=True)

        result = api_call_with_retry(wrapped)

        if result is not None:
            print("成功")
            print(f"  └─ 服务器返回: {result}")  # ← 新增这一行
        else:
            print("失败 - 检查网络/密钥/接口")

    def quit(self):
        try:
            self.browser.quit()
            print("\n浏览器已关闭")
        except:
            pass


def main():
    print("=== Google site: 批量收录查询 & 真实提交 ===\n")
    print("遇到验证码 → 浏览器手动处理 → 终端直接按回车（Enter）继续\n")

    # 尝试首次登录
    try:
        login()
    except Exception as e:
        print("无法登录，程序退出")
        return

    interval = DEFAULT_INTERVAL
    try:
        inp = input(f"查询间隔（秒，回车默认 {DEFAULT_INTERVAL}）：").strip()
        if inp:
            interval = float(inp)
            if interval < 5:
                interval = 5.0
                print("间隔过小，已调整为 5 秒")
    except:
        print(f"输入无效，使用默认 {DEFAULT_INTERVAL} 秒")

    path = input("域名文件路径（每行一个域名）：").strip().strip("'\"")
    if not path:
        print("未输入路径，退出。")
        return

    try:
        with open(path, encoding='utf-8') as f:
            domains = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"文件不存在：{path}")
        return
    except Exception as e:
        print(f"读取文件失败：{e}")
        return

    if not domains:
        print("文件为空，无域名可处理")
        return

    print(f"\n读取到 {len(domains)} 个域名，开始处理 ...\n")

    checker = GoogleSiteChecker()
    success_count = 0
    total = len(domains)

    try:
        for i, raw in enumerate(domains, 1):
            print(f"[{i:3}/{total:3}] {raw}")
            ok, cnt = checker.check_one(raw)
            if ok:
                success_count += 1
            if i < total:
                print(f"  等待 {interval:.1f} 秒 ...\n")
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\n用户中断（Ctrl+C）")
    finally:
        checker.quit()

    print(f"\n处理完成：成功 {success_count} / 总计 {total}")


if __name__ == '__main__':
    main()