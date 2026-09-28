import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def _stub_domain_check(monkeypatch):
    """测试默认不访问真实 whois 服务（离线可跑、不受域名真实注册状态影响）

    网站信息生成会调用西部数码 whois 检查域名是否已被注册，真实网络调用
    会让测试依赖外网、且结果随域名注册状态变化。这里统一把检查结果视为
    「未注册」，需要验证检查逻辑的测试自己 monkeypatch 覆盖本 fixture
    （见 tests/test_site_info_domain_check.py）。
    """
    try:
        from qmds.modules.web.services import site_info_generator
    except Exception:      # 依赖缺失时不影响其他测试
        return
    monkeypatch.setattr(site_info_generator, "check_domain_available",
                        lambda domain, log_fn=None: True)


@pytest.fixture(autouse=True)
def _stub_address_pool(monkeypatch):
    """测试默认不读真实地址库（data/us_addresses.json）

    真实地址库由采集脚本持续增长，直接读它会让测试输出随采集进度变化
    （城市、地址都不可控）。这里统一返回空库，生成逻辑走「代码生成街道 +
    模型补 ZIP」的回退分支，测试结果稳定。
    需要验证真实地址逻辑的测试自己 monkeypatch 覆盖本 fixture
    （见 tests/test_site_info_real_address.py 与批量城市分散用例）。
    """
    try:
        from qmds.modules.web.services import site_info_generator
    except Exception:      # 依赖缺失时不影响其他测试
        return
    monkeypatch.setattr(site_info_generator, "_load_address_pool", lambda: {})
