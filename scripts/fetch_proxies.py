"""从 Webshare API 拉取代理列表并写入 proxies.txt

用法:
    # 通过本地 VPN/Clash 代理访问 webshare API（本机直连境外不通时）
    $env:HTTP_PROXY = "http://127.0.0.1:7897"
    $env:HTTPS_PROXY = "http://127.0.0.1:7897"
    python scripts/fetch_proxies.py

    # 本机可直连境外时
    python scripts/fetch_proxies.py

    # 自定义 token / 输出文件
    python scripts/fetch_proxies.py --token YOUR_TOKEN --output proxies.txt

说明:
    - Token 从 --token 参数或环境变量 WEBSHARE_TOKEN 读取（避免硬编码到文件）。
    - 自动分页拉取全部代理，仅写入 valid=true 的条目。
    - 覆盖前自动备份原 proxies.txt 为 proxies.txt.bak.<timestamp>。
"""

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROXIES_FILE = PROJECT_ROOT / "proxies.txt"

WEBSHARE_API = "https://proxy.webshare.io/api/v2/proxy/list/"
DEFAULT_PAGE_SIZE = 100


def fetch_all_proxies(token: str, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """分页拉取全部代理，返回 results 列表"""
    all_results: list[dict] = []
    page = 1
    headers = {"Authorization": token}
    while True:
        params = {"mode": "direct", "page": page, "page_size": page_size}
        print(f"  拉取第 {page} 页 (page_size={page_size})...", end=" ")
        try:
            resp = requests.get(WEBSHARE_API, headers=headers, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"FAIL")
            print(f"  [错误] 请求失败: {e}", file=sys.stderr)
            print(f"  提示: 若本机无法直连境外，请设置 HTTP_PROXY/HTTPS_PROXY 环境变量", file=sys.stderr)
            print(f"        例如: $env:HTTPS_PROXY = 'http://127.0.0.1:7897'", file=sys.stderr)
            sys.exit(1)
        if resp.status_code != 200:
            print(f"FAIL")
            print(f"  [错误] HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            if resp.status_code == 401:
                print(f"  提示: token 无效或过期，请检查 WEBSHARE_TOKEN", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        results = data.get("results", [])
        all_results.extend(results)
        total = data.get("count", len(all_results))
        print(f"OK (本页 {len(results)} 个, 累计 {len(all_results)}/{total})")
        if not data.get("next"):
            break
        page += 1
    return all_results


def format_proxy_url(item: dict) -> str:
    """将 API 返回的代理对象格式化为 http://user:pass@ip:port"""
    user = item["username"]
    pw = item["password"]
    ip = item["proxy_address"]
    port = item["port"]
    return f"http://{user}:{pw}@{ip}:{port}"


def main():
    default_token = os.getenv("WEBSHARE_TOKEN", "")
    parser = argparse.ArgumentParser(description="从 Webshare 拉取代理并写入 proxies.txt")
    parser.add_argument("--token", "-t", default=default_token, help="Webshare API token（或设环境变量 WEBSHARE_TOKEN）")
    parser.add_argument("--output", "-o", default=str(PROXIES_FILE), help=f"输出文件（默认 {PROXIES_FILE}）")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help=f"每页数量（默认 {DEFAULT_PAGE_SIZE}）")
    parser.add_argument("--no-backup", action="store_true", help="覆盖前不创建备份")
    parser.add_argument("--dry-run", action="store_true", help="只拉取不写入文件")
    args = parser.parse_args()

    token = args.token
    if not token:
        print("[错误] 未提供 token。请使用 --token 参数或设置环境变量 WEBSHARE_TOKEN", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)

    print("=" * 60)
    print("Webshare 代理拉取")
    print("=" * 60)
    proxy_env = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""
    print(f"代理中转 : {proxy_env or '无（直连）'}")
    print(f"输出文件 : {output_path}")
    print(f"模式     : {'仅拉取（不写入）' if args.dry_run else '拉取 + 覆盖'}")
    print("=" * 60)

    # 1. 拉取
    print("\n[1/2] 拉取代理列表...")
    proxies = fetch_all_proxies(token, page_size=args.page_size)

    valid = [p for p in proxies if p.get("valid")]
    invalid = [p for p in proxies if not p.get("valid")]
    print(f"\n  总计 {len(proxies)} 个, 有效 {len(valid)} 个, 无效 {len(invalid)} 个")

    if not valid:
        print("[错误] 没有有效代理，不写入文件", file=sys.stderr)
        sys.exit(2)

    # 统计凭证
    creds = {(p["username"], p["password"]) for p in valid}
    print(f"  凭证组数: {len(creds)} 组")
    for u, pw in creds:
        print(f"    {u}:{pw}")

    # 2. 写入
    lines = [format_proxy_url(p) for p in valid]
    print(f"\n[2/2] 写入 {len(lines)} 个代理到 {output_path}")

    if args.dry_run:
        print("  [DRY-RUN] 跳过写入")
        print("\n前 5 个代理示例:")
        for ln in lines[:5]:
            print(f"  {ln}")
        return

    if not args.no_backup and output_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = output_path.with_suffix(f".txt.bak.{ts}")
        shutil.copy2(output_path, bak)
        print(f"  [备份] 原文件已备份到: {bak}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [完成] 已写入 {len(lines)} 个代理")

    print(f"\n提示: 运行 `python scripts/check_proxies.py` 验证代理可用性")


if __name__ == "__main__":
    main()
