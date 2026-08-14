"""数据二次清洗工具 - 读取文件夹中的表格数据进行清洗"""

import re
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger


IRREGULAR_NOUNS = {
    "women": "woman", "men": "man", "children": "child",
    "feet": "foot", "teeth": "tooth", "mice": "mouse",
    "geese": "goose", "oxen": "ox", "knives": "knife",
    "lives": "life", "wives": "wife", "leaves": "leaf",
    "shelves": "shelf", "halves": "half", "calves": "calf",
    "scarves": "scarf", "dwarves": "dwarf", "hooves": "hoof",
    "elves": "elf", "berries": "berry", "cherries": "cherry",
    "strawberries": "strawberry", "babies": "baby",
    "countries": "country", "cities": "city", "families": "family",
    "parties": "party", "stories": "story", "factories": "factory",
    "companies": "company", "batteries": "battery", "activities": "activity",
    "qualities": "quality", "quantities": "quantity", "utilities": "utility",
    "accessories": "accessory", "categories": "category",
    "galleries": "gallery", "libraries": "library", "machinery": "machinery",
    "jewelry": "jewelry", "footwear": "footwear", "outerwear": "outerwear",
    "underwear": "underwear", "sleepwear": "sleepwear", "activewear": "activewear",
    "swimwear": "swimwear", "sportswear": "sportswear", "workwear": "workwear",
}

CATEGORY_SEPARATOR_RE = re.compile(r"\s*->\s*|\s*>\s*|\s*,\s*|\s*/\s*|\s+-\s*|\s*-\s+|\s*[:：]\s*")


