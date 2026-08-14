"""模型优化分类结构工具

调用 LLM 优化商品分类：
1. 同义合并：语义相同的分类统一为单一标准表达
2. 单级分类补全：只有一级且非21个最大类的分类，参考 Google 标准层级思路，
   模型自主判断补充直接上一级作为父级（非查表映射）

参考 ai_menu_builder.py 的 AI 调用模式：多 key 轮换 + 同步 OpenAI + 3次重试。
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from qmds.config import settings
from qmds.config.categories import (
    CATEGORY_TO_TXT,
    CATEGORY_TXT_DIR,
    SHOPIFY_TO_GOOGLE_CATEGORY,
    _load_category_lines,
)
from qmds.utils.data_cleaner import read_table_file
from qmds.utils.logger import get_logger

log = get_logger("category_optimizer")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    log.warning("openai 未安装，模型优化分类功能不可用")

# ── LLM Configuration (复用 ai_menu_builder 配置) ──
_LLM_BASE_URL = "https://api.xiaomimimo.com/v1"
_LLM_MODEL = "mimo-v2.5-pro"
_LLM_MAX_TOKENS = 8000
_LLM_TIMEOUT = 30

# ── API Key file (复用 menu_ai_api_keys.txt) ──
_KEYS_FILE = settings.project_root / "menu_ai_api_keys.txt"

# ── Thread-safe key rotation ──
_key_lock = threading.Lock()
_key_index = 0

# ── 分批阈值 ──
BATCH_SIZE = 300

# ── 21个一级大类（Google 可读名，本地预过滤用） ──
_TOP_CATEGORIES = set(SHOPIFY_TO_GOOGLE_CATEGORY.values())


def _load_api_keys() -> list[str]:
    if not _KEYS_FILE.exists():
        return []
    lines = _KEYS_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _get_next_api_key() -> str:
    global _key_index
    with _key_lock:
        keys = _load_api_keys()
        if not keys:
            raise RuntimeError("未配置 AI API Key（menu_ai_api_keys.txt 为空或不存在）")
        key = keys[_key_index % len(keys)]
        _key_index += 1
    return key


# ── Google 参考路径构建 ──────────────────────────────

def build_reference_paths(paths_per_category: int = 3, max_total_chars: int = 6000) -> list[str]:
    """为每个一级分类选取若干条完整的深层路径作为参考

    策略：
    - 每个一级分类文件按二级分类分组，每组取 1 条最深路径
    - 路径截断到前 4 级（补全"直接上一级"只需 2-3 级关系）
    - 前缀（前3级）去重，避免相似路径
    - 选取 paths_per_category 条（覆盖不同二级分支）
    - token 熔断：总字符数超限时减为 2 条/类目
    """
    all_paths = []

    for category, txt_filename in CATEGORY_TO_TXT.items():
        lines = _load_category_lines(category)
        if not lines:
            continue

        # 按二级分类分组
        groups: dict[str, list[tuple[str, int]]] = {}
        for line in lines:
            parts = [p.strip() for p in line.split(">")]
            if len(parts) < 2:
                continue
            l2 = parts[1]
            truncated = " > ".join(parts[:4])  # 截断到前4级
            groups.setdefault(l2, []).append((truncated, len(parts)))

        # 每组取最深1条
        candidates = []
        for l2, items in groups.items():
            items.sort(key=lambda x: x[1], reverse=True)
            candidates.append(items[0][0])

        # 前缀（前3级）去重
        seen_prefixes: set[str] = set()
        selected = []
        for path in candidates:
            prefix = " > ".join(path.split(" > ")[:3])
            if prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                selected.append(path)
            if len(selected) >= paths_per_category:
                break

        all_paths.extend(selected)

    # token 熔断：超限则每个一级分类减为 2 条
    total_str = "\n".join(all_paths)
    if len(total_str) > max_total_chars and paths_per_category > 2:
        log.info(f"参考路径总字符数 {len(total_str)} 超限，减为 2 条/类目")
        return build_reference_paths(paths_per_category=2, max_total_chars=max_total_chars)

    log.info(f"构建参考路径 {len(all_paths)} 条，总字符数 {len(total_str)}")
    return all_paths


# ── Prompt 构造 ──────────────────────────────────────

def _build_prompt(
    categories: list[str],
    reference_paths: list[str],
    confirmed_expressions: Optional[dict] = None,
    attempt: int = 0,
) -> str:
    ref_text = "\n".join(reference_paths)
    cats_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(categories))
    top_cats_text = ", ".join(sorted(_TOP_CATEGORIES))

    prompt = f"""你是电商商品分类优化专家。请参考 Google Product Taxonomy 的分类标准，优化以下商品分类。

