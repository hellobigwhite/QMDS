"""从参考站点恢复通用设置（跳过 site-specific 字段）"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from qmds.utils.site_operator import get_operator, request_with_retry
from bs4 import BeautifulSoup

domains = [
    "throttline.com", "skythrustprope.com", "figuredistr.com",
    "cleaninshift.com", "edgefronti.com", "sonheaicnest.com",
    "battleheadset.com", "lumenheadroad.com", "trailisbeacon.com",
    "pinenamverse.com", "flightpagolf.com", "involtserve.com",
    "northroastmug.com", "frametripilot.com", "fairwaycrafted.com",
]
REFERENCE = "coastallooms.com"
SKIP = {"siteurl","home","admin_email","new_admin_email","blogname","blogdescription","site_icon","whl_page"}
HIDDEN = {"option_page","action","_wpnonce","_wp_http_referer"}

def read_form(site, op):
    resp = request_with_retry(op.session, "GET", f"https://www.{site}/wp-admin/options-general.php")
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"action": "options.php"})
    if not form:
        return None
    data = {}
    for inp in form.find_all("input"):
        n = inp.get("name")
        if n: data[n] = inp.get("value", "")
    for sel in form.find_all("select"):
        n = sel.get("name")
        if n:
            o = sel.find("option", selected=True)
            data[n] = o.get("value") if o else ""
    for ta in form.find_all("textarea"):
        n = ta.get("name")
        if n: data[n] = ta.text
    return data

op = get_operator()
print(f"读取 {REFERENCE}...")
op.login(REFERENCE)
ref = read_form(REFERENCE, op)
if not ref:
    print("失败"); sys.exit(1)
ref["date_format"] = "F j, Y"
ref["time_format"] = "g:i a"
print(f"参考字段({len(ref)}): {list(ref.keys())}")
print(f"日期={ref.get('date_format','?')}, 时间={ref.get('time_format','?')}")

for d in domains:
    print(f"\n=== {d} ===")
    op.login(d)
    cur = read_form(d, op)
    if not cur:
        print("  [跳过] 无法读取设置")
        continue
    fd = {}
    for k in ref:
        if k in SKIP or k in HIDDEN:
            fd[k] = cur.get(k, "")
        else:
            fd[k] = ref[k]
    r = request_with_retry(op.session, "POST", f"https://www.{d}/wp-admin/options.php", data=fd)
    print(f"  [{'OK' if r else 'FAIL'}] {len(fd)} fields pushed")
