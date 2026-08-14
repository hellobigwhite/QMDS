import asyncio
import glob
import json
import os
import re
import ssl
import sys
import time
from typing import List
from xml.etree import ElementTree as ET

# 将项目根目录加入 sys.path，确保能正确导入中文路径模块
sys.path.insert(0, r"D:\python_work")

import aiohttp
import furl
from exa_py import Exa

from 工作.shopify链接获取.shopify_domain_db import (
    add_failed_domain,
    get_all_domains_to_exclude,
)
from 工作.shopify链接获取.shopify_数据库操作 import (
    get_available_proxy,
    update_proxy_status,
)


def standardt_domain(url):
    """使用 furl 库从 URL 获取域名

    自动补 scheme：纯域名（如 'fstoys.com'）没有 http(s):// 前缀时，
    furl 会把它当成相对路径解析，导致 host 为 None。
    这里检测到无 scheme 时补 'http://' 再解析。
    """
    if not url or not isinstance(url, str):
        return None

    url = url.strip()
    if not url:
        return None

    # 跳过明显的注释/说明行（非域名）
    # 域名至少包含一个点，且不含空格和中文
    if " " in url or "." not in url:
        print(f"跳过非域名行: {url}")
        return None

    # furl 需要 scheme 才能正确解析 host
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    try:
        f = furl.furl(url)
        host = f.host
        if host and host.lower().startswith("www."):
            return host[4:]
        return host
    except Exception:
        print(f"无法规范化 URL: {url}")
        return None


def input_keywords():
    # 1. 用户输入关键词
    keyword = input("请输入Google搜索关键词: ").strip()
    if not keyword:
        print("关键词不能为空")
        return None

    # 关键词合法性校验：拒绝把整条命令/路径误粘贴进来
    # 含路径分隔符、冒号、可执行文件扩展名等，明显不是搜索关键词
    if re.search(r'[\\/:*?"<>|]|\.exe$|\.py\b|\s\.venv\b', keyword):
        print(
            f"关键词含非法字符或像命令/路径，请只输入搜索关键词（如 toy），"
            f"不要粘贴整条命令。收到: {keyword}"
        )
        return None

    # 2. 构建 AI 搜索提示词：直接要求返回以关键词为主推的 Shopify 网站
    ai_prompt = f"Search for Shopify websites featuring {keyword} or related products, where the website language is English."
    keyword_links = [ai_prompt]

    # 3. 输入匹配关键词
    match_keywords_input = input(
        f"请输入匹配关键词(以逗号连接，默认为'{keyword}'): "
    ).strip()
    if match_keywords_input:
        match_keywords = [
            kw.strip() for kw in match_keywords_input.split(",") if kw.strip()
        ]
    else:
        match_keywords = [keyword]

    # 4. 输入固定关键词
    fixed_keyword = input("请输入固定关键词(默认为空): ").strip()

    # 5. 输入目标域名数量（本轮搜索并筛选后达到该数量才停止，默认 100）
    target_count_input = input("请输入目标域名数量(默认为100): ").strip()
    try:
        target_count = int(target_count_input) if target_count_input else 100
        if target_count < 0:
            print("目标数量不能为负数，已重置为 100")
            target_count = 100
    except ValueError:
        print("目标数量必须是数字，已重置为 100")
        target_count = 100

    # 6. 文件路径处理
    base_dir = r"D:\python_work\data\分类域名库\shopify分类"
    non_shopify_base_dir = r"D:\python_work\data\分类域名库\非shopify分类"

    # 检查基础目录是否存在，不存在则创建
    for d in (base_dir, non_shopify_base_dir):
        if not os.path.exists(d):
            os.makedirs(d)
            print(f"已创建基础目录: {d}")

    # 文件名清洗：移除 Windows 非法字符 \ / : * ? " < > |，避免 open() 报错
    safe_keyword = re.sub(r'[\\/:*?"<>|]', "", keyword).strip()
    if not safe_keyword:
        print(f"关键词 '{keyword}' 清洗后为空，无法生成文件名")
        return None

    # 递归搜索文件
    search_pattern = os.path.join(base_dir, "**", f"{safe_keyword}.txt")
    found_files = glob.glob(search_pattern, recursive=True)

    file_path = ""
    non_shopify_file_path = ""

    if found_files:
        # 找到文件，取第一个匹配的
        file_path = found_files[0]
        print(f"找到现有文件: {file_path}")
        # 非 Shopify 文件路径：保持与 shopify 文件相同的相对分类结构
        rel = os.path.relpath(file_path, base_dir)
        non_shopify_file_path = os.path.join(non_shopify_base_dir, rel)
    else:
        # 未找到文件，让用户输入分类名
        category_name = input(f"未找到'{safe_keyword}.txt'文件，请输入分类名: ").strip()
        if not category_name:
            print("分类名不能为空")
            return None

        # 创建分类目录
        category_dir = os.path.join(base_dir, category_name)
        if not os.path.exists(category_dir):
            os.makedirs(category_dir)
            print(f"已创建分类目录: {category_dir}")

        # 创建文件路径
        file_path = os.path.join(category_dir, f"{safe_keyword}.txt")
        open(file_path, "a", encoding="utf-8").close()
        print(f"将创建新文件: {file_path}")

        # 非 Shopify 分类目录和文件
        non_shopify_category_dir = os.path.join(non_shopify_base_dir, category_name)
        os.makedirs(non_shopify_category_dir, exist_ok=True)
        non_shopify_file_path = os.path.join(
            non_shopify_category_dir, f"{safe_keyword}.txt"
        )
        open(non_shopify_file_path, "a", encoding="utf-8").close()
        print(f"将创建非Shopify文件: {non_shopify_file_path}")

    return (
        keyword_links,
        match_keywords,
        fixed_keyword,
        file_path,
        target_count,
        non_shopify_file_path,
    )


