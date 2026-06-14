import argparse
import csv
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests
import dns.resolver
import dns.exception


MIN_LABEL_LEN = 8
MAX_LABEL_LEN = 11
DEFAULT_WORKERS = 5
WHOIS_SERVER = "whois.verisign-grs.com"
RDAP_URL_TEMPLATE = "https://rdap.verisign.com/com/v1/domain/{domain}"
DNS_TYPES = ("A", "AAAA", "CNAME", "NS")
REQUEST_TIMEOUT = 8

DOMAIN_ALIASES = {"domain", "域名", "原域名"}
CATEGORY_ALIASES = {"category", "分类", "大类", "品类"}

GENERIC_WORDS = {
    "shop", "store", "mall", "hub", "zone", "world", "online", "official",
    "site", "home", "group", "mart", "global", "center", "club",
}

CATEGORY_TOKEN_MAP: Dict[str, List[str]] = {
    "beauty": ["beauty", "skin", "glow", "care", "cos"],
    "保健": ["beauty", "skin", "glow", "care", "cos"],
    "sports": ["sport", "fit", "gear", "run", "gym"],
    "体育用品": ["sport", "fit", "gear", "run", "gym"],
    "electronics": ["tech", "volt", "wire", "digi", "byte"],
    "电子产品": ["tech", "volt", "wire", "digi", "byte"],
    "toy": ["toys", "play", "kids", "fun", "game"],
    "玩具": ["toys", "play", "kids", "fun", "game"],
    "bag": ["bags", "pack", "carry", "tote", "case"],
    "箱包": ["bags", "pack", "carry", "tote", "case"],
    "camera": ["cam", "lens", "optic", "shot", "photo"],
    "相机与光学器件": ["cam", "lens", "optic", "shot", "photo"],
    "office": ["desk", "paper", "work", "file", "note"],
    "办公用品": ["desk", "paper", "work", "file", "note"],
    "pet": ["pets", "paws", "tail", "fur", "woof"],
    "动物": ["pets", "paws", "tail", "fur", "woof"],
    "furniture": ["furn", "home", "nest", "sofa", "wood"],
    "家具": ["furn", "home", "nest", "sofa", "wood"],
    "家居与园艺": ["home", "nest", "deco", "yard", "grow"],
    "art_entertainment": ["art", "show", "media", "scene", "craft"],
    "艺术与娱乐": ["art", "show", "media", "scene", "craft"],
    "media": ["media", "audio", "video", "press", "stream"],
    "媒体": ["media", "audio", "video", "press", "stream"],
    "auto": ["auto", "ride", "motor", "parts", "wheel"],
    "交通工具": ["auto", "ride", "motor", "parts", "wheel"],
    "liquor": ["drink", "brew", "wine", "bar", "sip"],
    "饮食": ["food", "meal", "cook", "bite", "dish"],
    "adult": ["adult", "intim", "secret", "desire", "night"],
    "成人": ["adult", "intim", "secret", "desire", "night"],
    "服饰与配饰": ["style", "wear", "dress", "look", "chic"],
    "software": ["soft", "code", "cloud", "apps", "stack"],
    "软件": ["soft", "code", "cloud", "apps", "stack"],
    "商业": ["biz", "trade", "sale", "deal", "mart"],
    "宗教": ["faith", "grace", "holy", "spirit", "pray"],
}


@dataclass
class DomainCheckResult:
    domain: str
    whois_status: str
    rdap_status: str
    dns_status: str
    summary_status: str
    notes: str


