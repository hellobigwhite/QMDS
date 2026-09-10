"""数据表上传站群系统辅助工具 — 惠升版

移植自 BB_Data_Tool 的 WebSites/system_upload_run_huisheng.py（requests 实现）。

流程：
1. 用配置中的账号密码登录站群系统（login_url）；
2. 遍历所选文件/文件夹（含子文件夹）中的 .xlsx 表格；
3. 逐个上传：multipart POST 到 upload_page_url，随后循环 GET
   {upload_page_url}do&cs=..&w=..&yz=..&cat=.. 轮询，直到服务器返回"完成"；
4. 上传获得的数据 ID（yz）按主分类保存到 txt 文件（数据ID.txt）：
   - 数据分配后的目录结构（每个主分类一个文件夹，主数据 main 前缀命名）：
     同一文件夹内主数据 ID 与补充数据 ID 写入同一个 txt，主数据 ID 在最前；
   - 其他结构：每个文件夹一个 txt，按 main 前缀识别主数据排序。

配置存放在 {data_dir}/config/site_upload_config.json，
包含 login_url / upload_page_url / username / password 四项。
"""

import json
import re
from pathlib import Path

from qmds.modules.web.services.site_review import DATA_PREFIX
from qmds.modules.web.task_manager import task_manager
from qmds.utils.logger import get_logger

log = get_logger("web.site_uploader")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

# 配置文件路径
CONFIG_FILE_NAME = "site_upload_config.json"

# 默认配置（迁移自 BB_Data_Tool 配置/站群系统配置.json 的惠升版字段，
# 可在数据导出页的站群上传卡片中修改后保存）
DEFAULT_CONFIG = {
    "login_url": "https://erp.yswl.site/index.php?main_page=login&dongzuo=denglu",
    "upload_page_url": "https://erp.yswl.site/index.php?main_page=erp_products&dongzuo=add_cp_pl",
    "username": "续豪",
    "password": "xh827",
}

REQUIRED_CONFIG_KEYS = ("login_url", "upload_page_url", "username", "password")

# 单文件上传轮询上限（防止异常响应导致死循环）
MAX_POLL_ROUNDS = 10000

# 数据 ID 保存的 txt 文件名
ID_TXT_NAME = "数据ID.txt"

# 网站数据表前缀 DATA_PREFIX（site_review.data_）：见顶部 import
# 以域名命名的文件夹（网站信息审核通过后网站文件夹改名为域名，如
# tanklidpro.com）：小写字母/数字/连字符组成的标签，至少两级，末级为字母 TLD
DOMAIN_FOLDER_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,63}$")

# 数据分配+拆表后的输出文件夹名模式
# 主数据:   {分类名}_split/            （每分类一个文件夹，整表一份）
# 补充数据: {分类名}_补充_split/       （该主分类的补充数据分卷）
SUPP_FOLDER_RE = re.compile(r"^(?P<base>.+)_补充_split$")
MAIN_FOLDER_RE = re.compile(r"^(?P<base>.+)_split$")
# 未拆表结构按文件名识别补充数据: {分类名}_补充.xlsx / {分类名}_补充_partN_*.xlsx
SUPP_FILE_RE = re.compile(r"^(?P<base>.+)_补充(?:[_.])")