def single_api_search(query, exclude_domains, max_retries=20):
    """使用 Exa API 搜索，返回规范化去重后的域名列表。


    exclude_domains: 排除域名列表（已访问的 txt 域名 + 数据库排除列表），
                     由调用方加载好后传入，避免每次重新读文件
    返回: ["d1.com", "d2.com", ...] 规范化去重后的域名列表
    """
    # 防御性拷贝，避免外部集合在请求期间被修改导致 Exa SDK 报错
    exclude_domains = list(exclude_domains) if exclude_domains else []

    retry_count = 0
    while retry_count < max_retries:
        try:
            exa = Exa("d8df404f-e2b5-4d5f-9bbb-ef7f25ff5f3f")

            result = exa.search(
                query,
                exclude_domains=exclude_domains,
                num_results=30,
                type="auto",
                user_location="US",
                contents={"highlights": True},
            )

            # 从搜索结果中提取 URL，规范化为域名，去重
            raw_urls = []
            for r in result.results:
                if r.url:
                    raw_urls.append(r.url)

            domains = []
            seen = set()
            for raw_url in raw_urls:
                d = standardt_domain(raw_url)
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)

            print(f"搜索 {query} 完成，规范化去重后得到 {len(domains)} 个域名")
            return domains
        except Exception as e:
            print(f"API 请求异常: {e}")
            time.sleep(1)
            retry_count += 1
            continue

    print("已达到最大重试次数.")
    return []


# Windows 控制台（cp936/GBK）输出 emoji 或某些 Unicode 字符时会抛出
# OSError: [Errno 22] Invalid argument。封装安全打印函数，失败时降级为 ASCII。
def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except (OSError, UnicodeEncodeError):
        safe_args = []
        for a in args:
            s = str(a)
            safe_args.append(s.encode("ascii", "replace").decode("ascii"))
        try:
            print(*safe_args, **kwargs)
        except Exception:
            pass


