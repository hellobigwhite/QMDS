"""进度回调协议回归测试

背景：合并导出任务曾因 DB 层以 progress_callback(current, total) 双参数调用、
而 task_manager.make_progress_callback 的 cb 只接受单参数而直接失败：
    任务失败: make_progress_callback.<locals>.cb() takes 1 positional argument but 2 were given

本文件锁定：
1) make_progress_callback 兼容 dict / str / (current, total) / (current, total, message) 各约定；
2) merge_export_category 全流程使用与 make_progress_callback 兼容的回调协议。
"""

import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qmds.modules.web.task_manager import make_progress_callback, task_manager

# 注意：不用 pytest 的 tmp_path fixture —— 本机沙箱环境下 pytest 自建的
# basetemp 目录会带上损坏的安全描述符导致 PermissionError，故自建临时目录。
_WORKSPACE_TMP = Path(__file__).resolve().parent.parent / ".tmp"


def _new_task(prefix: str) -> str:
    task_id = f"test_{prefix}_{int(time.time() * 1000)}"
    task_manager.create(task_id, "test", "progress callback test")
    return task_id


def _make_temp_dir(prefix: str) -> Path:
    path = _WORKSPACE_TMP / f"pc_test_{prefix}_{int(time.time() * 1000)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── make_progress_callback 协议兼容性 ──────────────────────

def test_callback_accepts_dict_and_string():
    task_id = _new_task("dict_str")
    cb = make_progress_callback(task_id)

    cb({"progress": 30, "current": 3, "total": 10, "message": "步骤一"})
    task = task_manager.get(task_id)
    assert task["progress"] == 30
    assert task["current"] == 3
    assert task["total"] == 10
    assert task["message"] == "步骤一"

    cb("纯文本消息")
    task = task_manager.get(task_id)
    assert task["message"] == "纯文本消息"


def test_callback_accepts_positional_conventions():
    task_id = _new_task("positional")
    cb = make_progress_callback(task_id)

    # 双参数约定：cb(current, total) —— 修复前此调用直接 TypeError
    cb(2, 4)
    task = task_manager.get(task_id)
    assert task["current"] == 2
    assert task["total"] == 4
    assert task["progress"] == 50

    # 三参数约定：cb(current, total, message)
    cb(4, 4, "完成")
    task = task_manager.get(task_id)
    assert task["progress"] == 100
    assert task["message"] == "完成"


def test_callback_raises_interrupted_when_stopped():
    task_id = _new_task("stop")
    cb = make_progress_callback(task_id)
    task_manager.stop(task_id, join_timeout=0)
    with pytest.raises(InterruptedError):
        cb({"progress": 10})


# ── merge_export_category 回调协议回归 ─────────────────────

class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, n):
        return _FakeCursor(self._docs[:n])

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    """模拟 pymongo 集合：find 只返回"已清洗未导出"文档，update_many 记录调用"""

    def __init__(self, docs):
        self.docs = docs
        self.update_many_calls = []

    def find(self, query):
        matched = [
            d for d in self.docs
            if d.get("clean_status") == query.get("clean_status")
            and d.get("export_status") == query.get("export_status")
        ]
        return _FakeCursor(matched)

    def update_many(self, filt, update):
        ids = filt["_id"]["$in"]
        self.update_many_calls.append((ids, update))
        return SimpleNamespace(modified_count=len(ids))


def _make_stub_product_db(collections: dict):
    """构造跳过 Mongo 连接的 ProductDBClient 桩

    Args:
        collections: {prefix: [doc, ...]}，prefix 形如 "animals_pet_supplies__pet_food"

    Returns:
        (client, {prefix: _FakeCollection})
    """
    from qmds.config.categories import parse_collection_prefix
    from qmds.db.product_db import ProductDBClient

    db = ProductDBClient.__new__(ProductDBClient)
    fake_cols = {prefix: _FakeCollection(docs) for prefix, docs in collections.items()}

    def _collection(category, subcategory=""):
        for prefix, col in fake_cols.items():
            cat, sub = parse_collection_prefix(prefix)
            if cat == category and sub == subcategory:
                return col
        return _FakeCollection([])

    db.collection = _collection
    db.list_categories_with_sub = lambda: [
        {"category": parse_collection_prefix(p)[0],
         "subcategory": parse_collection_prefix(p)[1],
         "prefix": p}
        for p in fake_cols
    ]
    # 计数器维护走桩，避免依赖 _counters 集合
    db._set_counter_type = lambda *a, **k: None
    db._inc_counters = lambda *a, **k: None
    db._dec_pool_status_counters = lambda *a, **k: None
    return db, fake_cols


def test_merge_export_category_progress_callback():
    """回归：合并导出全流程不得因进度回调签名不匹配而失败

    修复前 merge_export_category 以 progress_callback(idx, total) 双参数调用回调，
    配合 web 路由传入的 make_progress_callback(task_id) 直接抛
    TypeError("...takes 1 positional argument but 2 were given") 导致任务失败。
    """
    export_dir = _make_temp_dir("merge_export")
    try:
        _run_merge_export_scenario(export_dir)
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)


def _run_merge_export_scenario(export_dir: Path):
    db, cols = _make_stub_product_db({
        "animals_pet_supplies__pet_food": [
            {"_id": "a0", "标题": "Product A0",
             "clean_status": "cleaned", "export_status": "unexported"},
            {"_id": "a1", "标题": "Product A1",
             "clean_status": "cleaned", "export_status": "unexported"},
        ],
        "animals_pet_supplies__pet_toys": [
            # 跨子分类重复 Name，用于验证去重
            {"_id": "b0", "标题": "Product A0",
             "clean_status": "cleaned", "export_status": "unexported"},
            {"_id": "b1", "标题": "Product B1",
             "clean_status": "cleaned", "export_status": "unexported"},
        ],
    })

    task_id = _new_task("merge_export")
    cb = make_progress_callback(task_id)

    result = db.merge_export_category(
        "animals_pet_supplies", str(export_dir), progress_callback=cb)

    # 修复前：上面这行抛 TypeError，任务标记为 failed
    assert result is not None
    assert result["count"] == 3              # 4 行按 Name 去重后 3 行
    assert result["dedup_count"] == 1
    assert result["sub_count"] == 2
    assert result["marked_count"] == 4       # 去重只影响 Excel 行，4 条全部标记已导出
    assert Path(result["filepath"]).exists()

    # 任务进度推进到 100，日志记录每个二级分类的汇总消息
    task = task_manager.get(task_id)
    assert task["progress"] == 100
    assert task["current"] == 2
    assert task["total"] == 2
    messages = [entry["message"] for entry in task_manager.get_logs(task_id, limit=50)]
    assert any("汇总完成（1/2）" in m for m in messages)
    assert any("汇总完成（2/2）" in m for m in messages)

    # 各集合均被标记为已导出
    for prefix, col in cols.items():
        assert col.update_many_calls, f"{prefix} 未被标记为已导出"
        for ids, update in col.update_many_calls:
            assert update["$set"]["export_status"] == "exported"