def _natural_key(path) -> list:
    """自然排序键：part2 排在 part10 之前（按数字大小而非字典序）"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(path))]


def _config_path() -> Path:
    from qmds.config import settings
    return settings.data_dir / "config" / CONFIG_FILE_NAME


def load_upload_config() -> dict:
    """读取上传配置；文件不存在时以默认值创建"""
    path = _config_path()
    if not path.exists():
        save_upload_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"读取站群系统配置失败: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("站群系统配置格式不正确")
    return cfg


def save_upload_config(cfg: dict) -> Path:
    """保存上传配置（校验必填键，缺失键回退默认值）"""
    path = _config_path()
    merged = {k: str(cfg.get(k, "") or "").strip() for k in REQUIRED_CONFIG_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=4), encoding="utf-8")
    log.info(f"站群系统上传配置已保存: {path}")
    return path


def validate_config(cfg: dict):
    """校验配置完整性，缺失项抛 ValueError"""
    missing = [k for k in REQUIRED_CONFIG_KEYS if not str(cfg.get(k, "") or "").strip()]
    if missing:
        raise ValueError(f"站群系统配置不完整，缺少: {', '.join(missing)}（请先在下方保存配置）")


def _parse_fields(text: str) -> dict:
    """解析站群系统返回的类 JSON 文本（忠实移植原工具的解析逻辑）

    响应形如 {"code":"0","msg":"...","yz":"123","cat":"..."}，值中
    含冒号（如 URL）时会被截断为 "https" —— 上传流程依赖该行为判断续传。
    """
    text = (text or "").replace("\n", "").strip("{} ")
    d = {}
    for piece in text.split(","):
        if ":" not in piece:
            continue
        # 与原工具一致：split(':')[1] —— 值中含冒号（如 URL）时在第一个冒号处截断
        parts = piece.split(":")
        d[parts[0].strip('"').strip()] = parts[1].strip("'").strip('"')
    return d


def resolve_export_target(folder: str, filename: str = "") -> Path:
    """解析并校验 exports 目录下的上传目标（文件夹或单个 .xlsx，防目录穿越）"""
    from qmds.config import settings

    export_dir = (settings.data_dir / "exports").resolve()
    folder_path = (export_dir / folder).resolve()
    if not folder_path.is_relative_to(export_dir) or not folder_path.is_dir():
        raise FileNotFoundError("文件夹不存在")

    if not filename:
        return folder_path
    file_path = (folder_path / filename).resolve()
    if not file_path.is_relative_to(export_dir):
        raise FileNotFoundError("文件不存在")
    if file_path.suffix.lower() != ".xlsx" or file_path.name.startswith("~$"):
        raise FileNotFoundError("文件不存在")
    if not file_path.is_file():
        raise FileNotFoundError("文件不存在")
    return file_path


def collect_xlsx_files(target) -> list[Path]:
    """收集目标（文件或文件夹）中的所有 .xlsx（含子文件夹，跳过 ~$ 临时文件）

    按完整路径自然排序（part1, part2, ..., part10），保证上传顺序与分卷编号一致。
    """
    path = Path(target)
    if path.is_file():
        if path.suffix.lower() == ".xlsx" and not path.name.startswith("~$"):
            return [path]
        return []
    if not path.is_dir():
        raise FileNotFoundError("目标路径不存在")
    files = [p for p in path.rglob("*.xlsx") if not p.name.startswith("~$")]
    files.sort(key=_natural_key)
    return files


def collect_site_tables(folder) -> list[Path]:
    """收集网站文件夹中以 data_ 开头的数据表（自然排序: part1 -> part10）

    审核应用后网站数据表名为 data_main{分类}_part{N}_*.xlsx（主数据）与
    data_{分类}_supp_part{N}_*.xlsx（补充数据）；其他文件（分类统计.xlsx、
    网站信息.xlsx 等）不上传。
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() == ".xlsx"
             and p.name.startswith(DATA_PREFIX)
             and not p.name.startswith("~$")]
    files.sort(key=_natural_key)
    return files


def collect_domain_sites(target) -> list[dict]:
    """收集所选文件夹下所有以域名命名的网站文件夹

    网站信息审核应用后网站文件夹改名为域名（如 tanklidpro.com）。从所选
    文件夹递归查找（所选可以是任意上级目录，如日期/大类目录）；域名文件夹
    内部不再向下查找（其子文件夹属于该网站自身，避免误识别嵌套结构）。

    返回 [{"name": 域名, "path": 相对所选文件夹的路径, "tables": data_ 表数}]，
    按路径自然排序；没有 data_ 数据表的域名文件夹也列出（tables 为 0），
    由用户决定是否选择。
    """
    root = Path(target)
    if not root.is_dir():
        return []
    sites: list[dict] = []
    queue = [root]
    while queue:
        cur = queue.pop(0)
        try:
            entries = sorted(cur.iterdir(),
                             key=lambda p: _natural_key(p.name))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if DOMAIN_FOLDER_RE.match(entry.name.lower()):
                sites.append({
                    "name": entry.name,
                    "path": entry.relative_to(root).as_posix(),
                    "tables": len(collect_site_tables(entry)),
                })
            else:
                # 非域名文件夹：继续向下查找
                queue.append(entry)
    sites.sort(key=lambda s: _natural_key(s["path"]))
    return sites


