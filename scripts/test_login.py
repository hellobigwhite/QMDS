import sys, time
sys.path.insert(0, "src")
import urllib3, requests
urllib3.disable_warnings()
from qmds.db.site_db import SiteDBClient

sdb = SiteDBClient()
settings = sdb.get_all_settings()
sdb.close()
pwd = settings.get("wp_password", "")

domain = "chimneynp.com"
site_url = f"https://www.{domain}"
name = "chimneynp"
username = f"Ad{name}Min"
login_url = f"{site_url}/bbwllogin/"
data = {"log": username, "pwd": pwd, "wp-submit": "Log In",
        "redirect_to": f"{site_url}/wp-admin/", "testcookie": "1"}
headers = {"User-Agent": "Mozilla/5.0", "Referer": login_url}

s = requests.Session()
s.verify = False
print("posting login...")
r = s.post(login_url, data=data, headers=headers, verify=False, timeout=20)
print(f"  status: {r.status_code}, final url: {r.url}")
print(f"  cookies: {[c.name for c in s.cookies]}")
logged_in = any("wordpress_logged_in" in c.name for c in s.cookies)
print(f"  logged_in (cookie check): {logged_in}")
if not logged_in:
    print("  trying /wp-admin/ check...")
    check = s.get(f"{site_url}/wp-admin/", verify=False, timeout=20)
    print(f"    status: {check.status_code}, url: {check.url}")
    logged_in = check.status_code == 200 and "wp-admin" in check.url
    print(f"    logged_in (admin check): {logged_in}")

# 如果登录成功,测试 REST API
if logged_in:
    list_url = f"{site_url}/wp-admin/admin.php?page=wc-orders&paged=1"
    lr = s.get(list_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    import re
    nm = re.search(r'createNonceMiddleware\(\s*"([a-f0-9]+)"\s*\)', lr.text)
    nonce = nm.group(1) if nm else ""
    print(f"\nnonce: {nonce}")
    if nonce:
        api_url = f"{site_url}/wp-json/wc/v3/orders?per_page=3&status=any&after=2026-03-01T00:00:00&before=2026-04-01T00:00:00"
        ar = s.get(api_url, headers={"User-Agent": "Mozilla/5.0", "X-WP-Nonce": nonce}, timeout=20)
        print(f"API status: {ar.status_code}")
        if ar.status_code == 200:
            orders = ar.json()
            print(f"orders: {len(orders)}")
            if orders:
                o = orders[0]
                print(f"  first: id={o['id']} status={o['status']} date={o.get('date_created')} total={o.get('total')}")
                print(f"  line_items: {len(o.get('line_items',[]))}")
                print(f"  billing email: {o.get('billing',{}).get('email','')}")
