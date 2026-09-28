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