def normalize_domain(text: str) -> str:
    value = str(text or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = parsed.netloc or parsed.path
    if host.startswith("www."):
        host = host[4:]
    return host


def extract_label(domain: str) -> str:
    host = normalize_domain(domain)
    if host.endswith(".com"):
        return host[:-4]
    return host.split(".")[0]


def sanitize_label(label: str) -> str:
    return "".join(ch for ch in str(label or "").lower() if ch.isalnum())


def fit_compound(stem: str, token: str) -> List[str]:
    stem = sanitize_label(stem)
    token = sanitize_label(token)
    if not stem or not token:
        return []

    candidates = []
    for left, right in ((stem, token), (token, stem)):
        max_left = max(1, MAX_LABEL_LEN - len(right))
        min_left = max(1, MIN_LABEL_LEN - len(right))
        if min_left > len(left):
            continue
        clipped = left[: min(len(left), max_left)]
        if len(clipped) < min_left:
            continue
        label = f"{clipped}{right}"
        if MIN_LABEL_LEN <= len(label) <= MAX_LABEL_LEN:
            candidates.append(label)
    return candidates


def category_tokens(category: str) -> List[str]:
    key = str(category or "").strip()
    if not key:
        return ["brand", "goods", "trade"]

    tokens = []
    for map_key, values in CATEGORY_TOKEN_MAP.items():
        if map_key.lower() == key.lower():
            tokens.extend(values)

    if not tokens:
        raw = sanitize_label(key.replace("_", "").replace(" ", ""))
        if raw:
            tokens.append(raw[:5])
            tokens.append(raw[-4:])

    seen = set()
    result = []
    for token in tokens:
        token = sanitize_label(token)
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def derive_stem(domain: str, category: str) -> str:
    label = sanitize_label(extract_label(domain))
    if not label:
        return ""

    removable = set(category_tokens(category)) | GENERIC_WORDS
    stem = label
    for token in sorted(removable, key=len, reverse=True):
        if token and token in stem and len(stem) - len(token) >= 3:
            stem = stem.replace(token, "", 1)

    stem = sanitize_label(stem)
    if len(stem) < 3:
        stem = label[: max(3, min(5, len(label)))]
    return stem


def build_candidates(domain: str, category: str, limit: int = 20) -> List[str]:
    original = sanitize_label(extract_label(domain))
    stem = derive_stem(domain, category)
    tokens = category_tokens(category)

    candidates = []
    seen = set()
    for token in tokens:
        for label in fit_compound(stem, token):
            if label == original:
                continue
            if label not in seen:
                seen.add(label)
                candidates.append(f"{label}.com")
        if len(candidates) >= limit:
            break

    if len(candidates) < limit:
        for token in tokens:
            for extra in ("go", "my", "e", "i"):
                label = sanitize_label(f"{extra}{stem}{token}")
                if label == original:
                    continue
                if MIN_LABEL_LEN <= len(label) <= MAX_LABEL_LEN and label not in seen:
                    seen.add(label)
                    candidates.append(f"{label}.com")
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break

    return candidates[:limit]


def whois_lookup(domain: str) -> Tuple[str, str]:
    try:
        with socket.create_connection((WHOIS_SERVER, 43), timeout=REQUEST_TIMEOUT) as sock:
            sock.sendall((domain + "\r\n").encode("utf-8"))
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        text = b"".join(chunks).decode("utf-8", errors="ignore")
        upper_text = text.upper()
        if "NO MATCH FOR" in upper_text or "NOT FOUND" in upper_text:
            return "available", "WHOIS 未发现注册信息"
        if "DOMAIN NAME:" in upper_text:
            return "registered", "WHOIS 显示已注册"
        return "uncertain", "WHOIS 返回结果不明确"
    except Exception as exc:
        return "uncertain", f"WHOIS 查询失败: {exc}"


def rdap_lookup(domain: str) -> Tuple[str, str]:
    url = RDAP_URL_TEMPLATE.format(domain=domain)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return "available", "RDAP 404 未发现注册信息"
        if resp.status_code == 200:
            return "registered", "RDAP 显示已注册"
        return "uncertain", f"RDAP 返回状态码 {resp.status_code}"
    except Exception as exc:
        return "uncertain", f"RDAP 查询失败: {exc}"


def dns_lookup(domain: str) -> Tuple[str, str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = REQUEST_TIMEOUT
    has_record = False
    uncertain = False
    details = []

    for record_type in DNS_TYPES:
        try:
            answers = resolver.resolve(domain, record_type)
            if answers:
                has_record = True
                details.append(f"{record_type} 有记录")
                break
        except dns.resolver.NXDOMAIN:
            details.append(f"{record_type} NXDOMAIN")
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            details.append(f"{record_type} 无记录")
        except dns.exception.Timeout:
            uncertain = True
            details.append(f"{record_type} 超时")
        except Exception as exc:
            uncertain = True
            details.append(f"{record_type} 异常:{type(exc).__name__}")

    if has_record:
        return "has_records", "; ".join(details)
    if uncertain:
        return "uncertain", "; ".join(details)
    return "no_records", "; ".join(details)


def check_domain(domain: str) -> DomainCheckResult:
    normalized = normalize_domain(domain)
    if not normalized or not normalized.endswith(".com"):
        return DomainCheckResult(
            domain=normalized or str(domain or ""),
            whois_status="invalid",
            rdap_status="invalid",
            dns_status="invalid",
            summary_status="invalid",
            notes="只支持 .com 域名",
        )

    whois_status, whois_note = whois_lookup(normalized)
    rdap_status, rdap_note = rdap_lookup(normalized)
    dns_status, dns_note = dns_lookup(normalized)

    if whois_status == "available" and rdap_status == "available" and dns_status == "no_records":
        summary = "available"
    elif "registered" in {whois_status, rdap_status} or dns_status == "has_records":
        summary = "registered"
    else:
        summary = "uncertain"

    notes = " | ".join([whois_note, rdap_note, dns_note])
    return DomainCheckResult(
        domain=normalized,
        whois_status=whois_status,
        rdap_status=rdap_status,
        dns_status=dns_status,
        summary_status=summary,
        notes=notes,
    )


def read_input_rows(input_path: str) -> List[Dict[str, str]]:
    path = Path(input_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                parts = [part.strip() for part in line.split(",", 1)]
                rows.append(
                    {
                        "domain": parts[0],
                        "category": parts[1] if len(parts) > 1 else "",
                    }
                )
        return rows

    if suffix in {".csv", ".xlsx", ".xls"}:
        if suffix == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
        normalized_map = {str(col).strip().lower(): col for col in df.columns}
        domain_col = next((normalized_map[name] for name in normalized_map if name in DOMAIN_ALIASES), None)
        category_col = next((normalized_map[name] for name in normalized_map if name in CATEGORY_ALIASES), None)
        if domain_col is None:
            raise ValueError("输入文件缺少域名列")
        rows = []
        for _, row in df.iterrows():
            domain = str(row.get(domain_col, "") or "").strip()
            category = str(row.get(category_col, "") or "").strip() if category_col else ""
            if domain:
                rows.append({"domain": domain, "category": category})
        return rows

    raise ValueError("仅支持 txt / csv / xlsx 输入")


def check_and_suggest(row: Dict[str, str], max_candidates: int = 20) -> Dict[str, str]:
    original_domain = normalize_domain(row.get("domain", ""))
    category = str(row.get("category", "") or "").strip()
    original_result = check_domain(original_domain)

    output = {
        "原域名": original_domain,
        "品类": category,
        "原域名_WHOIS": original_result.whois_status,
        "原域名_RDAP": original_result.rdap_status,
        "原域名_DNS": original_result.dns_status,
        "原域名结果": original_result.summary_status,
        "建议域名": "",
        "建议域名_WHOIS": "",
        "建议域名_RDAP": "",
        "建议域名_DNS": "",
        "建议域名结果": "",
        "尝试候选数": 0,
        "候选列表": "",
        "备注": original_result.notes,
    }

    if original_result.summary_status == "available":
        output["建议域名"] = original_domain
        output["建议域名_WHOIS"] = original_result.whois_status
        output["建议域名_RDAP"] = original_result.rdap_status
        output["建议域名_DNS"] = original_result.dns_status
        output["建议域名结果"] = "keep_original"
        return output

    candidates = build_candidates(original_domain, category, limit=max_candidates)
    output["尝试候选数"] = len(candidates)
    output["候选列表"] = ", ".join(candidates)

    for candidate in candidates:
        result = check_domain(candidate)
        if result.summary_status == "available":
            output["建议域名"] = candidate
            output["建议域名_WHOIS"] = result.whois_status
            output["建议域名_RDAP"] = result.rdap_status
            output["建议域名_DNS"] = result.dns_status
            output["建议域名结果"] = "auto_replaced"
            output["备注"] = f"{original_result.notes} | 已找到可注册候选: {candidate}"
            return output

    output["建议域名结果"] = "manual_review"
    output["备注"] = f"{original_result.notes} | 未找到满足三重检测都为空的候选"
    return output


def run_batch(input_path: str, output_path: str, workers: int = DEFAULT_WORKERS, max_candidates: int = 20):
    rows = read_input_rows(input_path)
    if not rows:
        raise ValueError("输入文件中没有可处理的域名")

    results = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(check_and_suggest, row, max_candidates): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            results[index] = future.result()
            item = results[index]
            print(
                f"[{index + 1}/{len(rows)}] {item['原域名']} -> "
                f"{item['建议域名'] or '无可用候选'} ({item['建议域名结果'] or item['原域名结果']})"
            )

    df = pd.DataFrame(results)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    if output_path.lower().endswith(".csv"):
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(output_path, index=False, engine="openpyxl")
    print(f"已导出结果到: {output_path}")


def build_default_output(input_path: str) -> str:
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}_checked.xlsx"))


def parse_args():
    parser = argparse.ArgumentParser(description="批量检测 .com 域名并自动生成可注册候选")
    parser.add_argument("--input", required=True, help="输入文件路径，支持 txt/csv/xlsx")
    parser.add_argument("--output", default="", help="输出文件路径，默认与输入同目录 *_checked.xlsx")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发数")
    parser.add_argument("--max-candidates", type=int, default=20, help="每个域名最多尝试的候选数")
    return parser.parse_args()


def main():
    args = parse_args()
    output_path = args.output or build_default_output(args.input)
    run_batch(
        input_path=args.input,
        output_path=output_path,
        workers=args.workers,
        max_candidates=args.max_candidates,
    )


if __name__ == "__main__":
    main()
