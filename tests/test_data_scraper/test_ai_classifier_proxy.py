"""ai_classifier 代理服务客户端 测试

_proxy_service_fetch 应复用共享 Session 且 trust_env=False（不被 Clash 劫持）。
"""

from qmds.modules.data_scraper import ai_classifier


def test_proxy_service_session_trust_env_false():
    sess = ai_classifier._get_proxy_service_session()
    assert sess.trust_env is False


def test_proxy_service_session_reused():
    a = ai_classifier._get_proxy_service_session()
    b = ai_classifier._get_proxy_service_session()
    assert a is b  # 复用同一 Session（连接复用）


def test_proxy_service_fetch_uses_session(monkeypatch):
    called = []
    fake_resp = type("R", (), {"status_code": 200, "text": "<html>" * 30})()
    sess = ai_classifier._get_proxy_service_session()
    monkeypatch.setattr(sess, "get",
                        lambda *a, **k: called.append((a, k)) or fake_resp)
    resp = ai_classifier._proxy_service_fetch("https://demo.com")
    assert resp is not None
    assert len(called) == 1
    # 请求目标是代理服务地址，且参数带 url
    args, kwargs = called[0]
    assert kwargs["params"]["url"] == "https://demo.com"