# 检测 shopify 域名
async def shopify_url(
    urls: List[str],
    match_keywords: List[str],
    fixed_keyword: str,
    file_path: str,
    non_shopify_file_path: str = "",
    max_concurrency: int = 10,
):
    semaphore = asyncio.Semaphore(max_concurrency)

    success_domains = []
    non_shopify_success_domains = []  # 非 Shopify 电商域名
    failure_counts = {}  # 记录每个域名的失败次数
    matched_domains = set()  # 已匹配成功的域名
    lock = asyncio.Lock()  # 用于同步访问共享数据

    # 支付处理器指标：用于检测非 Shopify 电商网站
    PAYMENT_INDICATORS = [
        "js.stripe.com",
        "stripe.js",
        "paypal.com/sdk",
        "paypalobjects.com",
        "klarna.com",
        "squareup.com",
        "shopify_payments",
        "afterpay.com",
        '<meta name="generator" content="prestashop">',
        "opencart",
        "braintreegateway.com",
        "adyen.com",
        "checkout.com",
    ]

    async def detect_non_shopify_ecommerce(url, html_text):
        """检测非 Shopify 电商网站：通过支付处理器指标判断。

        返回 (是否电商, 指标名) 或 (False, None)。
        """
        html_lower = html_text.lower()
        for indicator in PAYMENT_INDICATORS:
            if indicator in html_lower:
                return True, indicator
        return False, None

    # 关键词预处理
    match_keywords_lower = [kw.lower() for kw in match_keywords]
    fixed_keyword_lower = fixed_keyword.lower() if fixed_keyword else ""

    def keyword_match(text: str) -> bool:
        """关键词匹配逻辑"""
        text = text.lower()

        # collections关键词拆分匹配
        def split_match(keyword, target):
            parts = keyword.split()
            # 整词匹配：每个词都要作为完整单词出现（单词边界），避免 cat 误匹配 category
            return all(
                re.search(r"\b" + re.escape(part) + r"\b", target) for part in parts
            )

        # 有固定关键词
        if fixed_keyword_lower:
            if fixed_keyword_lower not in text:
                return False

        # 匹配关键词
        for kw in match_keywords_lower:
            if split_match(kw, text):
                return True

        return False

    async def fetch_with_proxy(url):
        """使用本地代理数据库中的可用代理获取 URL 内容。"""
        retry_count = 0
        max_retries = 3
        last_error = "未知原因"

        # 部分代理的证书链不完整，沿用历史版本的 SSL 兼容策略。
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        while retry_count < max_retries:
            proxy = get_available_proxy()
            if not proxy:
                last_error = "无可用代理"
                await asyncio.sleep(1)
                retry_count += 1
                continue

            try:
                timeout = aiohttp.ClientTimeout(total=10)
                connector = aiohttp.TCPConnector(ssl=ssl_context)
                async with aiohttp.ClientSession(
                    timeout=timeout, connector=connector
                ) as session:
                    async with session.get(url, proxy=proxy) as resp:
                        if resp.status == 200:
                            update_proxy_status(proxy, success=True)
                            return await resp.text(errors="replace")
                        else:
                            last_error = f"HTTP状态码 {resp.status}"
                            update_proxy_status(proxy, success=False)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                update_proxy_status(proxy, success=False)

            retry_count += 1
            await asyncio.sleep(0.5)

        # 本地代理全部失败后，尝试直连兜底。
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(
                timeout=timeout, connector=connector
            ) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        safe_print(f"⚠️ 代理失败，本地直连成功: {url}")
                        return await resp.text(errors="replace")
                    else:
                        last_error = f"本地直连 HTTP状态码 {resp.status}"
        except Exception as e:
            last_error = f"本地直连异常: {type(e).__name__}: {e}"

        # 代理与本地直连均失败，打印最终失败的链接与原因。
        safe_print(f"❌ 请求失败: {url} | 原因: {last_error}")
        return None  # 所有重试都失败

    async def process_domain(domain):
        nonlocal failure_counts, matched_domains

        async with semaphore:
            # 检查是否已经匹配成功
            async with lock:
                if domain in matched_domains:
                    return

            base = f"https://{domain}"

            # ========= 1. 访问 meta.json =========
            meta_url = base + "/meta.json"
            meta_text = await fetch_with_proxy(meta_url)

            matched = False

            if not meta_text:
                # meta.json 拿不到（目标无此资源或请求失败）
                # 记录失败次数
                async with lock:
                    current_count = failure_counts.get(domain, 0) + 1
                    failure_counts[domain] = current_count

                    if current_count >= 5:  # 只有失败5次及以上才加入数据库
                        add_failed_domain(domain)
                        if domain in failure_counts:
                            del failure_counts[domain]  # 清除计数记录
                # 不直接 return，跳过 Shopify 检测，尝试非 Shopify 电商检测
                # （目标可能是非 Shopify 电商站，没有 /meta.json）
            else:
                # 清除该域名的失败计数（因为这次成功了）
                async with lock:
                    if domain in failure_counts:
                        del failure_counts[domain]

                try:
                    meta_json = json.loads(meta_text)
                    description = meta_json.get("description", "")
                except Exception:
                    # JSON 解析失败，记录失败次数
                    async with lock:
                        current_count = failure_counts.get(domain, 0) + 1
                        failure_counts[domain] = current_count

                        if current_count >= 5:
                            add_failed_domain(domain)
                            if domain in failure_counts:
                                del failure_counts[domain]
                    # 不直接 return，跳过 Shopify 检测，尝试非 Shopify 电商检测
                    description = ""

                # ========= 2. description匹配 =========
                if description and keyword_match(description):
                    matched = True

                # ========= 3. sitemap.xml =========
                if not matched:
                    sitemap_url = base + "/sitemap.xml"
                    sitemap_text = await fetch_with_proxy(sitemap_url)

                    if sitemap_text:
                        try:
                            root = ET.fromstring(sitemap_text)
                            locs = [
                                elem.text for elem in root.iter() if "loc" in elem.tag
                            ]
                            # 找 collections_1
                            collection_maps = [
                                loc for loc in locs if "collections_1" in loc
                            ]
                            for cmap in collection_maps:
                                cmap_text = await fetch_with_proxy(cmap)
                                if not cmap_text:
                                    continue
                                cmap_root = ET.fromstring(cmap_text)
                                cmap_locs = [
                                    e.text for e in cmap_root.iter() if "loc" in e.tag
                                ]
                                for link in cmap_locs:
                                    if "/collections/" in link:
                                        keyword_part = link.split("/collections/")[-1]
                                        keyword_part = keyword_part.replace("-", " ")
                                        if keyword_match(keyword_part):
                                            matched = True
                                            break
                                if matched:
                                    break
                        except Exception:
                            pass

            if matched:
                async with lock:
                    success_domains.append(domain)
                    matched_domains.add(domain)
                    # 不再添加到成功域名数据库，只在本地记录
            else:
                # Shopify 未匹配，尝试检测非 Shopify 电商网站
                # 抓取首页 HTML，通过支付处理器指标判断
                home_url = base + "/"
                home_text = await fetch_with_proxy(home_url)
                if home_text:
                    is_ecom, indicator = await detect_non_shopify_ecommerce(
                        home_url, home_text
                    )
                    if is_ecom:
                        safe_print(f"    🔹 {domain} 检测为非Shopify电商 ({indicator})")
                        async with lock:
                            non_shopify_success_domains.append(domain)
                            matched_domains.add(domain)

    # ========= 并发执行 =========
    tasks = [process_domain(url) for url in urls]
    await asyncio.gather(*tasks)

    # ========= 写入 Shopify 文件 =========
    with open(file_path, "a+", encoding="utf-8") as f:
        for d in success_domains:
            f.write(d + "\n")

    # ========= 写入非 Shopify 文件 =========
    if non_shopify_file_path and non_shopify_success_domains:
        # 确保目录存在
        os.makedirs(os.path.dirname(non_shopify_file_path), exist_ok=True)
        with open(non_shopify_file_path, "a+", encoding="utf-8") as f:
            for d in non_shopify_success_domains:
                f.write(d + "\n")

    safe_print(
        f"✅ Shopify 匹配成功: {len(success_domains)} 个，"
        f"非Shopify电商: {len(non_shopify_success_domains)} 个\n"
    )

    return success_domains


