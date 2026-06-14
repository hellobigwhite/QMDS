"""
建站脚本 —— 批量提交域名到 ERP 建站系统

使用方法：
1. 准备 Excel 文件（路径在下方 Website_path 设置），必须包含以下列：
   域名, 服务器, 模板, SEO Title（最大58字符）, Meta Description, 地址, 大类
   可选列：盘符（不填则默认 /www/wwwroot/）

2. 运行脚本：
   python mango\建站.py

脚本会自动登录 ERP → 读取 Excel → 逐个提交建站。
失败记录会保存到 data/logs/failed_domains.json，下次运行自动清空重试。
"""

import os
import re
import json
import urllib
from urllib import parse
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import time

# ======== 全局配置 ========
Website_path = r"C:\Users\Administrator\Desktop\建站域名管理.xlsx"
IMAGE_BASE_DIR = r"C:\Users\Administrator\Desktop\logo\未建站\开始建站"

LOGIN_URL = "https://erp.yswl.site/index.php?main_page=login&dongzuo=denglu"
ADD_SITE_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=addsite"
UPLOAD_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=uplogo"
ADD_PAGE_URL = "https://erp.yswl.site/index.php?main_page=site&p=addsite_d"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAILED_LOG = os.path.join(BASE_DIR, "data", "logs", "failed_domains.json")  # 失败域名记录文件（JSON）

session = requests.Session()
session.headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://erp.yswl.site/index.php/",
}

login_data = {"username": "linwei", "password": "linwei123"}  # 已去掉空格


