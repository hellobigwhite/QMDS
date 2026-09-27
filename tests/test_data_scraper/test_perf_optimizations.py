"""性能优化 测试

覆盖：
- P0-1 ensure_product_indexes 进程内缓存（每集合只建一次索引）
- P0-2 汇率配置模块级缓存
- P0-3 任务级线程本地 DB 客户端池（每线程复用，任务收尾统一关闭）
- P1-1 索引精简（无查询使用的索引不再创建）
- P1-2 save_raw_products 去掉 $in 预查询后去重语义不变（真实 MongoDB）
"""

import threading

import pytest

from qmds.db import product_db as pdb
from qmds.db.product_db import (
    ProductDBClient,
    _PRODUCT_INDEX_SPECS,
    _REDUNDANT_PRODUCT_INDEXES,
)
from qmds.modules.data_scraper import product_crawler as pc


class FakeCol:
    def __init__(self):
        self.created = []
        self.indexes = {}

    def create_index(self, keys, **kwargs):
        name = kwargs.get("name")
        self.created.append(name)
        self.indexes[name] = keys
        return name

    def index_information(self):
        return dict(self.indexes)

    def drop_index(self, name):
        self.indexes.pop(name, None)


class FakePDB(ProductDBClient):
    """只替换 collection()，其余走真实逻辑"""

    def __init__(self):
        self._col = FakeCol()

    def collection(self, category, subcategory=""):
        return self._col


# ── P0-1 索引缓存 ──────────────────────────────────────────

def test_ensure_indexes_cached_same_instance():
    ProductDBClient._indexed_prefixes.clear()
    client = FakePDB()
    client.ensure_product_indexes("c1", "s1")
    assert len(client._col.created) == len(_PRODUCT_INDEX_SPECS)
    client.ensure_product_indexes("c1", "s1")
    assert len(client._col.created) == len(_PRODUCT_INDEX_SPECS)  # 无新增
    ProductDBClient._indexed_prefixes.clear()


def test_ensure_indexes_cache_shared_across_instances():
    """每站新建 ProductDBClient 也能命中缓存（类属性共享）"""
    ProductDBClient._indexed_prefixes.clear()
    c1 = FakePDB()
    c1.ensure_product_indexes("c2", "s2")
    c2 = FakePDB()
    c2.ensure_product_indexes("c2", "s2")
    assert c2._col.created == []
    ProductDBClient._indexed_prefixes.clear()


def test_ensure_indexes_different_prefix_not_cached():
    ProductDBClient._indexed_prefixes.clear()
    c1 = FakePDB()
    c1.ensure_product_indexes("c3", "s3")
    c1.ensure_product_indexes("c3", "s4")
    assert len(c1._col.created) == len(_PRODUCT_INDEX_SPECS) * 2
    ProductDBClient._indexed_prefixes.clear()


# ── P1-1 索引精简 ──────────────────────────────────────────

def test_index_specs_keep_essential_drop_redundant():
    names = {kw["name"] for _, kw in _PRODUCT_INDEX_SPECS}
    # 保留：去重唯一索引 + 主查询复合索引
    assert "idx_unique_key" in names
    assert "idx_clean_export_status" in names
    assert "idx_clean_export_category_status" in names
    assert "idx_clean_export_optimize_status" in names
    # 精简：无查询使用的单字段索引不再创建
    for name in _REDUNDANT_PRODUCT_INDEXES:
        assert name not in names
    assert len(_PRODUCT_INDEX_SPECS) == 6


def test_drop_redundant_indexes_dry_run_then_apply():
    client = FakePDB()
    client._col.indexes = {n: [] for n in _REDUNDANT_PRODUCT_INDEXES}
    client._col.indexes["idx_unique_key"] = []
    targets = client.drop_redundant_indexes("c", "s", dry_run=True)
    assert set(targets) == set(_REDUNDANT_PRODUCT_INDEXES)
    assert "idx_title" in client._col.indexes  # dry-run 不删除
    dropped = client.drop_redundant_indexes("c", "s")
    assert set(dropped) == set(_REDUNDANT_PRODUCT_INDEXES)
    assert "idx_title" not in client._col.indexes
    assert "idx_unique_key" in client._col.indexes  # 保留项不动


# ── P0-2 汇率缓存 ──────────────────────────────────────────

def test_currency_map_cached():
    pc._currency_cache["loaded"] = False
    m1 = pc._load_currency_map()
    m2 = pc._load_currency_map()
    assert m1 is m2  # 同一对象，未重复读文件/解析
    assert len(m1) > 0


def test_currency_map_has_defaults_when_file_missing(monkeypatch):
    from pathlib import Path

    # 指向不存在的目录（沙箱下 tmp_path 不可用，用工作区路径）
    monkeypatch.setattr(pc.settings, "data_dir", Path(".tmp") / "pytest_currency_missing")
    pc._currency_cache["loaded"] = False
    m = pc._load_currency_map()
    assert m["USD"] == 1.0
    assert m["AUD"] == 1.53
    pc._currency_cache["loaded"] = False


# ── P0-3 线程本地 DB 客户端池 ──────────────────────────────

def test_thread_db_pool_reuses_per_thread():
    pool = pc._ThreadDBPool()
    a = pool.product_db()
    b = pool.product_db()
    assert a is b
    other = []

    def worker():
        other.append(pool.product_db())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(other) == 1
    assert other[0] is not a  # 不同线程独立客户端
    pool.close()


def test_thread_db_pool_close_closes_all(monkeypatch):
    closed = []

    class FakeClient:
        def close(self):
            closed.append(self)

    monkeypatch.setattr(pc, "ProductDBClient", FakeClient)
    monkeypatch.setattr(pc, "MongoDBClient", FakeClient)
    pool = pc._ThreadDBPool()
    pool.product_db()
    pool.source_db()
    pool.close()
    assert len(closed) == 2
    pool.close()  # 幂等


