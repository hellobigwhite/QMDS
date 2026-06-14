import requests
import tldextract
from urllib.parse import urlparse

def etld1(host: str) -> str:
    host = (host or "").split(":")[0].lower()
    ex = tldextract.extract(host)
    if not ex.suffix:
        return host
    return f"{ex.domain}.{ex.suffix}".lower()

def same_site_family(a_host: str, b_host: str) -> bool:
    return etld1(a_host) == etld1(b_host)

def healthcheck_domain(domain: str, timeout: int = 10):
    """
    判定目标：
    1) 证书是否能正常 verify（requests verify=True）
    2) 若发生跳转：www/非www、同根域跳转视为正常
    3) 最终落地的 host 若跨根域，才视为可疑
    """
    url = f"https://{domain}/"
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, verify=True)
    except requests.exceptions.SSLError as e:
        return False, "SSL_ERROR", {"error": str(e)}
    except Exception as e:
        return False, "CONNECT_ERROR", {"error": str(e)}

    final_host = urlparse(r.url).netloc.lower()
    src_host = domain.lower()

    # 最终落地跨根域，才可疑（否则 www/非www 都放行）
    if final_host and not same_site_family(src_host, final_host):
        return False, "FINAL_HOST_CROSS_DOMAIN", {"final_url": r.url}

    return True, "OK", {
        "final_url": r.url,
        "status": r.status_code,
        "redirects": [h.url for h in r.history]  # 跳转链
    }

print(healthcheck_domain("electricrckit.com"))
