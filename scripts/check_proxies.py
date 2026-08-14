"""代理可用性验证脚本

从 proxies.txt 读取全部代理，并发向 Shopify 站点发起测试请求，
打印每个不可用代理及失败原因，最后备份原文件并用可用代理覆盖 proxies.txt。

用法:
    python scripts/check_proxies.py
    python scripts/check_proxies.py --target https://www.allbirds.com
    python scripts/check_proxies.py --workers 30 --timeout 12 --dry-run
"""

import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 复用项目 UA（与 src/qmds/modules/data_scraper/product_crawler.py:32 一致）
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROXIES_FILE = PROJECT_ROOT / "proxies.txt"

# 默认测试目标：知名 Shopify 店铺的轻量端点（项目实际使用模式）
DEFAULT_TARGET = "https://www.allbirds.com/products.json?limit=1"


def load_proxies(path: Path) -> list[str]:
    """读取代理列表（兼容两种格式，与 settings.load_proxies 一致）"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            result.append(line)
            continue
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, user, pw = parts
            result.append(f"http://{user}:{pw}@{ip}:{port}")
    return result


def test_proxy(proxy_url: str, target: str, timeout: int) -> tuple[bool, str]:
    """测试单个代理对目标站点的可用性。返回 (是否可用, 原因/出口IP)"""
    import random

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            target,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            verify=False,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            # 进一步确认响应是 JSON（Shopify products.json 特征）
            ctype = resp.headers.get("Content-Type", "").lower()
            if "json" in ctype or resp.text.lstrip().startswith(("{", "[")):
                try:
                    data = resp.json()
                    n = len(data.get("products", [])) if isinstance(data, dict) else 0
                    return True, f"OK (products={n})"
                except ValueError:
                    return True, "OK (HTTP 200, 非 JSON 但可连通)"
            return True, "OK (HTTP 200)"
        else:
            return False, f"HTTP {resp.status_code}"
    except requests.exceptions.ProxyError as e:
        return False, f"ProxyError: {e}"
    except requests.exceptions.ConnectTimeout:
        return False, "ConnectTimeout"
    except requests.exceptions.ReadTimeout:
        return False, "ReadTimeout"
    except requests.exceptions.SSLError as e:
        return False, f"SSLError: {e}"
    except requests.exceptions.ConnectionError as e:
        return False, f"ConnectionError: {e}"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_target_reachable(target: str, timeout: int = 15) -> bool:
    """直连测试目标站点是否可达（避免目标本身不可用导致误判）"""
    import random

    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(target, headers=headers, timeout=timeout, verify=False)
        return resp.status_code == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="代理可用性验证")
    parser.add_argument("--target", "-t", default=DEFAULT_TARGET, help=f"测试目标 URL（默认 {DEFAULT_TARGET}）")
    parser.add_argument("--workers", "-w", type=int, default=20, help="并发线程数（默认 20）")
    parser.add_argument("--timeout", type=int, default=15, help="单代理超时秒数（默认 15）")
    parser.add_argument("--dry-run", action="store_true", help="只测试不覆盖 proxies.txt")
    parser.add_argument("--no-backup", action="store_true", help="覆盖前不创建备份")
    args = parser.parse_args()

    if not PROXIES_FILE.exists():
        print(f"[错误] 代理文件不存在: {PROXIES_FILE}", file=sys.stderr)
        sys.exit(1)

    proxies = load_proxies(PROXIES_FILE)
    total = len(proxies)
    if total == 0:
        print("[错误] proxies.txt 为空或无有效代理", file=sys.stderr)
        sys.exit(1)

    print(f"=" * 70)
    print(f"代理可用性验证")
    print(f"=" * 70)
    print(f"代理文件    : {PROXIES_FILE}")
    print(f"代理总数    : {total}")
    print(f"测试目标    : {args.target}")
    print(f"并发线程    : {args.workers}")
    print(f"超时设置    : {args.timeout}s")
    print(f"模式        : {'仅测试（不覆盖）' if args.dry_run else '测试 + 覆盖 proxies.txt'}")
    print(f"=" * 70)

    # 1. 直连验证目标可达
    print("\n[1/3] 直连测试目标站点可达性...")
    if not check_target_reachable(args.target, timeout=20):
        print(f"  [警告] 目标站点直连失败，仍将继续测试代理（部分代理可能因目标不可达而失败）")
    else:
        print(f"  [OK] 目标站点直连正常")

    # 2. 并发测试所有代理
    print(f"\n[2/3] 并发测试 {total} 个代理（{args.workers} 线程）...")
    results: dict[str, tuple[bool, str]] = {}
    done = 0
    start_ts = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(test_proxy, p, args.target, args.timeout): p for p in proxies}
        for fut in as_completed(futures):
            proxy_url = futures[fut]
            try:
                ok, reason = fut.result()
            except Exception as e:
                ok, reason = False, f"TaskError: {e}"
            results[proxy_url] = (ok, reason)
            done += 1
            status = "OK " if ok else "FAIL"
            print(f"  [{done:>3}/{total}] {status}  {proxy_url}  -> {reason}")
    elapsed = time.time() - start_ts

    # 3. 汇总
    ok_proxies = [p for p, (ok, _) in results.items() if ok]
    bad_proxies = [(p, r) for p, (ok, r) in results.items() if not ok]
    ok_count = len(ok_proxies)
    bad_count = len(bad_proxies)

    print(f"\n[3/3] 验证完成（耗时 {elapsed:.1f}s）")
    print(f"=" * 70)
    print(f"汇总: 可用 {ok_count}/{total}  |  不可用 {bad_count}/{total}")
    print(f"可用率: {ok_count / total * 100:.1f}%")
    print(f"=" * 70)

    if bad_proxies:
        print(f"\n不可用代理明细（{bad_count} 个）:")
        print("-" * 70)
        for i, (p, r) in enumerate(bad_proxies, 1):
            print(f"  {i:>3}. {p}")
            print(f"       原因: {r}")
        print("-" * 70)

    if ok_count == 0:
        print("\n[警告] 没有可用代理，不修改 proxies.txt")
        sys.exit(2)

    # 4. 覆盖 proxies.txt
    if args.dry_run:
        print(f"\n[DRY-RUN] 跳过覆盖 proxies.txt（可用代理 {ok_count} 个）")
    else:
        if not args.no_backup:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = PROXIES_FILE.with_suffix(f".txt.bak.{ts}")
            shutil.copy2(PROXIES_FILE, bak)
            print(f"\n[备份] 原文件已备份到: {bak}")

        PROXIES_FILE.write_text("\n".join(ok_proxies) + "\n", encoding="utf-8")
        print(f"[覆盖] proxies.txt 已更新: {ok_count} 个可用代理")

    print(f"\n完成。")


if __name__ == "__main__":
    main()
