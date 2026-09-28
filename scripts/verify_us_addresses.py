# -*- coding: utf-8 -*-
"""核验真实地址库：地址能否在地图上定位到真实房屋

对 data/us_addresses.json 里的地址做随机抽检，用 OpenStreetMap Nominatim
反查坐标与门牌号，并给出可直接点开的 Google 地图链接。

用法:
    python scripts/verify_us_addresses.py                 # 随机抽检 20 条
    python scripts/verify_us_addresses.py -n 50           # 抽检 50 条
    python scripts/verify_us_addresses.py --city "Denver, CO" -n 10
    python scripts/verify_us_addresses.py --csv .tmp/addr_check.csv
"""

import argparse
import csv
import json
import random
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDR_PATH = ROOT / "data" / "us_addresses.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

NOMINATIM = "https://nominatim.openstreetmap.org/search"
PROXY_FALLBACK = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}


def google_maps_url(addr: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(addr)


def load_samples(city_filter: str, count: int):
    store = json.loads(ADDR_PATH.read_text(encoding="utf-8"))
    rows = []
    for key in sorted(store):
        city, state = key.split("|")
        if city_filter and city_filter.strip().lower() != f"{city}, {state}".lower():
            continue
        for street, osm_city, zipcode in store[key]:
            rows.append(f"{street}, {osm_city}, {state} {zipcode}")
    if not rows:
        return []
    random.shuffle(rows)
    return rows[:count]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=20, help="抽检条数")
    ap.add_argument("--city", default="", help='只查某个城市，如 --city "Denver, CO"')
    ap.add_argument("--csv", default="", help="把结果导出为 CSV")
    ap.add_argument("--sleep", type=float, default=1.1, help="请求间隔（Nominatim 限速 1 req/s）")
    args = ap.parse_args()

    if not ADDR_PATH.exists():
        print(f"地址库不存在: {ADDR_PATH}（先跑 scripts/collect_us_addresses.py）")
        return 1
    samples = load_samples(args.city, args.count)
    if not samples:
        print("没有匹配的地址可抽检")
        return 1

    session = requests.Session()
    session.trust_env = False
    session.proxies = PROXY_FALLBACK
    session.headers.update({"User-Agent": "QMDS-address-verify/1.0"})

    ok, results = 0, []
    print(f"抽检 {len(samples)} 条地址\n")
    for addr in samples:
        hit, detail, coords = False, "", ""
        try:
            r = session.get(NOMINATIM, params={
                "q": addr, "format": "json", "limit": 1, "addressdetails": 1}, timeout=40)
            data = r.json()
            if data:
                it = data[0]
                a = it.get("address", {})
                hit = True
                ok += 1
                detail = f"{it.get('type')} | {a.get('house_number', '')} {a.get('road', '')}"
                coords = f"{it.get('lat')},{it.get('lon')}"
        except Exception as e:
            detail = f"请求失败 {type(e).__name__}"
        flag = "[OK]  " if hit else "[MISS]"
        print(f"{flag} {addr}")
        if hit:
            print(f"       {detail}  @{coords}")
            print(f"       {google_maps_url(addr)}")
        else:
            print(f"       {detail}")
        results.append({"address": addr, "locatable": hit, "detail": detail,
                        "coords": coords, "google_maps": google_maps_url(addr)})
        time.sleep(args.sleep)

    print(f"\n可定位 {ok}/{len(samples)}")
    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["address", "locatable", "detail", "coords", "google_maps"])
            w.writeheader()
            w.writerows(results)
        print(f"结果已导出: {out}")
    return 0 if ok == len(samples) else 2


if __name__ == "__main__":
    raise SystemExit(main())