## Google Product Taxonomy 分类标准参考（完整路径示例，仅供理解层级思路）
{ref_text}

## 21个一级大类（这些分类不处理，保持原样）
{top_cats_text}

## 待优化的唯一分类列表（共 {len(categories)} 个）
{cats_text}

## 优化规则
1. 同义合并：语义相同的分类必须统一为单一标准表达，严禁出现多种写法
   - "T Shirt"/"T-Shirt"/"Tee"/"Tshirt" -> 统一 "T-Shirt"
   - "Sneaker"/"Sneakers" -> 统一 "Sneakers"

2. 单级分类补全：只有一级（不含 |||）且非21个一级大类的分类，
   参考上方Google标准的分类层级思路，自主判断其直接上一级并补充为父级：
   - 不要直接从Google树中查表映射，而是用你的语义理解判断最合适的直接父级
   - 优先选择最贴近的直接上一级，不要直接用21个最大类
   - 即使该分类不在Google树中，也请按Google的分类思路判断合适的父级
   - 补全后的分类最多两级（即最多1个|||分隔符），不要超过两级
   - 尽量避免分类层级中出现重复的词，例如 "Hardware|||Hardware" 是不允许的
   - 不要为了凑层级而添加多余的上级分类，例如 "Wraps" 应补全为 "Arrow Accessories|||Wraps"，而不是 "Archery|||Arrow Accessories|||Wraps"，省略不必要的顶层分类
   - 示例："Sneaker" -> "Shoes|||Sneakers"
   - 示例："Fidget Spinner" -> "Toys & Games|||Fidget Spinners"

3. 本身是一级分类则不处理
4. 多级分类（已有 |||）只做同义合并，不补全父级
5. 输出统一使用 ||| 作为分隔符（不要用 > 或 ->），首字母大写

