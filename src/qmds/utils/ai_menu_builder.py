"""AI Menu Builder - LLM-driven navigation menu construction.

Builds a navigation menu centered around the site's main category theme,
using an LLM to select top-level items and sub-categories. Completely
independent from the existing wp_menu_config.py weighted-random approach.
"""

import os
import re
import json
from typing import Optional

import requests

from qmds.config import settings
from qmds.config.llm_models import (
    get_llm_model_config,
    get_llm_api_key,
    has_llm_api_key,
    get_llm_extra_body,
    get_llm_system_message,
    get_llm_default_headers,
    extract_llm_text,
    chat_completion_with_fallback,
)
from qmds.utils.logger import get_logger
from qmds.utils.wp_menu_config import (
    safe_json,
    request_with_retry,
    derive_site_url,
    wp_login,
    fetch_all_categories,
    fetch_category_tree,
    detect_nav_width,
    post_menu_list,
    norm_name,
    EXCLUDED,
)

log = get_logger("ai_menu_builder")

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    log.warning("openai 未安装，AI 构建菜单功能不可用")

# ── LLM Configuration (统一配置，从 llm_models.py 读取) ──
_LLM_MAX_TOKENS = 2000
_LLM_TIMEOUT = 30


# ── Category data preparation (reuses wp_menu_config logic) ──

def _build_id_to_info(all_cats):
    """Build term_id -> info lookup, filtering EXCLUDED categories and Home/Shop."""
    excluded_norm = {norm_name(n) for n in EXCLUDED}
    id_to_info = {}
    for cat in all_cats:
        tid = str(cat.get("term_id", ""))
        if not tid:
            continue
        term_name = cat.get("term_name", "")
        name_lower = term_name.lower().strip()
        if norm_name(term_name) in excluded_norm:
            continue
        if name_lower in EXCLUDED:
            continue
        # Never include Home or Shop as selectable categories
        if name_lower in ("home", "shop"):
            continue
        id_to_info[tid] = {
            "term_id": tid,
            "term_name": term_name,
            "parent_id": str(cat.get("parent_id", "0")),
            "product_count": int(cat.get("product_count", 0)),
            "slug": cat.get("slug", ""),
        }
    return id_to_info


def _parse_tree(tree_data):
    """Flatten tree into lookup and extract real parent-child relationships."""
    tree_id_to_node = {}

    def flatten(nodes):
        for node in nodes:
            nid = str(node.get("id", ""))
            if nid:
                tree_id_to_node[nid] = node
            if node.get("children"):
                flatten(node["children"])

    flatten(tree_data)

    def get_leaf_ids(node):
        children = node.get("children", [])
        if not children:
            return [str(node.get("id", ""))]
        leaves = []
        for child in children:
            leaves.extend(get_leaf_ids(child))
        return leaves

    real_children_of = {}
    tree_child_ids = set()
    for nid, node in tree_id_to_node.items():
        children = node.get("children", [])
        if not children:
            continue
        leaf_ids = get_leaf_ids(node)
        valid_leaves = [lid for lid in leaf_ids if lid and lid != nid]
        if valid_leaves:
            real_children_of[nid] = valid_leaves
            tree_child_ids.update(valid_leaves)
        for child in children:
            cid = str(child.get("id", ""))
            if cid:
                tree_child_ids.add(cid)

    return tree_id_to_node, real_children_of, tree_child_ids


def _build_tree_summary(tree_data, id_to_info, max_depth=2):
    """Build a compact text summary of the category tree (top 2 levels only)."""
    lines = []

    def render(nodes, depth=0):
        if depth > max_depth:
            return
        for node in nodes:
            nid = str(node.get("id", ""))
            title = node.get("title", "") or id_to_info.get(nid, {}).get("term_name", "")
            prefix = "  " * depth + ("- " if depth > 0 else "")
            lines.append(f"{prefix}{title} (id={nid})")
            if node.get("children") and depth < max_depth:
                render(node["children"], depth + 1)

    render(tree_data)
    return "\n".join(lines[:200])


def _build_category_list(id_to_info, max_items=200):
    """Build compact category list string, sorted by product_count desc."""
    items = sorted(id_to_info.items(), key=lambda x: -x[1]["product_count"])
    lines = []
    for tid, info in items[:max_items]:
        lines.append(f"{tid} | {info['term_name']} | {info['product_count']}")
    return "\n".join(lines)


# ── LLM Prompt ──

