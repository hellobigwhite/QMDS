"""手动筛选精准类目 - 人工审核辅助（批量打开网站 + 逐条决策）

纯函数集中在此模块，便于单测（不依赖数据库）：
- build_review_queue: 把 filtered 文档转成前端审核队列条目（含可打开的 URL）
- resolve_move_target: 校验并归一化「移到其他类目」的目标分类
- split_target_shopify_category: 兼容勾选项携带的 target（"category__subcategory"）
"""

from qmds.config.categories import normalize_subcategory
from qmds.db.mongodb import REVIEW_STATUS_KEPT, REVIEW_STATUS_PENDING

# 单次「批量打开」的标签页上限（防止浏览器弹窗拦截与内存暴涨）
MAX_OPEN_TABS = 30

# 审核队列一次性加载上限
REVIEW_QUEUE_LIMIT = 500


def build_review_queue(docs) -> list:
    """把 filtered 文档列表转成前端审核队列

    Args:
        docs: MongoDB 文档列表（含 _id / domain / url / store_url / collection_handle 等）

    Returns:
        [{"id", "domain", "url", "fallback_url", "title", "handle", "kept"}, ...]
        无可用 URL 的文档会被跳过（无法浏览决策）
    """
    queue = []
    for doc in docs or []:
        doc_id = doc.get("_id")
        if doc_id is None:
            continue
        url = str(doc.get("url") or "").strip()
        store_url = str(doc.get("store_url") or "").strip()
        handle = str(doc.get("collection_handle") or "").strip()
        if not url and store_url:
            url = f"{store_url.rstrip('/')}/collections/{handle}" if handle else store_url
        if not url:
            continue
        queue.append({
            "id": str(doc_id),
            "domain": str(doc.get("domain") or "").strip(),
            "url": url,
            "fallback_url": store_url,
            "title": str(doc.get("collection_title") or "").strip() or handle,
            "handle": handle,
            "kept": doc.get("review_status") == REVIEW_STATUS_KEPT,
        })
    return queue


def resolve_move_target(category: str, subcategory: str,
                        target_category: str, target_subcategory: str) -> tuple:
    """校验「移到其他类目」的目标，返回 (目标一级分类, 归一化后的二级分类)

    Args:
        category: 当前一级分类
        subcategory: 当前二级分类
        target_category: 目标一级分类（空字符串表示沿用当前一级分类）
        target_subcategory: 目标二级分类（空字符串归入 "other"）

    Raises:
        ValueError: 目标一级分类为空，或目标与当前类目相同
    """
    target_cat = str(target_category or "").strip() or str(category or "").strip()
    if not target_cat:
        raise ValueError("目标一级分类不能为空")
    target_sub = normalize_subcategory(target_subcategory)
    if target_cat == str(category or "").strip() and target_sub == normalize_subcategory(subcategory):
        raise ValueError("目标类目与当前类目相同，请选择其他类目")
    return target_cat, target_sub


def split_target_shopify_category(value: str) -> tuple:
    """解析 "category__subcategory" 形式的目标类目（下拉框拼接值）

    Returns:
        (category, subcategory)；不含 "__" 时 subcategory 为空字符串
    """
    raw = str(value or "").strip()
    if "__" in raw:
        cat, sub = raw.split("__", 1)
        return cat.strip(), sub.strip()
    return raw, ""


def normalize_review_filter(value: str) -> str:
    """归一化审核状态筛选参数："" 全部 / "pending" 未审核 / "kept" 已保留"""
    raw = str(value or "").strip().lower()
    if raw in (REVIEW_STATUS_KEPT, REVIEW_STATUS_PENDING):
        return raw
    return ""