def login(session, config: dict, log_fn=None):
    """登录站群系统；失败抛 RuntimeError"""
    login_url = config["login_url"]
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": USER_AGENT,
        "Referer": login_url,
    }
    response = session.post(login_url,
                            data={"username": config["username"],
                                  "password": config["password"]},
                            headers=headers, allow_redirects=True, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"登录失败（HTTP {response.status_code}）")
    if "msg" in response.text:
        # 登录接口失败时返回 {"msg": "原因"}
        try:
            failed = json.loads(response.text)["msg"]
        except Exception:
            failed = response.text[:200]
        raise RuntimeError(f"登录失败: {failed}")


def upload_one_table(session, file_path, upload_page_url: str, login_url: str,
                     log_fn=None, stop_check=None,
                     max_polls: int = MAX_POLL_ROUNDS) -> str:
    """上传一个 .xlsx 表格并轮询至完成，返回任务 id（yz）

    移植原工具 uploadOneTable：POST 上传文件后循环 GET 轮询处理进度，
    服务器返回 msg == "完成" 视为上传完毕。
    """
    headers = {"User-Agent": USER_AGENT, "Referer": login_url}
    headers2 = {"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"}

    file_path = Path(file_path)
    with open(file_path, "rb") as f:
        file_data = {
            "main_page": "erp_products",
            "dongzuo": "add_cp_pl",
            "file": (file_path.name, f),
        }
        response = session.post(upload_page_url, files=file_data,
                                headers=headers, timeout=600)
    if response.status_code != 200:
        raise RuntimeError(
            f"服务器无法处理（HTTP {response.status_code}），可能是数据过大或格式不对")

    cs = 2
    yz_id = "0"
    msg = ""
    for _ in range(max_polls):
        if stop_check and stop_check():
            raise InterruptedError("任务被用户停止")
        d = _parse_fields(response.text)
        if d.get("msg") == "完成":
            return str(yz_id)
        if d.get("code", "0") != "0":
            # msg == "https"（URL 被解析截断）且 code != "3"：按新 cs 续传
            if d.get("msg") != "https" or d.get("code") == "3":
                raise RuntimeError(f"上传失败: {str(d.get('msg', '')).strip()}")
            cs = d["code"]
        else:
            msg = d.get("msg", "")
            yz_id = d.get("yz", yz_id)
        upload_url = (f"{upload_page_url}do&cs={cs}&w={msg}"
                      f"&yz={d.get('yz', '')}&cat={d.get('cat', '')}")
        response = session.get(upload_url, headers=headers2, timeout=600)
        if response.status_code != 200:
            raise RuntimeError(f"轮询上传状态失败（HTTP {response.status_code}）")
    raise RuntimeError(f"上传轮询超过 {max_polls} 次仍未完成")


def save_upload_ids(target, uploaded: list) -> list[Path]:
    """把上传获得的数据 ID 按主分类写入 txt 文件

    参数:
        target: 上传目标（文件夹或文件路径）
        uploaded: [(文件路径, 数据ID), ...] 按上传顺序

    数据分配后的目录结构（每个主分类一个文件夹）:
      {X}/main{X}*.xlsx      主数据（main 前缀）
      {X}/{X}_supp*.xlsx     该主分类的补充数据
      extra{N}/extra{N}*.xlsx 额外补充
    -> 每个文件夹一个 数据ID.txt：主数据 ID 在最前（按 main 前缀识别），
       其后为补充数据的 ID（按上传顺序，即分卷编号顺序）。

    旧版结构兼容（*_split 文件夹）: {X}_补充_split/ 的 ID 归入 {X}_split/。

    返回写入的 txt 文件路径列表。
    """
    target = Path(target)
    # txt 所在文件夹 -> [(rank, 上传顺序, ID)]；rank: 0=主数据, 1=补充数据
    entries: dict[Path, list[tuple[int, int, str]]] = {}

    for order, (fp, pid) in enumerate(uploaded):
        fp = Path(fp)
        # 网站数据表带 data_ 前缀（data_mainCat...）: 剥离后再判断主/补充
        fname = fp.name
        if fname.startswith(DATA_PREFIX):
            fname = fname[len(DATA_PREFIX):]
        # 分组文件夹：文件所在目录；文件直接位于目标下时即目标本身
        if fp.parent == target:
            group_folder, group_name = target, target.name
        else:
            group_folder, group_name = fp.parent, fp.parent.name

        m = SUPP_FOLDER_RE.match(group_name)
        if m:
            # 旧版结构: 补充数据文件夹 -> 归入对应主数据文件夹
            main_folder = group_folder.parent / f"{m.group('base')}_split"
            out_folder = main_folder if main_folder.is_dir() else group_folder
            rank = 1
        elif fname.startswith("main"):
            # 主数据表以 main 开头命名（与补充数据同文件夹）
            out_folder = group_folder
            rank = 0
        elif MAIN_FOLDER_RE.match(group_name) and not SUPP_FILE_RE.match(fname):
            # 旧版结构: 主数据文件夹中的非补充文件
            out_folder = group_folder
            rank = 0
        else:
            # 补充数据 / 额外补充
            out_folder = group_folder
            rank = 1
        entries.setdefault(out_folder, []).append((rank, order, str(pid)))

    written = []
    for folder, items in entries.items():
        items.sort(key=lambda t: (t[0], t[1]))  # 主数据在前，同组按上传顺序
        txt = folder / ID_TXT_NAME
        txt.write_text("\n".join(pid for _, _, pid in items) + "\n",
                       encoding="utf-8")
        written.append(txt)
    return written


