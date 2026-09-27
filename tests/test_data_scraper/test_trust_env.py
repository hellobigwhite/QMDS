"""关键 Session 不信任环境代理 测试

环境变量 HTTP_PROXY=127.0.0.1:7897（Clash）会被 requests 默认信任，
把直连/代理服务请求劫持到本地代理，连接池打满且 Clash 出口 IP 常被
Shopify 的 CF 拦。这些 Session 必须 trust_env=False 直连。
"""

from qmds.modules.data_scraper import ai_classifier
from qmds.modules.data_scraper.product_crawler import ProductCrawler, ProxyServiceClient
from qmds.utils import cloudflare_client


def test_proxy_service_client_http_trust_env_false():
    client = ProxyServiceClient(breaker_enabled=False)
    assert client._http.trust_env is False


def test_product_crawler_session_trust_env_false():
    crawler = ProductCrawler(currency_map={"USD": 1.0})
    assert crawler.session.trust_env is False


def test_ai_classifier_direct_session_trust_env_false():
    assert ai_classifier._get_direct_session().trust_env is False


def test_cloudflare_scraper_trust_env_false():
    scraper = cloudflare_client.get_scraper()
    if scraper is not None:
        assert scraper.trust_env is False
