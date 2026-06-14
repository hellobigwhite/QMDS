#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import math
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import pandas as pd
import requests


MIN_LABEL_LEN = 8
MAX_LABEL_LEN = 11
DEFAULT_WORKERS = 5
DEFAULT_MAX_CANDIDATES = 30
WHOIS_SERVER = "whois.verisign-grs.com"
RDAP_URL_TEMPLATE = "https://rdap.verisign.com/com/v1/domain/{domain}"
DNS_TYPES = ("A", "AAAA", "CNAME", "NS", "MX")
REQUEST_TIMEOUT = 8

DOMAIN_ALIASES = {
    "domain", "域名", "原域名", "原始域名", "site", "url",
}
CATEGORY_ALIASES = {
    "category", "品类", "大类", "分类", "类目",
}
MAIN_CATEGORY_ALIASES = {
    "main_category", "主分类", "主类", "目标主分类",
}

GENERIC_WORDS = {
    "shop", "store", "mall", "hub", "zone", "world", "online", "official",
    "site", "home", "group", "mart", "global", "center", "club", "base",
    "depot", "direct", "house", "plus", "best", "smart", "top", "super",
    "pro", "lab", "box", "co", "usa", "us",
}

CATEGORY_TOKEN_MAP: Dict[str, List[str]] = {
    "beauty": ["beauty", "glow", "skin", "care", "luxe"],
    "美妆": ["beauty", "glow", "skin", "care", "luxe"],
    "美容": ["beauty", "glow", "skin", "care", "luxe"],
    "sports": ["sport", "fit", "gear", "play", "rush"],
    "体育用品": ["sport", "fit", "gear", "play", "rush"],
    "electronics": ["tech", "volt", "byte", "wire", "digi"],
    "电子产品": ["tech", "volt", "byte", "wire", "digi"],
    "toy": ["toys", "play", "kids", "fun", "game"],
    "玩具": ["toys", "play", "kids", "fun", "game"],
    "bag": ["bags", "carry", "pack", "tote", "case"],
    "箱包": ["bags", "carry", "pack", "tote", "case"],
    "camera": ["cam", "lens", "optic", "shot", "photo"],
    "相机与光学器材": ["cam", "lens", "optic", "shot", "photo"],
    "office": ["desk", "paper", "work", "file", "note"],
    "办公用品": ["desk", "paper", "work", "file", "note"],
    "pet": ["pets", "paws", "tail", "fur", "woof"],
    "宠物": ["pets", "paws", "tail", "fur", "woof"],
    "furniture": ["furn", "nest", "sofa", "wood", "deco"],
    "家具": ["furn", "nest", "sofa", "wood", "deco"],
    "家居与园艺": ["home", "nest", "yard", "grow", "deco"],
    "art": ["art", "craft", "scene", "media", "show"],
    "art_entertainment": ["art", "craft", "scene", "media", "show"],
    "艺术与娱乐": ["art", "craft", "scene", "media", "show"],
    "media": ["media", "audio", "video", "press", "stream"],
    "auto": ["auto", "ride", "motor", "parts", "wheel"],
    "交通工具": ["auto", "ride", "motor", "parts", "wheel"],
    "liquor": ["drink", "brew", "bar", "wine", "sip"],
    "food": ["food", "meal", "bite", "cook", "dish"],
    "成人": ["night", "desire", "secret", "intim", "adult"],
    "fashion": ["style", "wear", "dress", "chic", "look"],
    "服饰与配饰": ["style", "wear", "dress", "chic", "look"],
    "software": ["soft", "cloud", "stack", "code", "apps"],
    "商业": ["trade", "deal", "mart", "sale", "biz"],
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


def safe_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).replace("\ufeff", "").strip()


def safe_console_text(value: object) -> str:
    text = safe_text(value)
    try:
        text.encode("gbk")
        return text
    except UnicodeEncodeError:
        return text.encode("gbk", errors="replace").decode("gbk")


def normalize_column_name(value: object) -> str:
    return safe_text(value).replace(" ", "_").lower()


def normalize_domain(text: object) -> str:
    value = safe_text(text).lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = (parsed.netloc or parsed.path).strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if "/" in host:
        host = host.split("/", 1)[0]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def extract_label(domain: str) -> str:
    host = normalize_domain(domain)
    if host.endswith(".com"):
        return host[:-4]
    return host.split(".", 1)[0]


