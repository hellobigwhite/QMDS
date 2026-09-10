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
from qmds.config.llm_models import (
    get_llm_model_config,
    get_llm_api_key,
    has_llm_api_key,
    get_llm_extra_body,
    get_llm_system_message,
    get_llm_default_headers,
    extract_llm_text,
    chat_completion_with_fallback,
    count_mimo_keys,
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

# ── LLM Configuration (统一配置，从 llm_models.py 读取) ──
# 输出含 optimized + top 两个字段，每条映射约 20~25 token，
# 200 条/批约 4000~5000 token，10000 上限留足余量防截断
_LLM_MAX_TOKENS = 10000
# 批次生成实测 60~120 秒（300 个分类），推理型模型（DeepSeek 等）可能更久；
# 30s 会稳定超时，故取 300s 留足余量
_LLM_TIMEOUT = 300
# 重试时追加的格式警告（原 _build_prompt 的 attempt 逻辑移到通用调用层）
RETRY_HINT = "\n\n⚠️ 上次返回格式有误，请务必返回纯JSON，无markdown围栏，无额外文字。"

# ── 分批阈值 ──
# 300 -> 200：映射值新增 top（所属一级大类）字段后单条输出变长，
# 减小批量保证输出 token 不超上限
BATCH_SIZE = 200

# ── 模糊分类二次判定 ──
# 分类名过于宽泛（如 Accessories/Other/Parts）无法仅凭名称判断归属时，
# 模型标记 ambiguous；此时取该分类下 N 个商品样本送模型二次判定
_DISAMBIGUATE_SAMPLE_COUNT = 5   # 每个模糊分类采样的商品数
_DISAMBIGUATE_BATCH_SIZE = 20    # 二次判定每批处理的模糊分类数

# ── 21个一级大类（Google 可读名，本地预过滤用） ──
_TOP_CATEGORIES = set(SHOPIFY_TO_GOOGLE_CATEGORY.values())


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
6. top 字段：按商品语义判断每个分类真正所属的一级大类（从上方21个一级大类中
   原样选用名称，一字不差）。判断依据是商品本身属于什么，而不是它当前被放在哪里；
   即使分类当前归属于某个大类，若语义上属于其它大类，top 也要如实填写
7. ambiguous 字段：分类名过于宽泛或语义不明、仅凭名称无法可靠判断商品归属时
   设为 true（如 "Accessories" 可能是手机配件/眼镜配件/乐器配件，"Parts"、"Other"、
   "Sets"、"Misc" 等泛称，或无法从名称判断品类的品牌名/专有名词）。
   此时仍需给出暂定的 optimized 与 top；后续会提供该分类下的实际商品样本供二次判定

## 返回格式（仅JSON，无markdown）
注意：分类层级必须使用 ||| 作为分隔符，不要使用 > 或 ->
{{"mappings": {{"原分类1": {{"optimized": "优化后分类1", "top": "所属一级大类", "ambiguous": false}}, "原分类2": {{"optimized": "优化后分类2", "top": "所属一级大类", "ambiguous": true}}, ...}}}}"""

    if confirmed_expressions:
        hint = "\n".join([f'  "{k}" -> "{v}"' for k, v in confirmed_expressions.items()])
        prompt += f"\n\n## 已确定的统一表达（必须遵循）\n{hint}"

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

    # 第0层：剥离思考模型的 <think> 块
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE).strip()

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

    # 第5层：过滤非法映射，统一值结构为 {"optimized": str, "top": str|None, "ambiguous": bool}
    # 兼容两种返回：旧格式值为纯字符串（无 top）；新格式值为 dict（含 optimized/top/ambiguous）
    cleaned = {}
    for k, v in mappings.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if isinstance(v, str):
            if v.strip():
                cleaned[k] = {"optimized": v, "top": None, "ambiguous": False}
        elif isinstance(v, dict):
            opt = v.get("optimized")
            top = v.get("top")
            ambiguous = v.get("ambiguous")
            if isinstance(opt, str) and opt.strip():
                cleaned[k] = {
                    "optimized": opt,
                    "top": top.strip() if isinstance(top, str) and top.strip() else None,
                    "ambiguous": bool(ambiguous),
                }

    if not cleaned:
        raise ValueError("映射结果为空")

    return cleaned


# ── LLM 调用 ─────────────────────────────────────────

def _call_llm_core(prompt_base: str, log_callback=None, site_db=None,
                   action_label: str = "AI优化分类") -> dict:
    """通用 LLM 调用骨架：3 次重试 + 多 key 轮换 + 429 退避 + 温度递降

    prompt_base 为完整提示词；重试时追加格式警告。
    返回 _parse_llm_response 解析后的 mappings dict。
    """
    if not HAS_OPENAI:
        raise RuntimeError("openai 未安装")

    model_value = settings.llm_model
    if site_db is not None:
        model_value = site_db.get_setting("llm_model", "") or model_value
    config = get_llm_model_config(model_value, site_db)

    last_err = ""
    total_keys = 1 if config["provider"] == "ark" else count_mimo_keys()
    attempted_keys = 0

    for attempt in range(3):
        api_key = get_llm_api_key(config, site_db)
        attempted_keys += 1
        try:
            prompt = prompt_base
            if attempt > 0:
                prompt += RETRY_HINT
            # max_retries=0：禁用 SDK 内部重试。本函数外层已有 3 次重试
            # （含多 key 轮换、429 退避、温度递降），SDK 默认的 max_retries=2
            # 会对超时请求静默重试 2 次，把实际等待拉长到 3 倍超时（30s 配置
            # 实测等 95s），且绕过外层的 key 轮换与日志
            client = OpenAI(base_url=config["base_url"], api_key=api_key,
                            max_retries=0,
                            default_headers=get_llm_default_headers(config))
            completion = chat_completion_with_fallback(
                client,
                config=config,
                messages=[
                    {"role": "system", "content": get_llm_system_message(config)},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3 if attempt == 0 else 0.1,
                max_completion_tokens=_LLM_MAX_TOKENS,
                top_p=0.95,
                timeout=_LLM_TIMEOUT,
            )
            content = extract_llm_text(completion.choices[0].message)
            if not content:
                raise ValueError("LLM 返回空内容（思考 token 耗尽或模型无输出）")
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
                log.warning(f"{action_label}第 {attempt+1}/3 次失败: {e}，{wait}s 后重试")
                if log_callback:
                    log_callback(f"{action_label}第 {attempt+1}/3 次失败: {e}，{wait}s 后重试", "warning")
                time.sleep(wait)

    raise RuntimeError(f"{action_label}失败（3次重试）: {last_err}")


def _call_llm_optimize(
    categories: list[str],
    reference_paths: list[str],
    confirmed_expressions: Optional[dict] = None,
    log_callback=None,
    site_db=None,
) -> dict:
    """调用 LLM 优化分类，返回映射字典（主判定：仅凭分类名）"""
    prompt = _build_prompt(categories, reference_paths, confirmed_expressions)
    return _call_llm_core(prompt, log_callback=log_callback, site_db=site_db)


def _build_disambiguation_prompt(
    samples_by_category: dict,
    reference_paths: list,
) -> str:
    """构建模糊分类二次判定提示词

    Args:
        samples_by_category: {模糊分类名: [商品样本文本, ...]}（标题 + 描述片段）
        reference_paths: Google 参考路径（与主判定一致）
    """
    ref_text = "\n".join(reference_paths)
    top_cats_text = ", ".join(sorted(_TOP_CATEGORIES))

    blocks = []
    for i, (cat, samples) in enumerate(samples_by_category.items(), 1):
        sample_lines = "\n".join(f"   {j}. {s}" for j, s in enumerate(samples, 1))
        blocks.append(f'### 分类 {i}: "{cat}"' + "\n商品样本:\n" + sample_lines)
    cats_text = "\n\n".join(blocks)

    return (
        "你是电商商品分类专家。以下分类名称过于宽泛或语义不明，无法仅凭名称判断商品归属。\n"
        "现已提供每个分类下的实际商品样本（标题 | 描述片段），请根据商品的实际内容进行判定。\n"
        "\n"
        "## Google Product Taxonomy 分类标准参考（完整路径示例，仅供理解层级思路）\n"
        + ref_text + "\n\n"
        "## 21个一级大类（top 从中选用，一字不差）\n"
        + top_cats_text + "\n\n"
        "## 待判定分类及商品样本\n"
        + cats_text + "\n\n"
        "## 判定规则\n"
        "1. top：根据商品样本的实际内容判断该分类下商品主要属于哪个一级大类，\n"
        "   按多数样本的语义判断（从21个一级大类中选用名称，一字不差）\n"
        "2. optimized：参考样本语义给出标准表达（||| 分隔、最多两级、首字母大写），\n"
        "   与样本商品的实际品类一致\n"
        "3. 样本商品混杂多个大类时按多数判断；样本信息完全无法判断时 top 留空字符串\n"
        "\n"
        "## 返回格式（仅JSON，无markdown）\n"
        '{"mappings": {"分类名1": {"optimized": "优化后分类", "top": "所属一级大类"}, '
        '"分类名2": {"optimized": "优化后分类", "top": ""}, ...}}'
    )


# ── 映射校验 ─────────────────────────────────────────

# top 大类大小写容错查找表（模型可能返回大小写/空格略有差异的名称）
_TOP_CATEGORY_LOOKUP = {name.lower(): name for name in _TOP_CATEGORIES}


def _normalize_top_category(raw_top):
    """将模型返回的 top 大类名归一化为 21 个标准名称之一；无法识别返回 None"""
    if not raw_top or not isinstance(raw_top, str):
        return None
    return _TOP_CATEGORY_LOOKUP.get(raw_top.strip().lower())


def _validate_mappings(mappings: dict, log_callback=None) -> tuple[dict, list]:
    """校验映射结果，过滤无效映射

    值结构统一为 {"optimized": str, "top": str|None}；
    top 无法识别（不在21个一级大类内）时置 None（不影响优化本身）。

    返回 (valid_mappings, skipped_list)
    """
    valid_mappings = {}
    skipped = []

    for orig, entry in mappings.items():
        raw_optimized = entry.get("optimized") if isinstance(entry, dict) else entry
        raw_top = entry.get("top") if isinstance(entry, dict) else None
        optimized = raw_optimized
        if not optimized or not str(optimized).strip():
            skipped.append((orig, raw_optimized, "空值"))
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

        # top 校验：归一化到 21 个一级大类标准名，无法识别则置 None
        top_normalized = _normalize_top_category(raw_top)
        if raw_top and not top_normalized:
            log.warning(f"映射 top 大类无法识别，已忽略: {orig} -> {raw_top}")
            if log_callback:
                log_callback(f"映射 top 大类无法识别，已忽略: {orig} -> {raw_top}", "warning")

        ambiguous = bool(entry.get("ambiguous")) if isinstance(entry, dict) else False
        valid_mappings[orig] = {"optimized": optimized, "top": top_normalized,
                                "ambiguous": ambiguous}

    if skipped:
        msg = f"跳过 {len(skipped)} 个无效映射"
        log.warning(msg)
        if log_callback:
            log_callback(msg, "warning")
            for orig, opt, reason in skipped[:10]:
                log_callback(f"  跳过: {orig} -> {opt} ({reason})", "warning")

    return valid_mappings, skipped


# ── 核心优化函数 ─────────────────────────────────────

def build_optimize_mappings(
    unique_cats: list[str],
    log_callback=None,
    site_db=None,
    sample_fetcher=None,
) -> dict:
    """对给定唯一分类列表构建 LLM 优化映射（表格文件与数据库两种入口共用）

    流程：本地预过滤排除21个一级大类 -> 构建 Google 参考路径 -> 分批调用 LLM
    （跨批保持同义统一表达）-> 校验/归一化映射。
    模型标记为 ambiguous（分类名宽泛/语义不明）的分类，若提供了 sample_fetcher，
    取该分类下的商品样本分批送模型二次判定，用样本判定结果覆盖主判定。

    Args:
        unique_cats: 唯一分类字符串列表
        log_callback: 日志回调函数 (message, level)
        site_db: SiteDBClient，用于读取 LLM 模型配置（可选）
        sample_fetcher: 采样回调 fn(category, n) -> list[str]，返回该分类下
                        n 个商品的文本样本（标题 + 描述片段）；None 时模糊分类
                        仅优化不转移（保守处理）

    Returns:
        有效映射字典 {原分类: {"optimized", "top", "ambiguous"}}；
        无需优化时返回空字典。返回时所有 ambiguous 均已处理完毕（置 False）
    """
    def _log(msg, level="info"):
        log.info(msg) if level == "info" else log.warning(msg)
        if log_callback:
            log_callback(msg, level)

    # 本地预过滤：排除21个一级分类
    to_optimize = [c for c in unique_cats if str(c).strip() not in _TOP_CATEGORIES]
    skipped_top = len(unique_cats) - len(to_optimize)
    _log(f"本地预过滤: 排除 {skipped_top} 个一级大类，待优化 {len(to_optimize)} 个")

    if not to_optimize:
        _log("无需优化，所有分类均为一级大类")
        return {}

    # 构建参考路径
    reference_paths = build_reference_paths()
    _log(f"参考路径: {len(reference_paths)} 条")

    # 分批调用
    all_mappings = {}
    confirmed_expressions = {}

    if len(to_optimize) <= BATCH_SIZE:
        _log(f"一次性调用模型，共 {len(to_optimize)} 个分类")
        mappings = _call_llm_optimize(to_optimize, reference_paths, log_callback=log_callback, site_db=site_db)
        all_mappings.update(mappings)
        # 收集单级分类的统一表达，用于跨批一致性
        for orig, entry in mappings.items():
            optimized = entry.get("optimized") if isinstance(entry, dict) else entry
            if "|||" not in orig and "|||" not in str(optimized):
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
                site_db=site_db,
            )
            all_mappings.update(mappings)
            for orig, entry in mappings.items():
                optimized = entry.get("optimized") if isinstance(entry, dict) else entry
                if "|||" not in orig and "|||" not in str(optimized):
                    confirmed_expressions[orig] = optimized
            _log(f"批次 {idx+1} 完成，累计映射 {len(all_mappings)} 个")

    # 校验映射
    valid_mappings, skipped = _validate_mappings(all_mappings, log_callback)

    # ── 模糊分类二次判定：取商品样本送模型，用样本判定覆盖主判定 ──
    ambiguous_cats = [c for c, e in valid_mappings.items() if e.get("ambiguous")]
    if not ambiguous_cats:
        return valid_mappings

    if not sample_fetcher:
        # 无采样能力（调用方未提供）：保守处理——保留优化表达，但不转移
        _log(f"发现 {len(ambiguous_cats)} 个模糊分类（无商品采样能力，仅优化不转移）", "warning")
        for cat in ambiguous_cats:
            valid_mappings[cat]["top"] = None
            valid_mappings[cat]["ambiguous"] = False
        return valid_mappings

    _log(f"发现 {len(ambiguous_cats)} 个模糊分类，采样商品信息进行二次判定...")
    amb_batches = [ambiguous_cats[i:i + _DISAMBIGUATE_BATCH_SIZE]
                   for i in range(0, len(ambiguous_cats), _DISAMBIGUATE_BATCH_SIZE)]
    for b_idx, amb_batch in enumerate(amb_batches):
        # 采样：每个模糊分类取 N 个商品样本
        samples_by_category = {}
        for cat in amb_batch:
            samples = []
            try:
                samples = sample_fetcher(cat, _DISAMBIGUATE_SAMPLE_COUNT) or []
            except Exception as exc:
                log.warning(f"采样失败: {cat}: {exc}")
            if samples:
                samples_by_category[cat] = samples[:_DISAMBIGUATE_SAMPLE_COUNT]
            else:
                # 无样本：保持优化表达，不转移
                _log(f"  分类 {cat} 无商品样本，保持原判定且不转移", "warning")
                valid_mappings[cat]["top"] = None
                valid_mappings[cat]["ambiguous"] = False
        if not samples_by_category:
            continue

        # 分批二次判定（批内多个分类一次调用，返回格式与主判定一致）
        try:
            prompt = _build_disambiguation_prompt(samples_by_category, reference_paths)
            mappings2 = _call_llm_core(prompt, log_callback=log_callback, site_db=site_db,
                                       action_label="模糊分类二次判定")
            valid2, _ = _validate_mappings(mappings2, log_callback)
        except Exception as exc:
            # 二次判定失败：本批保守处理——保留优化表达，不转移
            _log(f"  二次判定失败（{exc}），本批 {len(samples_by_category)} 个分类保持原判定且不转移",
                 "warning")
            for cat in samples_by_category:
                valid_mappings[cat]["top"] = None
                valid_mappings[cat]["ambiguous"] = False
            continue

        resolved = 0
        for cat, entry in valid2.items():
            if cat not in valid_mappings:
                continue
            valid_mappings[cat] = {"optimized": entry["optimized"],
                                   "top": entry.get("top"),
                                   "ambiguous": False}
            resolved += 1
            _log(f"  二次判定: {cat} -> {entry['optimized']}"
                 + (f" [大类: {entry['top']}]" if entry.get("top") else " [大类: 未定，不转移]"))
        # 模型漏判/未返回的模糊分类：保守处理
        for cat in samples_by_category:
            if valid_mappings[cat].get("ambiguous"):
                _log(f"  二次判定未返回分类 {cat}，保持原判定且不转移", "warning")
                valid_mappings[cat]["top"] = None
                valid_mappings[cat]["ambiguous"] = False
        _log(f"二次判定批次 {b_idx + 1}/{len(amb_batches)} 完成，判定 {resolved} 个分类")

    return valid_mappings


def optimize_dataframe(
    df: pd.DataFrame,
    category_col: Optional[str] = None,
    log_callback=None,
    site_db=None,
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

    # 构建表格采样器：模糊分类二次判定时取同分类行的标题/描述样本
    title_col = next((c for c in ("标题", "Name", "name", "Title", "title")
                      if c in df.columns), None)
    desc_col = next((c for c in ("描述", "Description", "description")
                     if c in df.columns), None)

    def _df_sample_fetcher(cat: str, n: int) -> list:
        if not title_col and not desc_col:
            return []
        rows = df[df[category_col].astype(str) == cat].head(n)
        samples = []
        for _, row in rows.iterrows():
            title = str(row.get(title_col, "")).strip() if title_col else ""
            desc = str(row.get(desc_col, "")).strip()[:80] if desc_col else ""
            frag = (title + " | " + desc) if title and desc else (title or desc)
            if frag:
                samples.append(frag[:160])
        return samples

    # 构建优化映射（预过滤/参考路径/分批调用/校验/模糊二次判定 与数据库入口共用）
    valid_mappings = build_optimize_mappings(
        unique_cats, log_callback, site_db=site_db,
        sample_fetcher=_df_sample_fetcher if (title_col or desc_col) else None)

    if not valid_mappings:
        return df, {}

    # 应用映射（值结构 {"optimized": str, "top": str|None}，文件模式只取 optimized）
    def apply_mapping(x):
        if pd.isna(x):
            return x
        entry = valid_mappings.get(x)
        return entry["optimized"] if entry else x

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
            entry = valid_mappings.get(orig_val)
            opt_val = entry["optimized"] if entry else orig_val
            changed_examples.append((orig_val, opt_val))
        for orig_val, opt_val in changed_examples:
            _log(f"  变更: {orig_val} -> {opt_val}")

    return df, valid_mappings


def optimize_file(
    file_path: str | Path,
    category_col: Optional[str] = None,
    output_suffix: str = "_optimized",
    log_callback=None,
    site_db=None,
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

    df, mappings = optimize_dataframe(df, category_col, log_callback, site_db=site_db)

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
    site_db=None,
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
            result = optimize_file(file_path, category_col, output_suffix, log_callback, site_db=site_db)
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