def run_upload_task(task_id: str, target_path, config: dict | None = None):
    """站群系统上传后台任务体：登录 -> 逐个上传 -> 数据ID保存到txt"""
    import requests

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        cfg = config or load_upload_config()
        validate_config(cfg)

        task_manager.update(task_id, status="running", message="正在登录站群系统...")
        _log(f"任务启动: 数据表上传站群系统（惠升版） -> {cfg['upload_page_url']}")

        files = collect_xlsx_files(target_path)
        if not files:
            raise ValueError("目标路径中没有可上传的 .xlsx 表格")
        _log(f"待上传表格: {len(files)} 个")

        session = requests.Session()
        login(session, cfg, _log)
        _log("登录成功")
        if task_manager.is_stopped(task_id):
            task_manager.update(task_id, status="stopped", message="任务已停止")
            return

        uploaded: list[tuple[Path, str]] = []  # (文件, 数据ID) 按上传顺序
        failed = 0
        for i, fp in enumerate(files):
            if task_manager.is_stopped(task_id):
                task_manager.update(task_id, status="stopped",
                                    message=f"已停止: 已上传 {len(uploaded)} 个表格")
                return
            task_manager.update(task_id, current=i + 1, total=len(files),
                                message=f"上传中 {i + 1}/{len(files)}: {fp.name}")
            _log(f"开始上传 ({i + 1}/{len(files)}): {fp.name}")
            try:
                pid = upload_one_table(session, fp, cfg["upload_page_url"],
                                       cfg["login_url"], log_fn=_log,
                                       stop_check=lambda: task_manager.is_stopped(task_id))
                uploaded.append((fp, pid))
                _log(f"上传完毕: {fp.name}（数据ID {pid}）")
            except InterruptedError:
                task_manager.update(task_id, status="stopped",
                                    message=f"已停止: 已上传 {len(uploaded)} 个表格")
                return
            except Exception as e:
                failed += 1
                _log(f"上传失败 {fp.name}: {e}", "error")

        # 数据 ID 按主分类保存到 txt（主数据与对应补充数据放在一起，主数据在前）
        written_txts: list[Path] = []
        if uploaded:
            task_manager.update(task_id, progress=95,
                                message=f"保存 {len(uploaded)} 个数据ID到 txt 文件...")
            written_txts = save_upload_ids(target_path, uploaded)
            for txt in written_txts:
                _log(f"数据ID已保存: {txt.parent.name}/{txt.name}")

        summary = (f"完成: 上传 {len(uploaded)}/{len(files)} 个表格"
                   f"（失败 {failed}），数据ID已保存到 {len(written_txts)} 个 txt 文件")
        task_manager.update(task_id, status="completed", message=summary, progress=100)
        _log(summary)

    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="任务已停止")
    except Exception as e:
        log.error(f"站群系统上传任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")