def _to_singular(word: str) -> str:
    """将单词转换为单数形式"""
    lower = word.lower()
    if lower in IRREGULAR_NOUNS:
        return IRREGULAR_NOUNS[lower]
    if len(lower) <= 2:
        return lower
    if lower.endswith("ies") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith("ves") and len(lower) > 4:
        return lower[:-3] + "f"
    if lower.endswith("ses") or lower.endswith("xes") or lower.endswith("zes") or \
       lower.endswith("ches") or lower.endswith("shes"):
        return lower[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and not lower.endswith("us") and \
       not lower.endswith("is") and len(lower) > 3:
        return lower[:-1]
    if lower.endswith("ing") and len(lower) > 5:
        base = lower[:-3]
        if len(base) >= 2 and base[-1] == base[-2]:
            return base[:-1]
        return base
    if lower.endswith("ied") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith("ed") and len(lower) > 4:
        return lower[:-2]
    if lower.endswith("es") and len(lower) > 3:
        return lower[:-1]
    return lower


def normalize_categories(categories: str) -> str:
    """标准化Categories字段：统一分隔符为|||，单词转单数，首字母大写"""
    if not categories or pd.isna(categories):
        return categories

    parts = CATEGORY_SEPARATOR_RE.split(categories)
    normalized_parts = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        words = part.split()
        singular_words = [_to_singular(w) for w in words]
        normalized_part = " ".join(singular_words).title()
        normalized_parts.append(normalized_part)

    return "|||".join(normalized_parts)


def read_table_file(file_path: Path) -> Optional[pd.DataFrame]:
    """读取CSV或Excel文件"""
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".csv":
            return pd.read_csv(file_path, encoding="utf-8")
        elif suffix in (".xlsx", ".xls"):
            return pd.read_excel(file_path)
        else:
            logger.warning(f"不支持的文件格式: {suffix}, 跳过: {file_path}")
            return None
    except UnicodeDecodeError:
        if suffix == ".csv":
            return pd.read_csv(file_path, encoding="gbk")
        return None
    except Exception as e:
        logger.error(f"读取文件失败 {file_path}: {e}")
        return None


_NUM_SEP_RE = re.compile(r"[\s\-_.,/\\|:;]+")


def _is_pure_numeric_category(val) -> bool:
    """判断分类值是否为纯数字（去除空格及常见分隔符后全为数字）"""
    if val is None:
        return False
    s = str(val).strip()
    if not s:
        return False
    cleaned = _NUM_SEP_RE.sub("", s)
    return cleaned.isdigit()


def clean_dataframe(df: pd.DataFrame, price_threshold: float = 2500.0) -> pd.DataFrame:
    """对DataFrame进行二次清洗

    1. 删除Description字段为空的数据
    2. 删除Regular price字段大于阈值的数据
    3. 把Categories字段中分隔符统一为|||
    4. 对Categories字段中单词进行单复数合并，统一改为单数，大小写统一
    5. 去掉source_category字段值中的下划线（替换为空格）
    6. 分类字段为纯数字时，用同行的source_category值覆盖（source_category为空则保留原值）
    """
    original_count = len(df)
    logger.info(f"开始清洗，原始数据量: {original_count}")

    # 查找字段名（不区分大小写）
    desc_col = None
    price_col = None
    category_col = None
    source_cat_col = None

    for col in df.columns:
        col_lower = col.strip().lower()
        if col_lower in ("description", "描述"):
            desc_col = col
        elif col_lower in ("regular price", "原价", "regular_price"):
            price_col = col
        elif col_lower in ("categories", "category", "分类"):
            category_col = col
        elif col_lower == "source_category":
            source_cat_col = col

    # 1. 删除Description字段为空的数据
    if desc_col:
        before_count = len(df)
        df = df.dropna(subset=[desc_col])
        df = df[df[desc_col].astype(str).str.strip() != ""]
        deleted = before_count - len(df)
        logger.info(f"删除Description为空的数据: {deleted} 条")
    else:
        logger.warning("未找到Description字段")

    # 2. 删除Regular price字段大于阈值的数据
    if price_col:
        before_count = len(df)
        df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
        df = df[df[price_col].isna() | (df[price_col] <= price_threshold)]
        deleted = before_count - len(df)
        logger.info(f"删除Regular price大于{price_threshold}的数据: {deleted} 条")
    else:
        logger.warning("未找到Regular price字段")

    # 3. 4. 处理Categories字段
    if category_col:
        df[category_col] = df[category_col].apply(normalize_categories)
        logger.info("已完成Categories字段标准化")
    else:
        logger.warning("未找到Categories字段")

    # 5. 去掉source_category字段值中的下划线（替换为空格）
    if source_cat_col:
        before_na = df[source_cat_col].isna().sum()
        df[source_cat_col] = df[source_cat_col].fillna("").astype(str).str.replace("_", " ", regex=False)
        df.loc[df[source_cat_col] == "", source_cat_col] = None
        logger.info(f"已完成source_category下划线去除 (空值 {int(before_na)} 条)")
    else:
        logger.warning("未找到source_category字段")

    # 6. 分类字段为纯数字时，用同行的source_category覆盖（source_category为空则保留原值）
    if category_col and source_cat_col:
        cat_series = df[category_col].fillna("").astype(str)
        numeric_mask = cat_series.apply(_is_pure_numeric_category)
        src_series = df[source_cat_col].fillna("").astype(str).str.strip()
        src_nonempty_mask = src_series != ""
        replace_mask = numeric_mask & src_nonempty_mask
        replaced = int(replace_mask.sum())
        if replaced > 0:
            df.loc[replace_mask, category_col] = src_series[replace_mask]
            logger.info(f"纯数字分类替换为source_category: {replaced} 行")
        else:
            logger.info("无纯数字分类需要替换")

    final_count = len(df)
    logger.info(f"清洗完成，最终数据量: {final_count}，共删除: {original_count - final_count} 条")

    return df


def clean_folder(input_folder: str | Path, output_folder: Optional[str | Path] = None,
                 price_threshold: float = 2500.0, suffix: str = "_cleaned") -> dict:
    """对文件夹中的所有表格文件进行二次清洗

    Args:
        input_folder: 输入文件夹路径
        output_folder: 输出文件夹路径，默认为输入文件夹下的cleaned子目录
        price_threshold: 价格阈值，删除大于此价格的数据
        suffix: 输出文件名后缀

    Returns:
        清洗结果统计
    """
    input_path = Path(input_folder)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件夹不存在: {input_path}")

    if output_folder:
        output_path = Path(output_folder)
    else:
        output_path = input_path / "cleaned"

    output_path.mkdir(parents=True, exist_ok=True)

    supported_formats = (".csv", ".xlsx", ".xls")
    files = [f for f in input_path.iterdir() if f.suffix.lower() in supported_formats and f.is_file()]

    if not files:
        logger.warning(f"未找到支持的表格文件: {input_path}")
        return {"total_files": 0, "processed": 0, "failed": 0}

    results = {"total_files": len(files), "processed": 0, "failed": 0, "details": []}

    for file_path in files:
        logger.info(f"处理文件: {file_path.name}")
        df = read_table_file(file_path)
        if df is None:
            results["failed"] += 1
            results["details"].append({"file": file_path.name, "status": "failed", "reason": "读取失败"})
            continue

        try:
            cleaned_df = clean_dataframe(df, price_threshold=price_threshold)
            output_file = output_path / f"{file_path.stem}{suffix}{file_path.suffix}"
            if file_path.suffix.lower() == ".csv":
                cleaned_df.to_csv(output_file, index=False, encoding="utf-8-sig")
            else:
                cleaned_df.to_excel(output_file, index=False)
            results["processed"] += 1
            results["details"].append({
                "file": file_path.name,
                "status": "success",
                "original_count": len(df),
                "cleaned_count": len(cleaned_df),
                "output_file": output_file.name,
            })
            logger.info(f"已保存清洗结果: {output_file}")
        except Exception as e:
            results["failed"] += 1
            results["details"].append({"file": file_path.name, "status": "failed", "reason": str(e)})
            logger.error(f"处理文件失败 {file_path.name}: {e}")

    return results