# ======== 失败日志工具 ========
def load_failed_log():
    if not os.path.exists(FAILED_LOG):
        return {}
    try:
        with open(FAILED_LOG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_failed_log(failed_dict):
    with open(FAILED_LOG, "w", encoding="utf-8") as f:
        json.dump(failed_dict, f, ensure_ascii=False, indent=2)


def log_failure(domain, reason):
    failed = load_failed_log()
    failed[domain] = {"reason": reason, "time": datetime.now().isoformat()}
    save_failed_log(failed)
    print(f"❌ 记录失败：{domain} => {reason}")


def remove_failure(domain):
    failed = load_failed_log()
    if domain in failed:
        del failed[domain]
        save_failed_log(failed)
        print(f"✅ 从失败日志移除：{domain}")


# ======== 登录 ========
def login():
    resp = session.post(LOGIN_URL, data=login_data)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code == 200 and data.get("code") == 0:
        print("✅ 登录站群成功")
        response_sy = session.post('https://erp.yswl.site/index.php', data=login_data, allow_redirects=False)
        if response_sy.status_code == 200 and 'dashboard' in response_sy.text.lower():
            print("登录首页成功")
            return True
        else:
            raise ValueError("❌ 登录失败, 可能是触发了反爬机制或首页未包含 dashboard")
    else:
        raise ValueError(f"登录站群失败，状态码: {resp.status_code}, 响应: {resp.text[:200]}")


# ======== 从 Excel 获取域名数据 ========
def load_excel_domains():
    if not os.path.exists(Website_path):
        raise FileNotFoundError(f"文件不存在: {Website_path}")
    df = pd.read_excel(Website_path)

    required_cols = ["域名", "服务器", "模板", "SEO Title（最大58字符）", "Meta Description", "地址", "大类"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"缺少列: {col}")

    print("Excel 列名确认：", df.columns.tolist())  # 调试用
    return df.dropna(subset=["域名"])


# ======== 获取域名解析信息 ========
def get_jx(domain_name):
    url = f"https://erp.yswl.site/index.php?main_page=site&p=wh&sitename={domain_name}&ip="
    resp = session.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    if resp.status_code == 200 and soup.find("cite", string="爆单系统架设中。。。"):
        domain_td = soup.find("td", string=domain_name)
        if domain_td:
            row = domain_td.find_parent("tr")
            cells = row.find_all("td")
            if len(cells) >= 6:
                cf_data = {
                    "cfacc": cells[3].get_text(strip=True),
                    "cfkey": cells[4].get_text(strip=True),
                    "ip": cells[5].get_text(strip=True),
                }
                if not all(cf_data.values()):
                    raise ValueError("解析信息不完整（cfacc/cfkey/ip 其中有空）")
                print(f"✅ 解析信息获取成功: {cf_data}")
                return cf_data
            else:
                raise ValueError("解析页面结构异常，无法读取解析信息")
        else:
            raise ValueError(f"域名 {domain_name} 未解析或不存在")
    else:
        raise ValueError(f"进入 {domain_name} 的解析页面失败，状态码: {resp.status_code}")


# ======== 通用：获取 select 值 ========
def get_select_value(soup, select_name, match_text):
    select_tag = soup.find("select", {"name": select_name})
    if not select_tag:
        return None
    for option in select_tag.find_all("option"):
        if match_text in option.get_text(strip=True):
            return option.get("value", "").strip()
    return None


# ======== 获取建站页面参数 ========
def get_form_ids(fwq_input, mb_input, store_pf_input=""):
    resp = session.get(ADD_PAGE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    if resp.status_code == 200 and soup.find("cite", string="爆单系统架设中。。。"):
        fwq_value = get_select_value(soup, "site_fwq_id", fwq_input)
        mb_value = get_select_value(soup, "site_db_id", mb_input)
        store_pf_value = get_select_value(soup, "store_pf", store_pf_input)
        admin = soup.find("input", {"name": "site_admin_id"})
        admin_value = admin.get("value", "").strip() if admin else None

        if fwq_value and mb_value and admin_value:
            ids = {"site_fwq_id": fwq_value, "site_db_id": mb_value, "site_admin_id": admin_value, "store_pf": store_pf_value or "/www/wwwroot/"}
            print(f"✅ 建站页面参数提取成功: {ids}")
            return ids
        else:
            raise ValueError(
                f"从建站页面提取参数失败（服务器/模板/管理员 ID 可能未匹配） fwq={fwq_value}, mb={mb_value}, admin={admin_value}")
    else:
        raise ValueError(f"进入建站页面失败，状态码: {resp.status_code}")


# ======== 上传 LOGO ========
def upload_logo(domain_name):
    found_path = None
    domain_clean = domain_name.lower().replace("www.", "").strip()

    for root, dirs, files in os.walk(IMAGE_BASE_DIR):
        current_folder = os.path.basename(root).lower().strip()

        if current_folder == domain_clean or current_folder == f"www.{domain_clean}":
            logo_candidate = os.path.join(root, "logo.png")
            if os.path.exists(logo_candidate) and os.path.isfile(logo_candidate):
                found_path = logo_candidate
                break

    if not found_path:
        example = os.path.join(IMAGE_BASE_DIR, domain_name, "logo.png")
        raise FileNotFoundError(
            f"未找到 {domain_name} 的 logo.png\n"
            f"预期路径示例：{example}\n"
            f"已搜索根目录：{IMAGE_BASE_DIR}"
        )

    try:
        filename = os.path.basename(found_path)
        with open(found_path, "rb") as f:
            files = {"file": (filename, f)}
            data = {"model": domain_name}
            resp = session.post(UPLOAD_URL, files=files, data=data)

        res_json = resp.json()
        if res_json.get('code') == 0 and res_json.get('msg') == 'ok':
            print(f"✅ {domain_name} 上传 logo 成功，路径：{res_json.get('file')}")
            return res_json.get("file")
        else:
            raise ValueError(f"上传接口返回异常：{res_json}")

    except Exception as e:
        raise ValueError(f"上传图片失败 {found_path}：{str(e)}")


# ======== 地址解析 ========
def parse_us_address():
    url = "https://www.meiguodizhi.com/api/v1/dz"
    data = {"city": "", "path": "/", "method": "refresh"}
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()
            result = response.json()
            break
        except Exception as e:
            if attempt == max_retries:
                return None
            time.sleep(2)

    try:
        return {
            "store_code": result['address']['Zip_Code'],
            "store_state": f"US:{result['address']['State']}",
            "store_city": result['address']['City'],
            "store_address": result['address']['Address'],
        }
    except Exception:
        return None


# ======== 提交建站表单 ========
def post_site(form_data):
    resp = session.post(ADD_SITE_URL, data=form_data)
    try:
        json_resp = resp.json()
        success = json_resp.get("code") == 0
        return success, json_resp
    except Exception:
        raw = resp.text
        if raw.strip() == "":
            print(f"⚠️ {form_data['site_name']} 返回空响应 (HTTP {resp.status_code})，可能已建站或会话过期")
            return False, {"raw": raw, "status_code": resp.status_code, "error": "ERP 返回空响应"}
        print(f"⚠️ {form_data['site_name']} 返回非 JSON (HTTP {resp.status_code}): {raw[:300]}")
        return False, {"raw": raw, "status_code": resp.status_code, "error": f"ERP 返回非 JSON"}


# ======== 主流程 ========
def main():
    # 清除之前的失败域名记录
    failed_log = load_failed_log()
    if failed_log:
        print("检测到之前的失败域名记录，正在清除...")
        save_failed_log({})  # 清空失败记录
        print("✅ 已清除之前的失败域名记录")
    
    retry_failed = False

    try:
        if not login():
            return
    except Exception as e:
        print(f"登录失败：{e}")
        return

    try:
        df = load_excel_domains()
    except Exception as e:
        print(f"读取 Excel 失败: {e}")
        return

    print(f"共读取 {len(df)} 个域名")

    for idx, row in df.iterrows():
        domain = str(row["域名"]).strip()

        if "是否建空战" in df.columns:
            if str(row.get("是否建空战", "")).strip() == "是":
                print(f"跳过 {domain}（是否建空战 = 是）")
                continue

        fwq_input = str(row["服务器"]).strip()
        mb_input = str(row["模板"]).strip()  # 如列名改为"底板"，这里改成 row["底板"]
        store_pf = str(row.get("盘符", "")).strip()
        title = str(row["SEO Title（最大58字符）"]).strip()
        desc = str(row["Meta Description"]).strip()
        category = str(row["大类"]).strip()

        print(f"\n🚀 处理：{domain}")

        missing = []
        for name, val in [("服务器", fwq_input), ("模板", mb_input), ("SEO Title（最大58字符）", title), ("Meta Description", desc),
                          ("大类", category)]:
            if not val:
                missing.append(name)
        if missing:
            reason = f"缺少字段: {', '.join(missing)}"
            log_failure(domain, reason)
            continue

        try:
            cf_data = get_jx(domain)
        except Exception as e:
            log_failure(domain, f"获取解析信息失败: {e}")
            continue

        try:
            ids = get_form_ids(fwq_input, mb_input, store_pf)
        except Exception as e:
            log_failure(domain, f"获取建站参数失败: {e}")
            continue

        try:
            logo_file = upload_logo(domain)
        except Exception as e:
            log_failure(domain, f"上传 logo 失败: {e}")
            continue

        addr_info = parse_us_address()
        if not addr_info:
            log_failure(domain, "获取随机美国地址失败")
            continue

        form_data = {
            "site_name": domain.strip(),
            "cfacc": cf_data["cfacc"].strip(),
            "cfkey": cf_data["cfkey"].strip(),
            "site_fwq_id": str(ids["site_fwq_id"]).strip(),
            "site_db_id": str(ids["site_db_id"]).strip(),
            "site_title": title.strip(),
            "site_dec": desc.strip(),
            "store_adress": addr_info["store_address"].strip(),
            "store_city": addr_info["store_city"].strip(),
            "store_code": addr_info["store_code"].strip(),
            "store_state": addr_info["store_state"].strip(),
            "file": "",
            "imgs[0]": logo_file.strip(),
            "site_beizhu": category.strip(),
            "site_admin_id": str(ids["site_admin_id"]).strip(),
            "store_pf": "/www/wwwroot/"
        }

        try:
            success, resp = post_site(form_data)
            if success:
                print(f"🎉 {domain} 建站成功")
                remove_failure(domain)
            else:
                err_msg = resp.get("error") or str(resp)
                log_failure(domain, f"建站失败: {err_msg}")
                if "空响应" in err_msg:
                    print(f"{domain} 站可能已建成，或 ERP 会话过期")
                else:
                    print(f"{domain} 建站失败: {err_msg}")
        except Exception as e:
            log_failure(domain, f"提交建站异常: {e}")

        # 恢复你想要的这行（即使 success 是布尔值，也按原样保留）
        if success != 200:
            continue

    print("\n所有域名处理完毕。")
    remaining = load_failed_log()
    if remaining:
        print("剩余失败域名：")
        for d, info in remaining.items():
            print(f" - {d}: {info.get('reason')}")
    else:
        if os.path.exists(FAILED_LOG):
            try:
                os.remove(FAILED_LOG)
                print("失败日志已清空并删除文件")
            except:
                pass


if __name__ == "__main__":
    main()
