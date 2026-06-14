import os
import re
import json
import urllib
from urllib import parse
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime

# ======== 全局配置 ========
Website_path = r"C:\Users\Administrator\Desktop\建站域名管理.xlsx"
IMAGE_BASE_DIR = r"D:\上传数据"

LOGIN_URL = "https://erp.yswl.site/index.php?main_page=login&dongzuo=denglu"
ADD_SITE_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=addsite"
UPLOAD_URL = "https://erp.yswl.site/index.php?main_page=site&dongzuo=uplogo"
ADD_PAGE_URL = "https://erp.yswl.site/index.php?main_page=site&p=addsite_d"

FAILED_LOG = "failed_domains.json"  # 失败域名记录文件（JSON）

session = requests.Session()
session.headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://erp.yswl.site/index.php/",
}

login_data = {"username": "cd003", "password": "cd55221xx"}


# ======== 失败日志工具 ========
def load_failed_log():
    """加载失败域名日志，返回 dict: domain -> {reason, time}"""
    if not os.path.exists(FAILED_LOG):
        return {}
    try:
        with open(FAILED_LOG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_failed_log(failed_dict):
    """保存失败日志（覆盖写入）"""
    with open(FAILED_LOG, "w", encoding="utf-8") as f:
        json.dump(failed_dict, f, ensure_ascii=False, indent=2)


def log_failure(domain, reason):
    """记录单个域名失败原因（或更新已有记录）"""
    failed = load_failed_log()
    failed[domain] = {"reason": reason, "time": datetime.now().isoformat()}
    save_failed_log(failed)
    print(f"❌ 记录失败：{domain} => {reason}")


def remove_failure(domain):
    """从失败日志中删除已成功的域名"""
    failed = load_failed_log()
    if domain in failed:
        del failed[domain]
        save_failed_log(failed)
        print(f"✅ 从失败日志移除：{domain}")


# ======== 登录 ========
def login():
    """登录 ERP 系统"""
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
            raise ValueError("❌ 登录失败, 可能是触发了反爬机制")
    else:
        raise ValueError("登录站群失败")


# ======== 从 Excel 获取域名数据 ========
def load_excel_domains():
    """读取 Excel 并返回 DataFrame"""
    if not os.path.exists(Website_path):
        raise FileNotFoundError(f"文件不存在: {Website_path}")
    df = pd.read_excel(Website_path)

    required_cols = ["域名", "服务器", "模板", "标题", "描述", "地址", "大类"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"缺少列: {col}")

    # 保留至少有域名的行
    return df.dropna(subset=["域名"])


# ======== 获取域名解析信息 ========
def get_jx(domain_name):
    """访问解析页面，获取 cfacc / cfkey / ip"""
    url = f"https://erp.yswl.site/index.php?main_page=site&p=wh&sitename={domain_name}&ip="
    resp = session.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    if resp.status_code == 200 and soup.find("cite", string="爆单系统架设中。。。"):
        domain_td = soup.find("td", string=domain_name)
        if domain_td:
            row = domain_td.find_parent("tr")
            cells = row.find_all("td")
            # 防止索引越界
            if len(cells) >= 6:
                cf_data = {
                    "cfacc": cells[3].get_text(strip=True),
                    "cfkey": cells[4].get_text(strip=True),
                    "ip": cells[5].get_text(strip=True),
                }
                # 如果任一为空则视为失败
                if not all(cf_data.values()):
                    raise ValueError("解析信息不完整（cfacc/cfkey/ip 其中有空）")
                print(f"✅ 解析信息获取成功")
                return cf_data
            else:
                raise ValueError("解析页面结构异常，无法读取解析信息")
        else:
            raise ValueError(f"域名 {domain_name} 未解析或不存在")
    else:
        raise ValueError(f"登录{domain_name}的解析页面失败")


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
def get_form_ids(fwq_input, mb_input):
    """从建站页面提取服务器、模板、管理员 ID"""
    resp = session.get(ADD_PAGE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    if resp.status_code == 200 and soup.find("cite", string="爆单系统架设中。。。"):
        fwq_value = get_select_value(soup, "site_fwq_id", fwq_input)
        mb_value = get_select_value(soup, "site_db_id", mb_input)
        admin = soup.find("input", {"name": "site_admin_id"})
        admin_value = admin.get("value", "").strip() if admin else None
        if fwq_value and mb_value and admin_value:
            ids = {"site_fwq_id": fwq_value, "site_db_id": mb_value, "site_admin_id": admin_value}
            print(f"✅ 建站页面参数提取成功")
            return ids
        else:
            raise ValueError("从建站页面提取参数失败（服务器/模板/管理员 ID 可能未匹配）")
    else:
        raise ValueError("进入建站页面失败")


# ======== 上传 LOGO ========
def upload_logo(domain_name):
    """递归查找 logo.png 并上传"""
    for root, dirs, _ in os.walk(IMAGE_BASE_DIR):
        if domain_name in dirs:
            # 修改这里：从 domain_name/图片资源 下找 logo.png
            logo_path = os.path.join(root, domain_name, "图片资源", "logo.png")
            if os.path.exists(logo_path):
                try:
                    with open(logo_path, "rb") as f:
                        files = {"file": f}
                        data = {"model": domain_name}
                        resp = session.post(UPLOAD_URL, files=files, data=data)
                    res_json = resp.json()
                    if res_json.get('code') == 0 and res_json.get('msg') == 'ok':
                        print(f"✅ {domain_name} 上传 logo 成功, file: {res_json.get('file')}")
                        return res_json.get("file")
                    else:
                        raise ValueError(f"图片请求响应失败: {res_json}")
                except Exception as e:
                    raise ValueError(f"上传图片失败: {e}")
            raise ValueError("无法打开文件路径（logo.png 未找到）")
    raise ValueError("未搜寻到域名文件夹")


# ======== 地址解析 ========
def parse_us_address(address):
    address = re.sub(r"[^a-zA-Z0-9,\s]", "", address).strip()
    pattern = r"^(.*?),\s*([\w\s]+),\s*([A-Z]{2})\s*(\d{5})$"
    match = re.match(pattern, address)
    if not match:
        raise ValueError("获取地址参数出错（地址格式应为：街道, 城市, ST 12345）")
    street, city, state, zipcode = match.groups()
    return {
        "store_code": zipcode,
        "store_state": f"US:{state}",
        "store_city": city.strip(),
        "store_address": street.strip(),
    }


# ======== 提交建站表单 ========
def post_site(form_data):
    resp = session.post(ADD_SITE_URL, data=form_data)
    try:
        json_resp = resp.json()
        success = json_resp.get("code") == 0
        return success, json_resp
    except Exception:
        print(f"⚠️ {form_data['site_name']} 返回非 JSON 内容: {resp.text[:200]}")
        return False, {"raw": resp.text}


# ======== 主流程 ========
def main():
    # 加载失败日志，询问是否重试失败域名（如果存在）
    failed_log = load_failed_log()
    retry_failed = False
    skip_confirmation = False  # 新增：是否跳过确认环节

    if failed_log:
        print("检测到失败域名记录：")
        for d, info in failed_log.items():
            print(f" - {d}: {info.get('reason')} (记录时间: {info.get('time')})")
        ans = input("是否在本次运行时尝试重新建立这些失败域名？(y/n): ").strip().lower()
        retry_failed = ans == "y"
        if retry_failed:
            skip_confirmation = True  # 选择重试失败域名时跳过确认
            print("🎯 将直接处理失败域名")

    # 登录
    try:
        if not login():
            return
    except Exception as e:
        print(f"登录失败：{e}")
        return

    # 读取 Excel
    try:
        df = load_excel_domains()
    except Exception as e:
        print(f"读取 Excel 失败: {e}")
        return

    print(f"共读取 {len(df)} 个域名（包含可能的已失败域名）")

    # 如果用户选择重试失败域名，将保证这些域名在 df 中被处理（去重）
    if retry_failed and failed_log:
        failed_domains = set(failed_log.keys())
        # 筛选出在 Excel 中仍存在的失败域名并优先放入待处理集合（通过合并和去重实现）
        df_failed_present = df[df['域名'].isin(failed_domains)]
        if not df_failed_present.empty:
            # 合并并去重（以域名为准）
            df = pd.concat([df_failed_present, df]).drop_duplicates(subset=['域名']).reset_index(drop=True)
            print(f"将 {len(df_failed_present)} 个失败域名从日志中加入本次处理队列（若在 Excel 中存在）")

    # 筛选出需要建站的域名（"是否建空战"为"否"的）
    domains_to_process = []
    success_domains_list = []  # 新增：用于存储识别成功的域名

    for idx, row in df.iterrows():
        domain = str(row["域名"]).strip()
        # 检查是否建空战列（若存在且为 '否'，则处理）
        if "是否建空战" in df.columns:
            try:
                val = str(row.get("是否建空战", "")).strip()
                # 只有当明确为"否"时才建站，其他情况都跳过
                if val == "否":
                    domains_to_process.append((idx, row))
                    success_domains_list.append(domain)  # 添加到成功识别列表
            except Exception:
                pass
        else:
            # 如果没有"是否建空战"列，默认处理所有域名
            domains_to_process.append((idx, row))
            success_domains_list.append(domain)  # 添加到成功识别列表

    if not domains_to_process:
        print("❌ 没有需要建站的域名")
        return

    if skip_confirmation:
        # 选择重试失败域名时直接开始处理，不询问确认
        print(f"\n🎯 开始处理 {len(domains_to_process)} 个域名...")
    else:
        print(f"\n📋 识别结果: 找到 {len(domains_to_process)} 个需要建站的域名")

        # 显示识别成功的域名列表
        if success_domains_list:
            print("识别成功的域名:")
            for i, domain in enumerate(success_domains_list, 1):
                print(f"{i}. {domain}")

        # 询问是否处理这些域名
        while True:
            choice = input(f"是否处理这 {len(domains_to_process)} 个域名？(y=处理, n=跳过): ").strip().lower()
            if choice in ['y', 'yes', '是']:
                print("✅ 开始处理所有域名")
                break
            elif choice in ['n', 'no', '否']:
                print("⏭️ 跳过所有域名，程序结束")
                return
            else:
                print("❌ 请输入 y 或 n")

    # 遍历每个需要建站的域名
    for idx, row_data in domains_to_process:
        idx, row = idx, row_data
        domain = str(row["域名"]).strip()
        fwq_input = str(row["服务器"]).strip()
        mb_input = str(row["模板"]).strip()
        title = str(row["标题"]).strip()
        desc = str(row["描述"]).strip()
        address = str(row["地址"]).strip()
        category = str(row["大类"]).strip()

        print(f"\n🚀 正在处理：{domain}")

        # 预检查：Excel 中的关键字段是否齐全（只要有一个缺失就记录失败并跳过）
        missing_fields = []
        for field_name, field_val in [
            ("服务器", fwq_input),
            ("模板", mb_input),
            ("标题", title),
            ("描述", desc),
            ("地址", address),
            ("大类", category),
        ]:
            if not field_val:
                missing_fields.append(field_name)
        if missing_fields:
            reason = f"Excel 缺少必要字段: {', '.join(missing_fields)}"
            log_failure(domain, reason)
            print(f"❌ {domain} 跳过（{reason}）")
            continue

        # ① 获取解析信息
        try:
            cf_data = get_jx(domain)
        except Exception as e:
            reason = f"获取解析信息失败: {e}"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（解析信息获取失败）")
            continue
        if not cf_data:
            reason = "解析信息为空"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（解析信息为空）")
            continue

        # ② 获取建站参数
        try:
            ids = get_form_ids(fwq_input, mb_input)
        except Exception as e:
            reason = f"获取建站参数失败: {e}"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（建站页面参数提取失败）")
            continue
        if not ids:
            reason = "建站页面参数为空"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（建站页面参数为空）")
            continue

        # ③ 上传 LOGO
        try:
            logo_file = upload_logo(domain)
        except Exception as e:
            reason = f"上传 logo 失败: {e}"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（上传 logo 失败）")
            continue
        if not logo_file:
            reason = "上传 logo 未返回文件路径"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（上传 logo 未返回文件路径）")
            continue

        # ④ 地址解析
        try:
            addr_info = parse_us_address(address)
        except Exception as e:
            reason = f"地址解析失败: {e}"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（地址解析失败）")
            continue
        if not addr_info:
            reason = "地址解析结果为空"
            log_failure(domain, reason)
            print(f"❌ 跳过 {domain}（地址解析结果为空）")
            continue
        # ⑤ 组合表单数据
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
        }
        try:
            success, resp = post_site(form_data)
            if success:
                print(f"🎉 {domain} 建站成功")
                # 如果之前失败过则从失败日志中移除
                remove_failure(domain)

                # ======== 新增：更新Excel文件状态 ========
                try:
                    # 更新"是否建空战"列为"是"
                    df.at[idx, "是否建空战"] = "是"
                    # 立即保存Excel文件
                    df.to_excel(Website_path, index=False)
                    print(f"✅ 已更新 {domain} 的'是否建空战'状态为'是'")
                except Exception as e:
                    print(f"⚠️ 更新Excel状态失败: {e}")
                # ======== 新增结束 ========

            else:
                # 记录失败并继续（resp 中可能包含具体原因）
                if resp.get('raw') == '\n\n  ':
                    print("站已建成，请勿重复建站")
                    log_failure(domain, "站已建成，请勿重复建站")
                else:
                    reason = f"建站接口返回失败: {resp}"
                    log_failure(domain, reason)
                    print(f"❌ {domain} 建站接口返回失败，已记录")
        except Exception as e:
            reason = f"提交建站异常: {e}"
            log_failure(domain, reason)
            print(f"❌ {domain} 提交建站异常，已记录")
        if success != 200:
            continue
    print("\n所有域名处理完毕。")
    remaining_failed = load_failed_log()
    if remaining_failed:
        print("以下域名仍在失败日志中：")
        for d, info in remaining_failed.items():
            print(f" - {d}: {info.get('reason')} (记录时间: {info.get('time')})")
    else:
        # 若无失败记录则删除日志文件（可选）
        if os.path.exists(FAILED_LOG):
            try:
                os.remove(FAILED_LOG)
                print("所有失败域名已清除，删除失败日志文件。")
            except Exception:
                pass


# ======== 启动 ========
if __name__ == "__main__":
    main()