def _build_prompt(main_cat, target_top, cat_list_str, tree_summary):
    main_cat_display = main_cat if main_cat else "(not specified)"
    return f"""You are a website navigation menu architect. Build a navigation menu centered around the main category's theme, based on the main category and the website's category list.

Main category: {main_cat_display}
The navigation bar can hold {target_top} top-level menu items.

Available categories (ID | Name | Product Count):
{cat_list_str}

Existing category tree structure (for reference):
{tree_summary}

Requirements:
1. Identify the theme domain of the main category "{main_cat_display}" (e.g., "baseball bats" -> "sporting goods"). The menu should be built around this theme.
2. Appropriately highlight main-category-related content (relevant categories should be prioritized and placed near the front).
3. Select exactly {target_top} top-level menu items. They should be diverse, not all from the same sub-domain.
4. For each top-level menu, select 0-5 sub-categories from the list.
5. You may ONLY use IDs that exist in the list above.
6. Sub-categories should have a semantic relationship with their parent.

Return ONLY valid JSON (no markdown, no code fences):
{{"theme": "Sporting Goods", "items": [{{"id": "100", "children_ids": ["123", "456"]}}]}}"""


# ── LLM Call ──

def _call_llm_build_menu(main_cat, target_top, id_to_info, tree_data, site_db=None):
    """Call LLM to build menu. Returns list of {id, children_ids} or raises."""
    if not HAS_OPENAI:
        raise RuntimeError("openai 未安装")

    model_value = settings.llm_model
    if site_db is not None:
        model_value = site_db.get_setting("llm_model", "") or model_value
    config = get_llm_model_config(model_value, site_db)

    cat_list_str = _build_category_list(id_to_info)
    tree_summary = _build_tree_summary(tree_data, id_to_info)
    prompt = _build_prompt(main_cat, target_top, cat_list_str, tree_summary)

    last_err = ""
    for attempt in range(3):
        api_key = get_llm_api_key(config, site_db)
        try:
            client = OpenAI(base_url=config["base_url"], api_key=api_key,
                            default_headers=get_llm_default_headers(config))
            completion = chat_completion_with_fallback(
                client,
                config=config,
                messages=[
                    {"role": "system", "content": get_llm_system_message(config)},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_completion_tokens=_LLM_MAX_TOKENS,
                top_p=0.95,
                timeout=_LLM_TIMEOUT,
            )
            message = completion.choices[0].message
            content = extract_llm_text(message)
            if not content:
                raise ValueError("LLM 返回空内容（思考 token 耗尽或模型无输出）")
            result = json.loads(content)

            items = result.get("items", [])
            if not items:
                raise ValueError("LLM returned empty items")

            return items

        except Exception as e:
            last_err = str(e)
            log.warning(f"AI 构建菜单第 {attempt + 1}/3 次失败: {e}")
            if attempt < 2:
                import time
                time.sleep(2 if "429" not in str(e) else 5)

    raise RuntimeError(f"AI 构建菜单失败（3次重试）: {last_err}")


# ── Menu assembly ──

def _assemble_menu(llm_items, id_to_info):
    """Assemble menu_list from LLM output, validating all IDs exist."""
    menu_list = []
    used_ids = set()

    for item in llm_items:
        tid = str(item.get("id", ""))
        if not tid or tid not in id_to_info or tid in used_ids:
            continue

        info = id_to_info[tid]
        entry = {"id": tid, "title": info["term_name"], "children": []}
        used_ids.add(tid)

        for cid in item.get("children_ids", []):
            cid = str(cid)
            if cid and cid in id_to_info and cid not in used_ids:
                cinfo = id_to_info[cid]
                entry["children"].append({"id": cid, "title": cinfo["term_name"], "children": []})
                used_ids.add(cid)

        menu_list.append(entry)

    if not menu_list:
        raise RuntimeError("LLM 返回的所有 ID 无效，无法构建菜单")

    return menu_list


# ── Main category placement ──

def apply_main_cat_ai(menu_list, main_cat_name, all_cats):
    """Place main_cat (and its parent) at index 0 of menuList.
    PHP inserts Home/Shop before menuList, so main_cat ends up at position 3.
    """
    if not main_cat_name:
        return menu_list

    main_info = None
    for cat in all_cats:
        if cat.get("term_name", "").lower() == main_cat_name.lower():
            main_info = cat
            break
    if not main_info:
        for cat in all_cats:
            if norm_name(cat.get("term_name", "")) == norm_name(main_cat_name):
                main_info = cat
                break
    if not main_info:
        log.warning(f"主分类 '{main_cat_name}' 未在站点分类中找到")
        return menu_list

    main_tid = str(main_info.get("term_id", ""))
    main_name = main_info.get("term_name", main_cat_name)
    parent_id = str(main_info.get("parent_id", "0"))

    parent_info = None
    if parent_id and parent_id != "0":
        for cat in all_cats:
            if str(cat.get("term_id", "")) == parent_id:
                parent_info = cat
                break

    def remove_from_list(ml, target_id):
        for item in ml:
            if item["id"] == target_id:
                ml.remove(item)
                return item
            for child in item.get("children", []):
                if child["id"] == target_id:
                    item["children"].remove(child)
                    return child
        return None

    remove_from_list(menu_list, main_tid)
    if parent_info:
        remove_from_list(menu_list, str(parent_info.get("term_id", "")))

    insert_idx = 0

    if parent_info:
        parent_tid = str(parent_info.get("term_id", ""))
        parent_name = parent_info.get("term_name", "")
        parent_entry = {"id": parent_tid, "title": parent_name, "children": [
            {"id": main_tid, "title": main_name, "children": []}
        ]}
        menu_list.insert(insert_idx, parent_entry)
    else:
        main_entry = {"id": main_tid, "title": main_name, "children": []}
        menu_list.insert(insert_idx, main_entry)

    return menu_list


# ── Public API ──

def build_ai_menu_list(all_cats, tree_data, main_cat=None, nav_width=1140, site_db=None):
    """LLM-driven menu construction.

    Returns: list of {id, title, children: [{id, title, children: []}]}
    Raises: RuntimeError on LLM failure.
    """
    id_to_info = _build_id_to_info(all_cats)
    if not id_to_info:
        raise RuntimeError("站点无有效分类数据")

    _tree_id_to_node, _real_children_of, _tree_child_ids = _parse_tree(tree_data)

    target_top = max(5, min(10, nav_width // 130))

    llm_items = _call_llm_build_menu(main_cat, target_top, id_to_info, tree_data, site_db=site_db)
    menu_list = _assemble_menu(llm_items, id_to_info)

    return menu_list


class AiMenuConfigurator:
    """Configure WordPress navigation menu via LLM-driven construction.

    Completely independent from WpMenuConfigurator. Uses categorySearch.php
    and edit_category.php for data, LLM for menu structure, and
    updateCategory.php for submission.
    """

    def __init__(self, password=None, site_db=None):
        self._password = password
        self._session = requests.Session()
        self._session.verify = False
        self._site_db = site_db

    def configure(self, domain, main_cat=None, progress_callback=None):
        """AI 菜单配置流程

        Args:
            domain: 域名
            main_cat: 主分类名称（含|||时取最后一段），置顶到菜单 index 0
            progress_callback: 进度回调函数

        Returns:
            {"success": bool, "message": str, "menu_list": list}
        """
        site_url = derive_site_url(domain)

        if progress_callback:
            progress_callback("登录站点...")
        wp_login(self._session, site_url, self._password)

        if progress_callback:
            progress_callback("获取分类数据...")
        all_cats = fetch_all_categories(self._session, site_url)
        if not all_cats:
            return {"success": False, "message": "未获取到分类数据", "menu_list": []}

        if progress_callback:
            progress_callback("获取分类树结构...")
        tree_data = fetch_category_tree(self._session, site_url)

        if progress_callback:
            progress_callback("AI 构建菜单结构...")
        nav_width = detect_nav_width(self._session, site_url)

        cat_name = None
        if main_cat and main_cat.lower() not in ("home", "shop", "none", ""):
            cat_name = main_cat.rsplit("|||", 1)[-1].strip() if "|||" in main_cat else main_cat.strip()

        menu_list = build_ai_menu_list(all_cats, tree_data, main_cat=cat_name, nav_width=nav_width, site_db=self._site_db)

        if cat_name:
            if progress_callback:
                progress_callback(f"应用主分类置顶: {cat_name}...")
            menu_list = apply_main_cat_ai(menu_list, cat_name, all_cats)

        if progress_callback:
            progress_callback(f"提交菜单({len(menu_list)}个顶级)...")
        result = post_menu_list(self._session, site_url, menu_list)

        return {"success": result["success"], "message": result["message"], "menu_list": menu_list}