def letters_only(value: object) -> str:
    return re.sub(r"[^a-z]", "", safe_text(value).lower())


def english_word_tokens(value: object) -> List[str]:
    words = re.findall(r"[a-z]+", safe_text(value).lower())
    result = []
    seen = set()
    for word in words:
        if len(word) < 3 or word in GENERIC_WORDS:
            continue
        clipped = word[:6]
        if clipped not in seen:
            seen.add(clipped)
            result.append(clipped)
    return result


def build_lookup_key(value: object) -> str:
    return safe_text(value).strip().lower().replace("_", " ")


def category_tokens(category: object, main_category: object = "") -> List[str]:
    lookup_values = [build_lookup_key(category), build_lookup_key(main_category)]
    tokens: List[str] = []

    for lookup in lookup_values:
        if not lookup:
            continue
        for key, values in CATEGORY_TOKEN_MAP.items():
            normalized_key = build_lookup_key(key)
            if normalized_key == lookup or normalized_key in lookup:
                tokens.extend(values)

    tokens.extend(english_word_tokens(category))
    tokens.extend(english_word_tokens(main_category))

    if not tokens:
        tokens = ["brand", "goods", "trade", "prime"]

    result = []
    seen = set()
    for token in tokens:
        clean_token = letters_only(token)
        if 3 <= len(clean_token) <= 6 and clean_token not in seen:
            seen.add(clean_token)
            result.append(clean_token)
    return result


def derive_stems(domain: str, tokens: Sequence[str]) -> List[str]:
    label = letters_only(extract_label(domain))
    if not label:
        return ["brand"]

    stems = [label]
    removable = sorted(set(tokens) | GENERIC_WORDS, key=len, reverse=True)
    for token in removable:
        if token and token in label:
            trimmed = label.replace(token, "", 1)
            if len(trimmed) >= 3:
                stems.append(trimmed)

    if len(label) >= 6:
        stems.append(label[:5])
        stems.append(label[:6])
        stems.append(label[-5:])
        stems.append(label[:3] + label[-3:])

    result = []
    seen = set()
    for stem in stems:
        clean_stem = letters_only(stem)
        if len(clean_stem) >= 3 and clean_stem not in seen:
            seen.add(clean_stem)
            result.append(clean_stem)
    return result or ["brand"]


def fit_label(left: str, right: str) -> Optional[str]:
    left = letters_only(left)
    right = letters_only(right)
    if not left or not right:
        return None
    max_left_len = MAX_LABEL_LEN - len(right)
    min_left_len = MIN_LABEL_LEN - len(right)
    if max_left_len < 3:
        return None
    left_piece = left[:max_left_len]
    if len(left_piece) < max(3, min_left_len):
        return None
    label = f"{left_piece}{right}"
    if MIN_LABEL_LEN <= len(label) <= MAX_LABEL_LEN:
        return label
    return None


def register_candidate(label: str, original_label: str, bag: List[str], seen: set):
    clean_label = letters_only(label)
    if not clean_label or clean_label == original_label:
        return
    if not (MIN_LABEL_LEN <= len(clean_label) <= MAX_LABEL_LEN):
        return
    if clean_label in seen:
        return
    seen.add(clean_label)
    bag.append(f"{clean_label}.com")


def build_candidates(domain: str, category: object, main_category: object, limit: int) -> List[str]:
    original_label = letters_only(extract_label(domain))
    tokens = category_tokens(category, main_category)
    stems = derive_stems(domain, tokens)

    candidates: List[str] = []
    seen = set()

    for stem in stems:
        for token in tokens:
            for left, right in ((stem, token), (token, stem)):
                label = fit_label(left, right)
                if label:
                    register_candidate(label, original_label, candidates, seen)
                if len(candidates) >= limit:
                    return candidates[:limit]

    for stem in stems:
        for token in tokens:
            for prefix in ("my", "go", "us", "get", "pro"):
                label = letters_only(f"{prefix}{stem}{token}")
                register_candidate(label[:MAX_LABEL_LEN], original_label, candidates, seen)
                if len(candidates) >= limit:
                    return candidates[:limit]

    for token in tokens:
        for suffix in ("co", "lab", "now", "max"):
            label = letters_only(f"{token}{suffix}")
            if len(label) < MIN_LABEL_LEN:
                label = letters_only(f"{label}{token}")[:MAX_LABEL_LEN]
            register_candidate(label, original_label, candidates, seen)
            if len(candidates) >= limit:
                return candidates[:limit]

    return candidates[:limit]


