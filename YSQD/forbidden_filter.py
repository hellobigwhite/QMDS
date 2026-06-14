"""Small test harness for the existing prohibited-word filter.

This module intentionally reuses product_processing_v2.find_prohibited_match so
manual tests match the current production filtering behavior.
"""

import argparse
import json
import sys
from typing import Any, Dict, Iterable, List, Optional

from product_processing_v2 import (
    CATEGORY_FIELD,
    DESC_FIELD,
    TITLE_FIELD,
    find_prohibited_keywords,
    find_prohibited_match,
    has_brand_keyword,
)


def build_product(
    title: str = "",
    category: str = "",
    desc: str = "",
    source_category: str = "",
) -> Dict[str, str]:
    return {
        TITLE_FIELD: title or "",
        CATEGORY_FIELD: category or "",
        DESC_FIELD: desc or "",
        "source_category": source_category or "",
    }


def check_product(doc: Dict[str, Any], include_brand: bool = False) -> Dict[str, Any]:
    """Return a structured check result without mutating or deleting anything."""
    match = find_prohibited_match(doc)
    result = {
        "blocked": bool(match),
        "match_field": match[0] if match else "",
        "match_keyword": match[1] if match else "",
    }

    if include_brand:
        result["brand_keyword_hit"] = has_brand_keyword(doc)

    return result


def check_text(
    title: str = "",
    category: str = "",
    desc: str = "",
    source_category: str = "",
    include_brand: bool = False,
) -> Dict[str, Any]:
    return check_product(
        build_product(
            title=title,
            category=category,
            desc=desc,
            source_category=source_category,
        ),
        include_brand=include_brand,
    )


def field_keyword_details(doc: Dict[str, Any]) -> Dict[str, List[str]]:
    """Show all raw keyword hits per field for debugging match behavior."""
    return {
        field: find_prohibited_keywords(str(doc.get(field) or ""))
        for field in (TITLE_FIELD, CATEGORY_FIELD, DESC_FIELD, "source_category")
    }


def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no} invalid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise SystemExit(f"{path}:{line_no} must be a JSON object")
            yield item


def normalize_input_doc(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Accept both Chinese production fields and simple English test fields."""
    doc = dict(raw)
    if TITLE_FIELD not in doc and "title" in doc:
        doc[TITLE_FIELD] = doc.get("title", "")
    if CATEGORY_FIELD not in doc and "category" in doc:
        doc[CATEGORY_FIELD] = doc.get("category", "")
    if DESC_FIELD not in doc and "desc" in doc:
        doc[DESC_FIELD] = doc.get("desc", "")
    if DESC_FIELD not in doc and "description" in doc:
        doc[DESC_FIELD] = doc.get("description", "")
    return doc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Test the existing prohibited-word filter.")
    parser.add_argument("--title", default="", help="Product title text.")
    parser.add_argument("--category", default="", help="Product category text.")
    parser.add_argument("--desc", default="", help="Product description text.")
    parser.add_argument("--source-category", default="", help="Optional source category text.")
    parser.add_argument("--jsonl", default="", help="Read one product JSON object per line.")
    parser.add_argument("--include-brand", action="store_true", help="Also report brand keyword hits.")
    parser.add_argument("--details", action="store_true", help="Show raw keyword hits for each field.")
    args = parser.parse_args(argv)

    if args.jsonl:
        rows = []
        for raw in iter_jsonl(args.jsonl):
            doc = normalize_input_doc(raw)
            result = check_product(doc, include_brand=args.include_brand)
            if args.details:
                result["field_keyword_details"] = field_keyword_details(doc)
            rows.append({"input": raw, "result": result})
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    doc = build_product(
        title=args.title,
        category=args.category,
        desc=args.desc,
        source_category=args.source_category,
    )
    result = check_product(doc, include_brand=args.include_brand)
    if args.details:
        result["field_keyword_details"] = field_keyword_details(doc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