def run_domain_upload_task(task_id: str, target_path, site_names: list,
                           config: dict | None = None):
    """按网站（域名文件夹）逐站上传任务体

    登录一次站群系统，按所选顺序一个网站一个网站地顺序上传：每个网站只
    上传文件夹内以 data_ 开头的数据表（自然排序，part1 -> part10）。
    全部完成后在任务日志中按网站分组输出上传返回的数据 ID，并把各网站的
    数据 ID 写入其文件夹内的 数据ID.txt（主数据 ID 在最前）。
    """
    import requests

    def _log(msg, level="info"):
        task_manager.add_log(task_id, msg, level)

    try:
        cfg = config or load_upload_config()
        validate_config(cfg)

        root = Path(target_path)
        all_sites = {s["name"]: s for s in collect_domain_sites(root)}
        chosen = []
        for name in site_names:
            site = all_sites.get(str(name or "").strip())
            if site is None:
                raise ValueError(f"未找到域名文件夹: {name}")
            chosen.append(site)
        if not chosen:
            raise ValueError("未选择任何网站")

        total_tables = sum(s["tables"] for s in chosen)
        task_manager.update(task_id, status="running", progress=0,
                            message="正在登录站群系统...")
        _log(f"任务启动: 按网站上传数据表 -> {cfg['upload_page_url']}")
        _log(f"选中 {len(chosen)} 个网站，共 {total_tables} 个 data_ 数据表")

        session = requests.Session()
        login(session, cfg, _log)
        _log("登录成功")

        done = 0           # 已处理表格数（含失败）
        uploaded_total = 0  # 上传成功表格数
        ok_sites = 0        # 至少上传成功一个表的网站数
        site_ids: list[tuple[str, list[str]]] = []  # (域名, [数据ID...])

        for si, site in enumerate(chosen, start=1):
            if task_manager.is_stopped(task_id):
                task_manager.update(
                    task_id, status="stopped",
                    message=f"已停止: 已上传 {uploaded_total} 个表格")
                return
            name = site["name"]
            folder = root / site["path"]
            tables = collect_site_tables(folder)
            if not tables:
                _log(f"[{si}/{len(chosen)}] ⚠ 网站 {name}: 没有 data_ 数据表，跳过")
                site_ids.append((name, []))
                continue
            _log(f"[{si}/{len(chosen)}] ▶ 网站 {name}: "
                 f"开始上传 {len(tables)} 个数据表")

            site_uploaded: list[tuple[Path, str]] = []
            site_failed = 0
            for fp in tables:
                if task_manager.is_stopped(task_id):
                    task_manager.update(
                        task_id, status="stopped",
                        message=f"已停止: 已上传 {uploaded_total} 个表格")
                    return
                done += 1
                task_manager.update(
                    task_id, current=done, total=total_tables,
                    message=f"网站 {name}: 上传 {done}/{total_tables}（{fp.name}）")
                try:
                    pid = upload_one_table(
                        session, fp, cfg["upload_page_url"], cfg["login_url"],
                        log_fn=_log,
                        stop_check=lambda: task_manager.is_stopped(task_id))
                    site_uploaded.append((fp, pid))
                    _log(f"  ✓ {fp.name}（数据ID {pid}）")
                except InterruptedError:
                    task_manager.update(
                        task_id, status="stopped",
                        message=f"已停止: 已上传 {uploaded_total} 个表格")
                    return
                except Exception as e:
                    site_failed += 1
                    _log(f"  ✗ {fp.name}: {e}", "error")

            uploaded_total += len(site_uploaded)
            ids = [pid for _, pid in site_uploaded]
            if site_uploaded:
                ok_sites += 1
                try:
                    save_upload_ids(folder, site_uploaded)
                except Exception as e:
                    _log(f"网站 {name}: 数据ID保存到 txt 失败: {e}", "warning")
            site_ids.append((name, ids))
            _log(f"[{si}/{len(chosen)}] ✔ 网站 {name} 完成: "
                 f"上传 {len(site_uploaded)}/{len(tables)} 个表格"
                 + (f"（失败 {site_failed}）" if site_failed else "")
                 + (f"，数据ID: {', '.join(ids)}" if ids else ""))

        # ── 汇总: 按网站分组输出全部数据 ID ──
        _log("── 全部网站数据ID汇总（按网站分组）──")
        for name, ids in site_ids:
            if ids:
                _log(f"{name}（{len(ids)} 个）: {', '.join(ids)}")
            else:
                _log(f"{name}: 无上传成功的数据表")

        summary = (f"完成: 上传 {ok_sites}/{len(chosen)} 个网站"
                   f"（{uploaded_total}/{total_tables} 个表格）")
        task_manager.update(task_id, status="completed",
                            message=summary, progress=100)
        _log(summary)

    except InterruptedError:
        task_manager.update(task_id, status="stopped", message="任务已停止")
    except Exception as e:
        log.error(f"按网站上传任务失败: {e}")
        task_manager.update(task_id, status="failed", message=f"失败: {e}")
        task_manager.add_log(task_id, f"任务失败: {e}", "error")