def count_domains_in_file(file_path: str) -> int:
    """统计 txt 文件中已保存的域名行数（去空行）"""
    if not os.path.exists(file_path):
        return 0
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def load_visited_domains(file_path: str, non_shopify_file_path: str = "") -> set:
    """加载已访问域名集合（shopify txt + 非shopify txt + 数据库排除列表），统一走 standardt_domain 规范化。

    每次循环重新调用，确保上一轮写入 txt 的新域名被纳入排除列表。
    """
    visited = set()

    # 读取 shopify 分类文件中的域名（规范化后加入集合）
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    d = standardt_domain(line)
                    if d:
                        visited.add(d)
    except Exception as e:
        print(f"读取已采集文件失败: {e}")

    # 读取非 shopify 分类文件中的域名（规范化后加入集合）
    if non_shopify_file_path:
        try:
            if os.path.exists(non_shopify_file_path):
                with open(non_shopify_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        d = standardt_domain(line)
                        if d:
                            visited.add(d)
        except Exception as e:
            print(f"读取非Shopify文件失败: {e}")

    # 读取数据库中的排除域名（规范化后加入集合）
    try:
        for raw in get_all_domains_to_exclude():
            d = standardt_domain(raw)
            if d:
                visited.add(d)
    except Exception as e:
        print(f"读取数据库排除列表失败: {e}")

    return visited


def run_ai_search():
    """主流程：调用 Exa 搜索 -> 异步检测 Shopify -> 追加保存结果。
    程序运行期间累计匹配成功的域名数达到 target_count 则停止；
    否则重新加载排除集合后继续搜索，直到达标或连续无进展。
    """
    (
        keyword_links,
        match_keywords,
        fixed_keyword,
        file_path,
        target_count,
        non_shopify_file_path,
    ) = input_keywords()

    safe_print(f"目标域名数量(本次运行累计): {target_count}")

    total_success = 0  # 本次程序运行期间累计匹配成功的域名数
    round_idx = 0
    no_progress_count = 0  # 连续无进展轮次，防止无限循环
    MAX_NO_PROGRESS = 3  # 连续 3 轮无新增成功域名则退出

    # 跨轮累积的已访问域名集合：移到循环外，避免每轮重新赋值丢失上轮内存记录。
    # 初始加载一次（shopify txt + 非shopify txt + 数据库），
    # 后续每轮只追加加载新持久化的域名，保留本轮内存里“搜过但未持久化”的域名。
    visited_domains_global = load_visited_domains(file_path, non_shopify_file_path)

    while True:
        round_idx += 1
        # 每轮追加加载新持久化的域名（上一轮写入 txt 的），不覆盖内存累积
        newly_persisted = load_visited_domains(file_path, non_shopify_file_path)
        visited_domains_global |= newly_persisted
        safe_print(f"\n{'=' * 60}")
        safe_print(
            f"第 {round_idx} 轮搜索 | 已加载排除域名 {len(visited_domains_global)} 个 | "
            f"累计 {total_success}/{target_count} 个"
        )
        safe_print(f"{'=' * 60}")

        round_success = 0

        for idx, query in enumerate(keyword_links, 1):
            safe_print(f"\n[{idx}/{len(keyword_links)}] 开始搜索: {query}")

            # 1. 调用 Exa 搜索，传入已加载的访问集合（txt + 数据库）作为排除列表
            domains = single_api_search(query, visited_domains_global, max_retries=20)
            if not domains:
                safe_print(f"搜索 {query} 无结果，跳过")
                continue

            # 2. 过滤掉全局已访问的域名
            filtered_domains = [d for d in domains if d not in visited_domains_global]
            # 更新全局已访问集合（本轮后续 query 也会排除它们）
            visited_domains_global.update(domains)

            safe_print(
                f"本批搜索 {len(domains)} 个域名，过滤已访问后剩 {len(filtered_domains)} 个"
            )
            if not filtered_domains:
                safe_print("所有域名已访问过，跳过检测")
                continue

            # 3. 异步检测 Shopify 网站（10 并发），匹配成功的结果追加写入 txt
            try:
                success_domains = asyncio.run(
                    shopify_url(
                        filtered_domains,
                        match_keywords,
                        fixed_keyword,
                        file_path,
                        non_shopify_file_path,
                        max_concurrency=10,
                    )
                )
                round_success += len(success_domains)
                total_success += len(success_domains)
                safe_print(
                    f"本批新增 {len(success_domains)} 个，本轮 {round_success} 个，"
                    f"累计 {total_success}/{target_count} 个，已追加保存到: {file_path}"
                )
                # 累计达标立即跳出 query 循环
                if total_success >= target_count:
                    safe_print(f"累计已达目标 {target_count} 个，停止后续搜索")
                    break
            except Exception as e:
                safe_print(f"异步检测失败: {e}")

        # 一轮结束，判断累计是否达标
        safe_print(
            f"\n第 {round_idx} 轮结束，本轮新增 {round_success} 个，累计 {total_success} 个"
        )

        # 退出条件判断：本次运行累计成功数达到目标
        if total_success >= target_count:
            safe_print(
                f"累计匹配成功 {total_success} 个，已达到目标 {target_count} 个，停止搜索"
            )
            break

        if round_success == 0:
            no_progress_count += 1
            safe_print(f"连续 {no_progress_count}/{MAX_NO_PROGRESS} 轮无新增成功域名")
            if no_progress_count >= MAX_NO_PROGRESS:
                safe_print(
                    f"连续 {MAX_NO_PROGRESS} 轮无进展，停止搜索（可能已无更多匹配站点）"
                )
                break
        else:
            no_progress_count = 0  # 有进展则重置计数

    safe_print(f"\n{'=' * 60}")
    safe_print(
        f"全部搜索完成，共 {round_idx} 轮，本次运行累计匹配成功 {total_success} 个域名"
    )
    safe_print(f"结果文件: {file_path}")
    safe_print(f"{'=' * 60}")


if __name__ == "__main__":
    run_ai_search()
