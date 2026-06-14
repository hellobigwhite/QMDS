import re
import requests
import tldextract
from urllib.parse import urlparse


def _etld1(host):
    host = (host or "").split(":")[0].lower()
    ex = tldextract.extract(host)
    if not ex.suffix:
        return host
    return f"{ex.domain}.{ex.suffix}".lower()


def _same_site_family(a_host, b_host):
    return _etld1(a_host) == _etld1(b_host)


def _extract_title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:200]
    return ""


def healthcheck_domain(domain, timeout=10, check_path=""):
    check_path = check_path.strip()
    if check_path and not check_path.startswith("/"):
        check_path = "/" + check_path
    url = f"https://{domain}{check_path}" if check_path else f"https://{domain}/"

    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True, verify=True)
    except requests.exceptions.SSLError as exc:
        return False, "SSL_ERROR", {"error": str(exc)}
    except Exception as exc:
        return False, "CONNECT_ERROR", {"error": str(exc)}

    final_host = urlparse(resp.url).netloc.lower()
    final_path = urlparse(resp.url).path.rstrip("/") or "/"
    src_host = domain.lower()
    src_path = check_path.rstrip("/") or "/" if check_path else "/"

    redirect_chain = [h.url for h in resp.history]
    has_redirect = len(redirect_chain) > 0
    same_domain = _same_site_family(src_host, final_host) if final_host else True

    if final_host and not same_domain:
        return False, "FINAL_HOST_CROSS_DOMAIN", {
            "original_domain": domain,
            "final_url": resp.url,
            "final_domain": final_host,
            "redirect_chain": redirect_chain,
            "status": resp.status_code,
        }

    if check_path and src_path != final_path:
        return False, "FINAL_PATH_CHANGED", {
            "original_domain": domain,
            "original_path": check_path,
            "final_url": resp.url,
            "final_domain": final_host,
            "final_path": final_path,
            "redirect_chain": redirect_chain,
            "status": resp.status_code,
        }

    page_title = _extract_title(resp.text) if resp.status_code == 200 else ""
    has_real_content = bool(page_title)

    return True, "OK", {
        "original_domain": domain,
        "original_path": check_path or "/",
        "final_url": resp.url,
        "final_domain": final_host,
        "final_path": final_path,
        "status": resp.status_code,
        "redirect_chain": redirect_chain,
        "has_redirect": has_redirect,
        "page_title": page_title,
        "has_real_content": has_real_content,
    }
