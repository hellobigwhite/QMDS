# -*- coding: utf-8 -*-
"""真实地址库测试：地址必须对应真实存在的房屋（用户要求）

地址来源是 scripts/collect_us_addresses.py 从 OpenStreetMap 采集的住宅门牌，
生成时直接抽取并原样写进提示词，模型不得改写。
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qmds.modules.web.services import site_info_generator as sig
from qmds.modules.web.services.site_info_generator import (
    _ADDRESS_MIN_PER_CITY,
    _site_creative_direction,
    build_site_info_prompt,
)

# conftest 的 _stub_address_pool 会把 _load_address_pool 换成空库，方便其他测试
# 稳定运行；这里保存模块导入时的原始实现，供「验证真实文件读取逻辑」的用例使用。
_ORIG_LOAD_ADDRESS_POOL = sig._load_address_pool

STATS = {"categories": [{"category": "Hardware|||Plumbing", "count": 160,
                         "level1": "Hardware", "level2": "Plumbing", "level3": ""}],
         "summary": {"产品总数": 160}}

REAL_POOL = {
    "Denver|CO": [["2300 Court Pl", "Denver", "80205"]] * 12
                  + [["1904 S York St", "Denver", "80210"]] * 12,
    "Chicago|IL": [["404 S Wells St", "Chicago", "60607"]] * 12,
}


def _pool_addresses(pool):
    out = set()
    for key, rows in pool.items():
        state = key.split("|")[-1]
        for street, city, zipcode in rows:
            out.add(f"{street}, {city}, {state} {zipcode}")
    return out


@pytest.fixture
def real_pool(monkeypatch):
    """注入真实地址库（结构与 data/us_addresses.json 一致）"""
    monkeypatch.setattr(sig, "_load_address_pool", lambda: REAL_POOL)
    return REAL_POOL


@pytest.fixture
def empty_pool(monkeypatch):
    """地址库不可用（未采集 / 文件缺失）"""
    monkeypatch.setattr(sig, "_load_address_pool", lambda: {})
    return {}


def _reset_pool_cache(monkeypatch, path):
    """让 _load_address_pool 走真实文件读取（清掉缓存与 mtime 记录）"""
    monkeypatch.setattr(sig, "_US_ADDRESS_PATH", path)
    monkeypatch.setattr(sig, "_address_pool_cache", None)
    monkeypatch.setattr(sig, "_address_pool_mtime", None)


# ── 抽取 ─────────────────────────────────────

def test_real_address_picked_and_city_matches(real_pool):
    """抽出的地址必须来自库、格式完整，且 city/state 与地址自洽"""
    allowed = _pool_addresses(real_pool)
    for i in range(20):
        d = _site_creative_direction(f"addr|{i}|v0")
        addr = d["address"]
        assert addr, "地址库可用时必须给出真实地址"
        assert addr in allowed, f"地址被改写了: {addr}"

        street, city, state_zip = addr.split(", ")
        assert city == d["city"], "城市必须与地址一致（否则就是假地址）"
        state, zipcode = state_zip.split()
        assert state == d["state"]
        assert len(zipcode) == 5 and zipcode.isdigit()


def test_house_number_range_collapsed_to_single_house(monkeypatch):
    """"10811-10819 S Racine Ave" 是整排房屋号段，取起始号才是单个房屋地址"""
    monkeypatch.setattr(sig, "_load_address_pool", lambda: {
        "Chicago|IL": [["10811-10819 S Racine Ave", "Chicago", "60643"]] * 12})
    d = _site_creative_direction("range|1|v0")
    assert d["address"] == "10811 S Racine Ave, Chicago, IL 60643"


def test_fallback_when_pool_empty(empty_pool):
    """地址库不可用时回退到代码生成街道，address 留空"""
    d = _site_creative_direction("fallback|1|v0")
    assert d["address"] == ""
    assert d["street"]
    assert (d["city"], d["state"]) in sig._ADDRESS_CITIES


def test_pool_skips_thin_cities(monkeypatch):
    """数据量不足的城市不参与抽取（避免整批站点地址来回重复）"""
    thin = {"Nowhere|XX": [["1 A St", "Nowhere", "00001"]] * (_ADDRESS_MIN_PER_CITY - 1)}
    fat = {"Denver|CO": [["2300 Court Pl", "Denver", "80205"]] * _ADDRESS_MIN_PER_CITY}
    path = Path(".tmp/test_us_addresses.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({**thin, **fat}), encoding="utf-8")
    _reset_pool_cache(monkeypatch, path)
    try:
        pool = _ORIG_LOAD_ADDRESS_POOL()
        assert "Denver|CO" in pool
        assert "Nowhere|XX" not in pool
    finally:
        path.unlink(missing_ok=True)


def test_missing_pool_file_degrades_gracefully(monkeypatch):
    """地址库文件缺失时不影响生成流程（返回空库并回退）"""
    _reset_pool_cache(monkeypatch, Path(".tmp/does_not_exist.json"))
    assert _ORIG_LOAD_ADDRESS_POOL() == {}
    d = _site_creative_direction("missing|1|v0")
    assert d["address"] == ""


def test_pool_reloads_after_file_update(monkeypatch):
    """采集脚本新增城市后，服务不重启也应能用上新城市（按 mtime 自动重载）"""
    path = Path(".tmp/test_pool_reload.json")
    path.parent.mkdir(exist_ok=True)

    def _write(data, stamp):
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        os.utime(path, (stamp, stamp))            # 明确不同的 mtime

    try:
        _write({"Denver|CO": [["1 A St", "Denver", "80201"]] * 12}, 1_600_000_000)
        _reset_pool_cache(monkeypatch, path)
        assert list(_ORIG_LOAD_ADDRESS_POOL()) == ["Denver|CO"]

        _write({"Denver|CO": [["1 A St", "Denver", "80201"]] * 12,
                "Austin|TX": [["2 B St", "Austin", "78701"]] * 12}, 1_700_000_000)
        assert sorted(_ORIG_LOAD_ADDRESS_POOL()) == ["Austin|TX", "Denver|CO"]
    finally:
        path.unlink(missing_ok=True)


def test_broken_file_keeps_previous_pool(monkeypatch):
    """文件写到一半（坏 JSON）时继续用上一次的数据，而不是清空回退"""
    path = Path(".tmp/test_pool_broken.json")
    path.parent.mkdir(exist_ok=True)
    try:
        path.write_text(json.dumps({"Denver|CO": [["1 A St", "Denver", "80201"]] * 12}),
                        encoding="utf-8")
        _reset_pool_cache(monkeypatch, path)
        assert list(_ORIG_LOAD_ADDRESS_POOL()) == ["Denver|CO"]

        path.write_text('{"Denver|CO": [["1 A St"', encoding="utf-8")   # 半个 JSON
        os.utime(path, (1_700_000_000, 1_700_000_000))
        assert list(_ORIG_LOAD_ADDRESS_POOL()) == ["Denver|CO"]          # 仍用旧数据
    finally:
        path.unlink(missing_ok=True)


def test_category_key_groups_numbered_folders():
    """同一类目的多个网站文件夹（带序号）要归到同一个分组键"""
    assert sig._category_key("Faucets_1") == "faucets"
    assert sig._category_key("Faucets_2") == "faucets"
    assert sig._category_key("Faucets") == "faucets"
    assert sig._category_key("Toilet_Tank_Lids") == "toilet tank lids"
    assert sig._category_key("Faucets_1") != sig._category_key("Sinks_1")


def test_city_of_parses_address():
    assert sig._city_of("412 Oak St, Denver, CO 80205") == "Denver"
    assert sig._city_of("") == ""


def test_direction_skips_excluded_cities(real_pool):
    """排除某城市后不会再选到它（同类目网站分散到不同城市）"""
    for i in range(12):
        d = _site_creative_direction(f"excl|{i}|v0", exclude_cities={"Denver"})
        assert d["city"] == "Chicago", d["address"]
        assert "Chicago" in d["address"]


def test_direction_falls_back_when_every_city_excluded(real_pool):
    """地址库城市被该类目用完了：退回全量（宁可重复城市也要地址真实）"""
    d = _site_creative_direction("excl-all|1|v0",
                                 exclude_cities={"Denver", "Chicago"})
    assert d["city"] in ("Denver", "Chicago")
    assert d["address"]


# ── 提示词 ────────────────────────────────────

def test_prompt_uses_real_address_verbatim(real_pool):
    """地址库命中时，提示词给出真实地址并要求逐字照抄"""
    direction = _site_creative_direction("prompt|1|v0")
    prompt = build_site_info_prompt(STATS, "Faucets", direction=direction)
    assert "real, existing US residential address" in prompt
    assert direction["address"] in prompt
    assert "copy the real store address given above EXACTLY" in prompt
    assert "It is a real home address that has to stay findable on a map" in prompt
    # 回退话术不得出现
    assert "The address MUST be" not in prompt


def test_prompt_fallback_rule_without_pool(empty_pool):
    """地址库不可用时沿用旧的回退规则"""
    direction = _site_creative_direction("prompt|2|v0")
    prompt = build_site_info_prompt(STATS, "Faucets", direction=direction)
    assert "The address MUST be" in prompt
    assert "follow the store location line above" in prompt
    assert "real, existing US residential address" not in prompt