## 返回格式（仅JSON，无markdown）
注意：分类层级必须使用 ||| 作为分隔符，不要使用 > 或 ->
{{"mappings": {{"原分类1": "优化后分类1", "原分类2": "优化后分类2", ...}}}}"""

    if confirmed_expressions:
        hint = "\n".join([f'  "{k}" -> "{v}"' for k, v in confirmed_expressions.items()])
        prompt += f"\n\n## 已确定的统一表达（必须遵循）\n{hint}"

    if attempt > 0:
        prompt += "\n\n⚠️ 上次返回格式有误，请务必返回纯JSON，无markdown围栏，无额外文字。"

    return prompt


# ── JSON 解析容错 ────────────────────────────────────

def _repair_json(text: str) -> str:
    """修复常见 JSON 格式问题"""
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    text = text.replace("'", '"')
    return text


def _parse_llm_response(content: str) -> dict:
    """解析 LLM 返回内容，多层容错"""
    content = content.strip()

    # 第1层：去除 markdown 围栏
    content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content).strip()

    # 第2层：json.loads
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        # 第3层：尝试修复常见 JSON 错误
        content_fixed = _repair_json(content)
        result = json.loads(content_fixed)

    # 第4层：校验结构
    mappings = result.get("mappings")
    if not isinstance(mappings, dict):
        raise ValueError("返回缺少 mappings 字段或格式错误")

    # 第5层：过滤非法映射
    cleaned = {}
    for k, v in mappings.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if not v.strip():
            continue
        cleaned[k] = v

    if not cleaned:
        raise ValueError("映射结果为空")

    return cleaned


# ── LLM 调用 ─────────────────────────────────────────

def _call_llm_optimize(
    categories: list[str],
    reference_paths: list[str],
    confirmed_expressions: Optional[dict] = None,
    log_callback=None,
) -> dict:
    """调用 LLM 优化分类，返回映射字典

    对策：
    - 多 key 轮换 + key 失效跳过
    - 429 指数退避
    - 3 次重试 + 温度递降
    - 返回解析多层容错
    """
    if not HAS_OPENAI:
        raise RuntimeError("openai 未安装")

    last_err = ""
    total_keys = len(_load_api_keys())
    attempted_keys = 0

    for attempt in range(3):
        api_key = _get_next_api_key()
        attempted_keys += 1
        try:
            prompt = _build_prompt(categories, reference_paths, confirmed_expressions, attempt)
            client = OpenAI(base_url=_LLM_BASE_URL, api_key=api_key)
            completion = client.chat.completions.create(
                model=_LLM_MODEL,
                messages=[
                    {"role": "system", "content": "You are MiMo, an AI assistant. Respond with valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3 if attempt == 0 else 0.1,
                max_completion_tokens=_LLM_MAX_TOKENS,
                top_p=0.95,
                timeout=_LLM_TIMEOUT,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = completion.choices[0].message.content.strip()
            cleaned = _parse_llm_response(content)
            return cleaned

        except Exception as e:
            last_err = str(e)
            err_str = str(e).lower()

            if "401" in err_str or "403" in err_str:
                log.warning(f"API Key 失效，跳过: {api_key[:8]}...")
                if attempted_keys >= total_keys:
                    raise RuntimeError(f"所有 API Key 均失效: {last_err}")
                continue

            if "429" in err_str:
                wait = 5 * (attempt + 1)
                log.warning(f"触发限流，等待 {wait}s 后重试")
                if log_callback:
                    log_callback(f"触发限流，等待 {wait}s 后重试", "warning")
                time.sleep(wait)
                continue

            if attempt < 2:
                wait = 2 * (attempt + 1)
                log.warning(f"AI优化分类第 {attempt+1}/3 次失败: {e}，{wait}s 后重试")
                if log_callback:
                    log_callback(f"AI优化分类第 {attempt+1}/3 次失败: {e}，{wait}s 后重试", "warning")
                time.sleep(wait)

    raise RuntimeError(f"AI优化分类失败（3次重试）: {last_err}")


# ── 映射校验 ─────────────────────────────────────────

def _validate_mappings(mappings: dict, log_callback=None) -> tuple[dict, list]:
    """校验映射结果，过滤无效映射

    返回 (valid_mappings, skipped_list)
    """
    valid_mappings = {}
    skipped = []

    for orig, optimized in mappings.items():
        if not optimized or not optimized.strip():
            skipped.append((orig, optimized, "空值"))
            continue

        # 归一化分隔符：模型可能返回 > 或 -> 而非 |||
        normalized = re.sub(r'\s*->\s*|\s*>\s*', '|||', optimized)
        if normalized != optimized:
            log.warning(f"分类分隔符已归一化为|||: {orig} -> {normalized}")
            if log_callback:
                log_callback(f"分类分隔符已归一化为|||: {orig} -> {normalized}", "warning")
            optimized = normalized

        cleaned = re.sub(r'[\s\-_.,/\\|:;]+', '', optimized)
        if cleaned.isdigit():
            skipped.append((orig, optimized, "纯数字"))
            continue

        if "undefined" in optimized.lower():
            skipped.append((orig, optimized, "含undefined"))
            continue

        # 去除层级中的重复词（同一分类内各级不能相同）
        parts = optimized.split("|||")
        seen = set()
        deduped = []
        for p in parts:
            p_lower = p.strip().lower()
            if p_lower not in seen:
                seen.add(p_lower)
                deduped.append(p)
        if len(deduped) < len(parts):
            optimized = "|||".join(deduped)
            log.warning(f"分类层级有重复词，已去重: {orig} -> {optimized}")
            if log_callback:
                log_callback(f"分类层级有重复词，已去重: {orig} -> {optimized}", "warning")

        # 去重后仍超过两级，保留最后面的两级（删除最大类）
        parts = optimized.split("|||")
        if len(parts) > 2:
            optimized = "|||".join(parts[-2:])
            log.warning(f"分类超过两级，已保留末两级: {orig} -> {optimized}")
            if log_callback:
                log_callback(f"分类超过两级，已保留末两级: {orig} -> {optimized}", "warning")

        valid_mappings[orig] = optimized

    if skipped:
        msg = f"跳过 {len(skipped)} 个无效映射"
        log.warning(msg)
        if log_callback:
            log_callback(msg, "warning")
            for orig, opt, reason in skipped[:10]:
                log_callback(f"  跳过: {orig} -> {opt} ({reason})", "warning")

    return valid_mappings, skipped


# ── 核心优化函数 ─────────────────────────────────────

def optimize_dataframe(
    df: pd.DataFrame,
    category_col: Optional[str] = None,
    log_callback=None,
) -> tuple[pd.DataFrame, dict]:
    """对 DataFrame 的分类列进行模型优化

    Args:
        df: 输入 DataFrame
        category_col: 分类列名，为 None 时自动检测
        log_callback: 日志回调函数 (message, level)

    Returns:
        (优化后的 df, 有效映射字典)
    """
    def _log(msg, level="info"):
        log.info(msg) if level == "info" else log.warning(msg)
        if log_callback:
            log_callback(msg, level)

    # 自动检测分类列
    if category_col is None:
        for col in df.columns:
            col_lower = col.strip().lower()
            if col_lower in ("categories", "category", "分类"):
                category_col = col
                break
    if category_col is None or category_col not in df.columns:
        raise ValueError(f"未找到分类列，可用列: {', '.join(df.columns.tolist())}")

    _log(f"使用分类列: {category_col}")

    # 提取唯一分类
    unique_cats = df[category_col].dropna().astype(str).unique().tolist()
    _log(f"唯一分类数: {len(unique_cats)}")

    # 本地预过滤：排除21个一级分类
    to_optimize = [c for c in unique_cats if c.strip() not in _TOP_CATEGORIES]
    skipped_top = len(unique_cats) - len(to_optimize)
    _log(f"本地预过滤: 排除 {skipped_top} 个一级大类，待优化 {len(to_optimize)} 个")

    if not to_optimize:
        _log("无需优化，所有分类均为一级大类")
        return df, {}

    # 构建参考路径
    reference_paths = build_reference_paths()
    _log(f"参考路径: {len(reference_paths)} 条")

    # 分批调用
    all_mappings = {}
    confirmed_expressions = {}

    if len(to_optimize) <= BATCH_SIZE:
        _log(f"一次性调用模型，共 {len(to_optimize)} 个分类")
        mappings = _call_llm_optimize(to_optimize, reference_paths, log_callback=log_callback)
        all_mappings.update(mappings)
        # 收集单级分类的统一表达，用于跨批一致性
        for orig, optimized in mappings.items():
            if "|||" not in orig and "|||" not in optimized:
                confirmed_expressions[orig] = optimized
    else:
        batches = [to_optimize[i:i + BATCH_SIZE] for i in range(0, len(to_optimize), BATCH_SIZE)]
        _log(f"分批调用，共 {len(batches)} 批，每批最多 {BATCH_SIZE} 个")
        for idx, batch in enumerate(batches):
            _log(f"批次 {idx+1}/{len(batches)}: {len(batch)} 个分类")
            mappings = _call_llm_optimize(
                batch, reference_paths,
                confirmed_expressions=confirmed_expressions if confirmed_expressions else None,
                log_callback=log_callback,
            )
            all_mappings.update(mappings)
            for orig, optimized in mappings.items():
                if "|||" not in orig and "|||" not in optimized:
                    confirmed_expressions[orig] = optimized
            _log(f"批次 {idx+1} 完成，累计映射 {len(all_mappings)} 个")

    # 校验映射
    valid_mappings, skipped = _validate_mappings(all_mappings, log_callback)

    # 应用映射
    def apply_mapping(x):
        if pd.isna(x):
            return x
        return valid_mappings.get(x, x)

    original_values = df[category_col].copy()
    df[category_col] = df[category_col].map(apply_mapping)

    # 统计变更
    changed_mask = df[category_col].astype(str) != original_values.astype(str)
    changed_count = int(changed_mask.sum())
    _log(f"映射应用完成: {len(valid_mappings)} 个映射，{changed_count} 行发生变更")

    # 记录变更明细（前20个）
    if changed_count > 0:
        changed_examples = []
        for orig_val in original_values[changed_mask].unique()[:20]:
            opt_val = valid_mappings.get(orig_val, orig_val)
            changed_examples.append((orig_val, opt_val))
        for orig_val, opt_val in changed_examples:
            _log(f"  变更: {orig_val} -> {opt_val}")

    return df, valid_mappings


def optimize_file(
    file_path: str | Path,
    category_col: Optional[str] = None,
    output_suffix: str = "_optimized",
    log_callback=None,
) -> dict:
    """优化单个表格文件

    Args:
        file_path: 输入文件路径
        category_col: 分类列名，为 None 时自动检测
        output_suffix: 输出文件名后缀
        log_callback: 日志回调函数 (message, level)

    Returns:
        结果统计字典
    """
    def _log(msg, level="info"):
        log.info(msg) if level == "info" else log.warning(msg)
        if log_callback:
            log_callback(msg, level)

    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    _log(f"读取文件: {file_path.name}")
    df = read_table_file(file_path)
    if df is None:
        raise ValueError(f"读取文件失败: {file_path}")

    original_count = len(df)
    _log(f"数据量: {original_count} 行")

    df, mappings = optimize_dataframe(df, category_col, log_callback)

    # 保存结果
    output_file = file_path.parent / f"{file_path.stem}{output_suffix}{file_path.suffix}"
    if file_path.suffix.lower() == ".csv":
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(output_file, index=False)

    _log(f"优化结果已保存: {output_file.name}")

    return {
        "input_file": str(file_path),
        "output_file": str(output_file),
        "original_count": original_count,
        "mappings_count": len(mappings),
    }


def optimize_folder(
    input_folder: str | Path,
    category_col: Optional[str] = None,
    output_folder: Optional[str | Path] = None,
    output_suffix: str = "_optimized",
    log_callback=None,
) -> dict:
    """批量优化文件夹中的表格文件

    Args:
        input_folder: 输入文件夹路径
        category_col: 分类列名，为 None 时自动检测
        output_folder: 输出文件夹路径，默认为输入文件夹
        output_suffix: 输出文件名后缀
        log_callback: 日志回调函数 (message, level)

    Returns:
        结果统计字典
    """
    def _log(msg, level="info"):
        log.info(msg) if level == "info" else log.warning(msg)
        if log_callback:
            log_callback(msg, level)

    input_path = Path(input_folder)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件夹不存在: {input_path}")

    output_path = Path(output_folder) if output_folder else input_path
    output_path.mkdir(parents=True, exist_ok=True)

    supported_formats = (".csv", ".xlsx", ".xls")
    files = [f for f in input_path.iterdir() if f.suffix.lower() in supported_formats and f.is_file()]

    if not files:
        _log(f"未找到支持的表格文件: {input_path}")
        return {"total_files": 0, "processed": 0, "failed": 0}

    results = {"total_files": len(files), "processed": 0, "failed": 0, "details": []}

    for file_path in files:
        _log(f"处理文件: {file_path.name}")
        try:
            result = optimize_file(file_path, category_col, output_suffix, log_callback)
            results["processed"] += 1
            results["details"].append({
                "file": file_path.name,
                "status": "success",
                **result,
            })
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "file": file_path.name,
                "status": "failed",
                "reason": str(e),
            })
            _log(f"处理文件失败 {file_path.name}: {e}", "error")

    return results