def whois_lookup(domain: str) -> Tuple[str, str]:
    try:
        with socket.create_connection((WHOIS_SERVER, 43), timeout=REQUEST_TIMEOUT) as sock:
            sock.sendall(f"{domain}\r\n".encode("utf-8"))
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
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return "available", "RDAP 404 未发现注册信息"
        if response.status_code == 200:
            return "registered", "RDAP 显示已注册"
        return "uncertain", f"RDAP 返回状态码 {response.status_code}"
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


def summarize_status(whois_status: str, rdap_status: str, dns_status: str) -> str:
    if whois_status == "available" and rdap_status == "available" and dns_status == "no_records":
        return "available"
    if "registered" in {whois_status, rdap_status} or dns_status == "has_records":
        return "registered"
    return "uncertain"


def check_domain(domain: str) -> DomainCheckResult:
    normalized = normalize_domain(domain)
    label = letters_only(extract_label(normalized))

    if not normalized or not normalized.endswith(".com"):
        return DomainCheckResult(
            domain=normalized or safe_text(domain),
            whois_status="invalid",
            rdap_status="invalid",
            dns_status="invalid",
            summary_status="invalid",
            notes="仅支持 .com 域名",
        )

    if not (MIN_LABEL_LEN <= len(label) <= MAX_LABEL_LEN):
        return DomainCheckResult(
            domain=normalized,
            whois_status="invalid",
            rdap_status="invalid",
            dns_status="invalid",
            summary_status="invalid",
            notes=f"域名长度不符合要求，去掉 .com 后需为 {MIN_LABEL_LEN}-{MAX_LABEL_LEN} 个字母",
        )

    whois_status, whois_note = whois_lookup(normalized)
    rdap_status, rdap_note = rdap_lookup(normalized)
    dns_status, dns_note = dns_lookup(normalized)
    summary_status = summarize_status(whois_status, rdap_status, dns_status)
    notes = " | ".join([whois_note, rdap_note, dns_note])

    return DomainCheckResult(
        domain=normalized,
        whois_status=whois_status,
        rdap_status=rdap_status,
        dns_status=dns_status,
        summary_status=summary_status,
        notes=notes,
    )


def split_text_line(line: str) -> List[str]:
    return [item.strip() for item in re.split(r"[\t,|]+", line) if item.strip()]


def find_matching_column(columns: Iterable[object], aliases: set) -> Optional[object]:
    normalized_map = {normalize_column_name(column): column for column in columns}
    for alias in aliases:
        if alias in normalized_map:
            return normalized_map[alias]
    return None


def read_input_rows(input_path: str) -> List[Dict[str, str]]:
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"未找到输入文件: {input_path}")

    suffix = path.suffix.lower()

    if suffix == ".txt":
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                parts = split_text_line(line)
                if not parts:
                    continue
                rows.append(
                    {
                        "domain": parts[0],
                        "category": parts[1] if len(parts) >= 2 else "",
                        "main_category": parts[2] if len(parts) >= 3 else "",
                    }
                )
        return rows

    if suffix in {".csv", ".xlsx", ".xls"}:
        dataframe = pd.read_csv(path) if suffix == ".csv" else pd.read_excel(path)
        domain_col = find_matching_column(dataframe.columns, DOMAIN_ALIASES)
        category_col = find_matching_column(dataframe.columns, CATEGORY_ALIASES)
        main_category_col = find_matching_column(dataframe.columns, MAIN_CATEGORY_ALIASES)

        if domain_col is None:
            raise ValueError("输入文件缺少域名列，支持列名如: domain / 域名 / 原始域名")

        rows = []
        for _, row in dataframe.iterrows():
            domain = safe_text(row.get(domain_col))
            if not domain:
                continue
            rows.append(
                {
                    "domain": domain,
                    "category": safe_text(row.get(category_col)) if category_col else "",
                    "main_category": safe_text(row.get(main_category_col)) if main_category_col else "",
                }
            )
        return rows

    raise ValueError("仅支持 txt / csv / xlsx / xls 输入文件")


