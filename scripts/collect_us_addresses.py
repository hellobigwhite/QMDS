# -*- coding: utf-8 -*-
"""采集美国真实住宅地址（OpenStreetMap / Overpass），供网站信息生成的地址字段使用

需求背景：地址必须对应真实存在的房屋（能在 Google/OSM 地图上查到），
不能让模型凭空编造。做法是先从 OSM 采集「带门牌号的住宅建筑」地址，
落库到 data/us_addresses.json，生成时随机抽取并格式化为
"516 16th St NW, Albuquerque, NM 87104" 这样的美式写法。

用法:
    python scripts/collect_us_addresses.py                      # 采集全部城市
    python scripts/collect_us_addresses.py --cities "Denver, CO" "Chicago, IL"
    python scripts/collect_us_addresses.py --limit 150          # 每城最多条数
    python scripts/collect_us_addresses.py --refresh            # 忽略已采集的城市
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests

from qmds.modules.web.services.site_info_generator import _ADDRESS_CITIES

# Windows 控制台默认 GBK，中文/符号直接 print 会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "us_addresses.json"

PROXY_FALLBACK = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# 多个 Overpass 镜像轮换：单台服务器排队时（504）自动换下一台，明显提速
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)
_ovp_state = {"i": 0}

# 只收住宅类建筑（用户要求：必须对应到房屋，不要商业楼/大马路）
RESIDENTIAL_BUILDINGS = ("house|residential|apartments|detached|semidetached_house"
                         "|terrace|bungalow|dormitory|houseboat")

# 方向词：出现在任何位置都缩写（1904 South York Street -> 1904 S York St）
DIRECTIONS = {
    "north": "N", "south": "S", "east": "E", "west": "W",
    "northeast": "NE", "northwest": "NW", "southeast": "SE", "southwest": "SW",
}

# 路型词：只缩写「最后一个词」，否则会把街名本身缩写坏
# （Denver 的 "Court Place" 曾被缩成 "Ct Pl"）
STREET_TYPES = {
    "street": "St", "avenue": "Ave", "boulevard": "Blvd", "drive": "Dr",
    "road": "Rd", "lane": "Ln", "court": "Ct", "place": "Pl", "terrace": "Ter",
    "circle": "Cir", "parkway": "Pkwy", "highway": "Hwy", "square": "Sq",
    "trail": "Trl", "way": "Way", "walk": "Walk", "loop": "Loop", "row": "Row",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = PROXY_FALLBACK
    s.headers.update({"User-Agent": "QMDS-address-collector/1.0 (internal build tool)"})
    return s


def abbreviate(street: str) -> str:
    """把 OSM 全称街道名转成美式缩写（1904 South York Street -> 1904 S York St）

    方向词任意位置都缩写；路型词只缩写最后一个词，避免把街名缩写坏
    （"2300 Court Place" 应为 "2300 Court Pl"，而不是 "2300 Ct Pl"）。
    """
    words = str(street).split()
    last = len(words) - 1
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if low in DIRECTIONS:
            out.append(DIRECTIONS[low])
        elif low in STREET_TYPES and i == last:
            out.append(STREET_TYPES[low])
        else:
            out.append(w)
    return " ".join(out)


def city_bbox(session, city: str, state: str):
    """用 Nominatim 取城市边界框"""
    try:
        r = session.get(NOMINATIM, params={
            "q": f"{city}, {state}, USA", "format": "json", "limit": 1}, timeout=40)
        data = r.json()
        if not data:
            return None
        bb = data[0].get("boundingbox")
        if not bb or len(bb) != 4:
            return None
        return tuple(float(x) for x in bb)      # (south, north, west, east)
    except Exception as e:
        print(f"    bbox 获取失败: {type(e).__name__}: {str(e)[:80]}")
        return None


def _overpass(session, query, label, tries: int = 3):
    """执行 Overpass 查询：镜像轮换 + 504/超时重试"""
    for attempt in range(tries):
        url = OVERPASS_ENDPOINTS[_ovp_state["i"] % len(OVERPASS_ENDPOINTS)]
        _ovp_state["i"] += 1
        try:
            r = session.post(url, data={"data": query}, timeout=180)
            if r.status_code == 200:
                return r.json().get("elements", [])
            print(f"    {label} HTTP {r.status_code} @{url.split('/')[2]}（第 {attempt + 1} 次）")
        except Exception as e:
            print(f"    {label} {type(e).__name__} @{url.split('/')[2]}（第 {attempt + 1} 次）")
        time.sleep(4 * (attempt + 1))
    return []


def fetch_addresses(session, bbox, parts: int, limit: int) -> list:
    """把城市 bbox 切成 parts×parts 网格分片查询住宅地址

    整块 bbox 一次查容易 504（实测 Chicago 连续 3 次网关超时），分片后单次
    负担小、成功率明显提高。每片按需取数，凑够 limit 就停。
    """
    south, north, west, east = bbox
    out, per_part = [], max(40, limit)
    for gy in range(parts):
        for gx in range(parts):
            s = south + (north - south) * gy / parts
            n = south + (north - south) * (gy + 1) / parts
            w = west + (east - west) * gx / parts
            e = west + (east - west) * (gx + 1) / parts
            # 只查 way：住宅门牌挂在建筑多边形上，node 基本是商业 POI 且耗时翻倍
            q = (f'[out:json][timeout:120];'
                 f'way["addr:housenumber"]["addr:street"]'
                 f'["building"~"^({RESIDENTIAL_BUILDINGS})$"]'
                 f'({s:.5f},{w:.5f},{n:.5f},{e:.5f});out center {per_part};')
            out.extend(_overpass(session, q, f"片{gy}{gx}", tries=2))
            time.sleep(1.5)                      # 遵守 Overpass 使用政策
            if len([e for e in out]) >= limit * 4:
                return out
    return out


def normalize(elements, city: str, state: str, limit: int):
    """清洗：格式化门牌/街道、校验城市与 ZIP、去重

    只保留 OSM 标注了 addr:city 且与目标城市一致、且带 5 位 ZIP 的地址。
    bbox 查询会带进紧邻卫星城的地址（Chicago bbox 里采到 Oak Park 的 60302），
    如果不校验，生成出来的就是「城市与邮编对不上」的假地址。
    """
    target = city.strip().lower()
    seen, rows = set(), []
    for e in elements:
        t = e.get("tags", {})
        num = str(t.get("addr:housenumber", "")).strip().split(";")[0].strip()
        # "10811-10819" 是整排房屋的号段，取起始号才是单个房屋地址
        if "-" in num:
            num = num.split("-")[0].strip()
        street = abbreviate(t.get("addr:street", ""))
        zipcode = str(t.get("addr:postcode", "")).strip()
        osm_city = str(t.get("addr:city", "")).strip()
        if not num or not street:
            continue
        if not zipcode.isdigit() or len(zipcode) != 5:
            continue                             # 缺 ZIP 丢弃，避免不完整地址
        if osm_city.lower() != target:
            continue                             # 城市与目标不符（卫星城）丢弃
        key = (num.lower(), street.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append([f"{num} {street}", osm_city, zipcode])
        if len(rows) >= limit:
            break
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", nargs="*", help='只采集指定城市，如 --cities "Denver, CO"')
    ap.add_argument("--limit", type=int, default=150, help="每城最多采集条数")
    ap.add_argument("--parts", type=int, default=3, help="城市 bbox 分片数（parts×parts 网格）")
    ap.add_argument("--min-ok", type=int, default=30, help="已有多少条才算采够、不必重采")
    ap.add_argument("--refresh", action="store_true", help="忽略已有数据重新采集")
    args = ap.parse_args()

    session = _session()
    store = {}
    if OUT_PATH.exists() and not args.refresh:
        try:
            store = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            store = {}

    targets = _ADDRESS_CITIES
    if args.cities:
        wanted = {c.strip().lower() for c in args.cities}
        targets = [t for t in _ADDRESS_CITIES if f"{t[0]}, {t[1]}".lower() in wanted]
        if not targets:
            print("未匹配到城市，请检查 --cities 参数")
            return 1

    print(f"待采集城市: {len(targets)} 个，每城上限 {args.limit} 条")
    for city, state in targets:
        key = f"{city}|{state}"
        existing = store.get(key) or []
        # 数据够多才跳过；不足的城市（分片没覆盖到）要重采并合并，
        # 否则那些城市永远补不上数据
        if len(existing) >= args.min_ok and not args.refresh:
            print(f"  [skip] {city}, {state}: 已有 {len(existing)} 条")
            continue
        if existing:
            print(f"  [redo] {city}, {state}: 仅 {len(existing)} 条，重采")
        bbox = city_bbox(session, city, state)
        if not bbox:
            print(f"  [fail] {city}, {state}: 未取到边界框")
            continue
        time.sleep(1.2)                          # Nominatim 限速
        parts = args.parts
        # 面积大的城市（洛杉矶、达拉斯这类）2×2 分片覆盖不到，自动加密网格
        span = (bbox[1] - bbox[0]) * (bbox[3] - bbox[2])
        if span > 0.05:
            parts = max(parts, 4)
        rows = normalize(fetch_addresses(session, bbox, parts, args.limit),
                         city, state, args.limit)
        if rows:
            if existing:
                # 合并去重（街道 + 城市 + ZIP 作为键）
                merged = {tuple(r) for r in existing} | {tuple(r) for r in rows}
                rows = [list(r) for r in sorted(merged)]
            store[key] = rows
            print(f"  [ok] {city}, {state}: {len(rows)} 条真实住宅地址")
        else:
            print(f"  [fail] {city}, {state}: 未采到住宅地址")
        # 原子写：先写临时文件再替换，避免正在运行的服务读到半个 JSON
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = OUT_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp_path.replace(OUT_PATH)

    total = sum(len(v) for v in store.values())
    print(f"\n完成：{len(store)} 个城市，共 {total} 条真实地址 -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