def test_crawl_single_site_reuses_pool_client(monkeypatch):
    """同一线程连续爬两个站点，只创建 1 个 ProductDBClient（原来每站 1 个）"""
    created = []

    class FakePDBClient:
        def __init__(self):
            created.append(self)

        def save_raw_products(self, *a, **k):
            return 0

        def close(self):
            pass

    class FakeCrawler:
        def crawl_site(self, *a, **k):
            return {"success": True, "products": [], "count": 0}

        def close(self):
            pass

    class FakeMongo:
        def close(self):
            pass

    monkeypatch.setattr(pc, "ProductDBClient", FakePDBClient)
    monkeypatch.setattr(pc, "MongoDBClient", FakeMongo)
    monkeypatch.setattr(pc, "create_crawler", lambda: FakeCrawler())

    module = pc.ProductCrawler.__new__(pc.ProductCrawler)
    module._acquire_site_slot = lambda *a, **k: pc._site_concurrency.acquire(timeout=5)
    module._mark_crawled = lambda *a, **k: None

    pool = pc._ThreadDBPool()
    for i in range(3):
        module._crawl_single_site({"url": f"https://s{i}.com", "domain": f"s{i}.com"},
                                  "Home", i + 1, 3, db_pool=pool)
    assert len(created) == 1  # 三次爬取复用同一个客户端
    pool.close()


def test_crawl_single_site_temp_pool_closed_without_db_pool(monkeypatch):
    """未传 db_pool 时自建临时池并关闭（连接不泄漏）"""
    closed = []

    class FakePDBClient:
        def save_raw_products(self, *a, **k):
            return 0

        def close(self):
            closed.append(self)

    class FakeCrawler:
        def crawl_site(self, *a, **k):
            return {"success": True, "products": [], "count": 0}

        def close(self):
            pass

    monkeypatch.setattr(pc, "ProductDBClient", FakePDBClient)
    monkeypatch.setattr(pc, "MongoDBClient", lambda: type("M", (), {"close": lambda self: None})())
    monkeypatch.setattr(pc, "create_crawler", lambda: FakeCrawler())
    module = pc.ProductCrawler.__new__(pc.ProductCrawler)
    module._acquire_site_slot = lambda *a, **k: pc._site_concurrency.acquire(timeout=5)
    module._mark_crawled = lambda *a, **k: None
    module._crawl_single_site({"url": "https://z.com", "domain": "z.com"}, "Home", 1, 1)
    assert len(closed) == 1


class FakeDedupCol:
    """记录预查询键与最终插入键的假集合"""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.inserted = []
        self.queried = []

    def find(self, query, projection):
        keys = query["unique_key"]["$in"]
        self.queried.extend(keys)
        return [{"unique_key": k} for k in keys if k in self.existing]

    def insert_many(self, docs, ordered=False):
        self.inserted.extend(d["unique_key"] for d in docs)


class FakeDedupPDB(ProductDBClient):
    def __init__(self, existing=()):
        self._col = FakeDedupCol(existing)

    def collection(self, category, subcategory=""):
        return self._col

    def ensure_product_indexes(self, *a, **k):
        pass

    def _set_counter_type(self, *a, **k):
        pass

    def _inc_counters(self, *a, **k):
        pass


def test_save_only_inserts_new_keys():
    """预查询命中的键不再插入（重爬场景只走查询，不走写失败路径）"""
    db = FakeDedupPDB(existing=["k2"])
    n = db.save_raw_products("c", "s", [{"unique_key": "k1"}, {"unique_key": "k2"},
                                        {"unique_key": "k3"}])
    assert n == 2
    assert db._col.inserted == ["k1", "k3"]
    assert set(db._col.queried) == {"k1", "k2", "k3"}  # 预查询覆盖整批


def test_save_returns_zero_and_skips_insert_when_all_exist():
    db = FakeDedupPDB(existing=["k1", "k2"])
    assert db.save_raw_products("c", "s", [{"unique_key": "k1"}, {"unique_key": "k2"}]) == 0
    assert db._col.inserted == []  # 完全重复时零写入
    assert set(db._col.queried) == {"k1", "k2"}


# ── P1-2 去重（真实 MongoDB，独立临时集合） ────────────────

def _mongo_available() -> bool:
    try:
        db = ProductDBClient()
        db.client.admin.command("ping")
        db.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _mongo_available(), reason="MongoDB 不可用")
def test_save_raw_products_dedup_semantics():
    """去掉 $in 预查询后：新商品计数正确、重复商品返回 0、混合只计新增"""
    db = ProductDBClient()
    cat, sub = "__pytest_perf", "dedup"
    col = db.collection(cat, sub)
    col.drop()
    try:
        p1 = [{"unique_key": "k1", "标题": "A"}, {"unique_key": "k2", "标题": "B"}]
        assert db.save_raw_products(cat, sub, p1) == 2
        assert col.count_documents({}) == 2
        # 全部重复（模拟重爬）
        assert db.save_raw_products(cat, sub, p1) == 0
        assert col.count_documents({}) == 2
        # 部分重复
        assert db.save_raw_products(cat, sub, [{"unique_key": "k2"}, {"unique_key": "k3"}]) == 1
        assert col.count_documents({}) == 3
        # 批内重复只算一条
        assert db.save_raw_products(cat, sub, [{"unique_key": "k4"}, {"unique_key": "k4"}]) == 1
        assert col.count_documents({}) == 4
    finally:
        col.drop()
        try:
            db.db["_counters"].delete_many({"collection_key": {"$regex": "^__pytest_perf"}})
        except Exception:
            pass
        db.close()
        ProductDBClient._indexed_prefixes.clear()