def build_output_row(index: int, domain: str, category: str, main_category: str, original: DomainCheckResult) -> Dict[str, str]:
    return {
        "序号": index,
        "原始域名": domain,
        "品类": category,
        "主分类": main_category,
        "原始WHOIS": original.whois_status,
        "原始RDAP": original.rdap_status,
        "原始DNS": original.dns_status,
        "原始结果": original.summary_status,
        "建议域名": "",
        "建议WHOIS": "",
        "建议RDAP": "",
        "建议DNS": "",
        "建议结果": "",
        "最终域名": "",
        "最终状态": "",
        "候选尝试数": 0,
        "候选列表": "",
        "备注": original.notes,
    }


def check_and_suggest(index: int, row: Dict[str, str], max_candidates: int = DEFAULT_MAX_CANDIDATES) -> Dict[str, str]:
    original_domain = normalize_domain(row.get("domain", ""))
    category = safe_text(row.get("category"))
    main_category = safe_text(row.get("main_category"))
    original_result = check_domain(original_domain)

    output = build_output_row(index, original_domain, category, main_category, original_result)

    if original_result.summary_status == "available":
        output["建议域名"] = original_domain
        output["建议WHOIS"] = original_result.whois_status
        output["建议RDAP"] = original_result.rdap_status
        output["建议DNS"] = original_result.dns_status
        output["建议结果"] = "keep_original"
        output["最终域名"] = original_domain
        output["最终状态"] = "原域名可注册"
        return output

    candidates = build_candidates(original_domain, category, main_category, limit=max_candidates)
    output["候选尝试数"] = len(candidates)
    output["候选列表"] = ", ".join(candidates)

    for candidate in candidates:
        candidate_result = check_domain(candidate)
        if candidate_result.summary_status == "available":
            output["建议域名"] = candidate
            output["建议WHOIS"] = candidate_result.whois_status
            output["建议RDAP"] = candidate_result.rdap_status
            output["建议DNS"] = candidate_result.dns_status
            output["建议结果"] = "auto_replaced"
            output["最终域名"] = candidate
            output["最终状态"] = "已自动替换为可注册域名"
            output["备注"] = f"{original_result.notes} | 已找到可注册候选: {candidate}"
            return output

    output["最终状态"] = "需人工复核"
    output["备注"] = f"{original_result.notes} | 未找到同时满足 WHOIS、RDAP、DNS 三重条件的可注册候选"
    return output


def run_batch(input_path: str, output_path: str, workers: int, max_candidates: int):
    rows = read_input_rows(input_path)
    if not rows:
        raise ValueError("输入文件中没有可处理的数据")

    results: List[Optional[Dict[str, str]]] = [None] * len(rows)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(check_and_suggest, index + 1, row, max_candidates): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            item = future.result()
            results[index] = item
            final_domain = item["最终域名"] or item["建议域名"] or "-"
            final_status = item["最终状态"] or item["原始结果"]
            print(
                f"[{index + 1}/{len(rows)}] "
                f"{safe_console_text(item['原始域名'])} -> "
                f"{safe_console_text(final_domain)} "
                f"({safe_console_text(final_status)})"
            )

    dataframe = pd.DataFrame(results)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".csv":
        dataframe.to_csv(output, index=False, encoding="utf-8-sig")
    else:
        dataframe.to_excel(output, index=False, engine="openpyxl")
    print(f"结果已导出到: {safe_console_text(output)}")


def build_default_output(input_path: str) -> str:
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}_checked.xlsx"))


def parse_args():
    parser = argparse.ArgumentParser(
        description="批量检测 .com 域名，并自动生成满足 WHOIS + RDAP + DNS 三重条件的可注册候选域名。"
    )
    parser.add_argument("--input", required=True, help="输入文件路径，支持 txt / csv / xlsx / xls")
    parser.add_argument("--output", default="", help="输出文件路径，默认与输入同目录，文件名为 *_checked.xlsx")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发线程数，默认 5")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help="每个域名最多尝试多少个候选")
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
