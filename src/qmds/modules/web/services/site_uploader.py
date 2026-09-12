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
5. 数据 ID 同时回写 网站信息.xlsx（存在时）：按网站行的「主数据ID」与
   「补充数据ID」两列分别记录（各逗号分隔），行匹配「网站（文件夹）」
   （审核应用后为域名）或「域名」列；只记录本次上传成功的 ID。

配置存放在 {data_dir}/config/site_upload_config.json，
包含 login_url / upload_page_url / username / password 四项。
"""

import json
import os
import re
from pathlib import Path

from qmds.modules.web.services.site_review import DATA_PREFIX
from qmds.modules.web.task_manager import task_manager
from qmds.utils import winpath
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
    if not winpath.is_file(file_path):
        raise FileNotFoundError("文件不存在")
    return file_path


def _rglob_xlsx(folder: Path) -> list[Path]:
    """递归列出文件夹下所有 .xlsx（超长路径安全）

    数据分配输出的分卷文件路径可能超过 Windows MAX_PATH(260)，Path.rglob
    对超长路径会漏文件/抛错；os.walk（基于 os.scandir）配合扩展长度前缀
    可以正常枚举。返回路径保持入参的相对/绝对形态（不做绝对化）。
    """
    folder = Path(folder)
    top = winpath.long_path(folder)
    out: list[Path] = []
    for root, _dirs, names in os.walk(top):
        # relpath 还原相对/绝对形式（保持入参路径形态，不引入前缀）
        rel = os.path.relpath(root, top)
        base = folder if rel == "." else folder / Path(rel)
        for n in names:
            if n.lower().endswith(".xlsx"):
                out.append(base / n)
    return out


def collect_xlsx_files(target) -> list[Path]:
    """收集目标（文件或文件夹）中的所有 .xlsx（含子文件夹，跳过 ~$ 临时文件）

    按完整路径自然排序（part1, part2, ..., part10），保证上传顺序与分卷编号一致。
    """
    path = Path(target)
    if winpath.is_file(path):
        if path.suffix.lower() == ".xlsx" and not path.name.startswith("~$"):
            return [path]
        return []
    if not winpath.is_dir(path):
        raise FileNotFoundError("目标路径不存在")
    files = [p for p in _rglob_xlsx(path) if not p.name.startswith("~$")]
    files.sort(key=_natural_key)
    return files


def collect_site_tables(folder) -> list[Path]:
    """收集网站文件夹中以 data_ 开头的数据表（主数据在前，补充数据按分卷顺序）

    审核应用后网站数据表名为 data_main{分类}_part{N}_*.xlsx（主数据）与
    data_{分类}_supp_part{N}_*.xlsx（补充数据）；其他文件（分类统计.xlsx、
    网站信息.xlsx 等）不上传。

    排序：主数据表（data_main 前缀）必须排在补充数据之前 —— ERP 站群
    系统要求先上传主数据建立站点基础，再上传补充数据（否则服务器返回
    「表错了」）。组内按自然排序（part1 -> part10）。
    """
    folder = Path(folder)
    if not winpath.is_dir(folder):
        return []
    # 只看文件夹直接子文件（非递归，与原 iterdir 语义一致）；
    # os.scandir 配合扩展长度前缀，超长路径也能枚举
    files: list[Path] = []
    try:
        with os.scandir(winpath.long_path(folder)) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".xlsx"):
                    files.append(folder / e.name)
    except OSError:
        return []
    files = [p for p in files
             if p.name.startswith(DATA_PREFIX)
             and not p.name.startswith("~$")]

    def _rank(p: Path) -> tuple:
        # 剥离 data_ 前缀后以 main 开头 = 主数据表，排最前
        base = p.name[len(DATA_PREFIX):] if p.name.startswith(DATA_PREFIX) else p.name
        return (0 if base.startswith("main") else 1, _natural_key(p.name))

    files.sort(key=_rank)
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
    if not winpath.is_dir(root):
        return []
    sites: list[dict] = []
    queue = [root]
    while queue:
        cur = queue.pop(0)
        try:
            # os.scandir 枚举（超长路径安全；深层文件夹可能超 260 字符）
            with os.scandir(winpath.long_path(cur)) as it:
                names = [e.name for e in it if e.is_dir()]
        except OSError:
            continue
        entries = sorted((cur / n for n in names),
                         key=lambda p: _natural_key(p.name))
        for entry in entries:
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


# ── ERP 兼容格式转换（inlineStr -> sharedStrings）──────────────
# 本机 openpyxl(3.1.x) 写出的 xlsx 全部使用内联字符串（<c t="inlineStr">
# <is><t>文本</t></is>）且不含 xl/sharedStrings.xml；ERP 站群的服务端
# 解析器不接受该写法，上传直接返回「表错了」。老 BB 工具的批量拆表输出
# 之所以能上传，是因为拆表后用 Excel/WPS 重新保存过（save_with_xlwings，
# 注释原话「以解决兼容性问题」）——重存把字符串转成了共享字符串表。
# 此处在上传前用纯 Python 完成同样的转换（不依赖本机 office）：
#   1. 内联字符串 -> 共享字符串表（新增 xl/sharedStrings.xml，同步更新
#      [Content_Types].xml 与 xl/_rels/workbook.xml.rels）；
#   2. 工作表名统一为 Sheet（老工具拆表输出的工作表名）。
# 转换后数据与原文件逐值校验一致，再原子替换原文件；已是兼容格式时不动。
#
# 修复路径：早期版本的转换没有解码数字字符引用，已转换过的文件
# sharedStrings 中残留 &#NNNN; 形态（ERP 字符串比较列名失败）。此处
# 检测已存在的 sharedStrings 中的数字字符引用并解码为原始字符——
# 解码不改变 XML 语义与共享索引，可安全作用于任何形态的文件。
#
# rels 规范化：魔改 openpyxl 把 Relationship 写成 Type/Target/Id 属性
# 顺序且 worksheet Target 为包根绝对路径（/xl/worksheets/sheet1.xml）；
# Excel/WPS 标准形态是 Id/Type/Target 顺序 + 相对路径。ERP 的 PHP
# 解析器按属性顺序正则匹配、按目录拼接解析路径，两种偏差都会导致
# 找不到 worksheet 部件（表错了）。此处统一规范化所有 .rels。

_SST_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_SST_CT = ("application/vnd.openxmlformats-officedocument"
           ".spreadsheetml.sharedStrings+xml")
_SST_REL = ("http://schemas.openxmlformats.org/officeDocument/2006"
            "/relationships/sharedStrings")

# openpyxl 输出的内联字符串单元格: <c r="A1" t="inlineStr"><is><t>文本</t></is></c>
_INLINE_CELL_RE = re.compile(
    r'<c\b([^>]*?)\st="inlineStr"([^>]*)>'
    r'<is><t(?:\s+xml:space="preserve")?>(.*?)</t></is></c>', re.S)
# 空字符串单元格: <c r="C2" t="inlineStr"></c>（无 <is> 内容）-> 普通空单元格
_EMPTY_INLINE_CELL_RE = re.compile(r'<c\b([^>]*?)\st="inlineStr"([^>]*)></c>')
_SHEET_NAME_RE = re.compile(r'(<sheet\b[^>]*?)\sname="[^"]*"([^>]*/>)')
_SHEET_TAG_RE = re.compile(r"<sheet\b[^>]*/>")


def _ensure_decl(xml: str) -> str:
    """给缺少 XML 声明的部分补上标准声明（openpyxl 输出常省略）"""
    if xml.lstrip().startswith("<?xml"):
        return xml
    return _XML_DECL + xml


# 数字字符引用（&#NNNN; / &#xHH;）——与原始字符在 XML 语义上完全等价，
# 但 ERP 的 PHP 解析器做字符串级比较，不解码实体：中文列名（自定义分类 等）
# 必须以原始 UTF-8 字符写入 sharedStrings（与老工具 WPS 重存后的形态一致）。
# 注意 &amp; &lt; &gt; &quot; &apos; 属于 XML 结构转义，必须保留不解码。
_DEC_CHAR_REF_RE = re.compile(r"&#(?:x([0-9A-Fa-f]+)|(\d+));")


def _decode_char_refs(text: str) -> str:
    """把数字字符引用解码为原始字符（&#20013; -> 中）"""
    return _DEC_CHAR_REF_RE.sub(
        lambda m: chr(int(m.group(1), 16) if m.group(1) else int(m.group(2))),
        text)


_REL_TAG_RE = re.compile(r"<Relationship\b[^>]*/>")
_REL_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')


def _rels_base(part_name: str) -> str:
    """rels 部件对应源部件的所在目录（带尾部斜杠）

    xl/_rels/workbook.xml.rels -> 'xl/'（源部件 xl/workbook.xml）
    _rels/.rels                -> ''  （包根）
    """
    d = part_name.rsplit("/", 1)[0] if "/" in part_name else ""
    if d == "_rels":
        return ""
    if d.endswith("/_rels"):
        return d[: -len("/_rels")] + "/"
    return ""


def _normalize_rels(xml: str, base: str) -> str:
    """Relationship 规范化为 Excel/WPS 标准形态（幂等）

    - 属性顺序统一为 Id, Type, Target（魔改 openpyxl 写成 Type/Target/Id，
      ERP 的 PHP 解析器按顺序做正则匹配会失败）；
    - 包根绝对 Target（/xl/worksheets/sheet1.xml）转为相对路径
      （worksheets/sheet1.xml，按 base 剥离前缀；ERP 按目录拼接解析）。
    """
    def _repl(m):
        attrs = dict(_REL_ATTR_RE.findall(m.group(0)))
        rid = attrs.get("Id", "")
        typ = attrs.get("Type", "")
        tgt = attrs.get("Target", "")
        if tgt.startswith("/") and base and tgt[1:].startswith(base):
            tgt = tgt[len(base) + 1:]
        mode = attrs.get("TargetMode", "")
        mode_attr = f' TargetMode="{mode}"' if mode else ""
        return f'<Relationship Id="{rid}" Type="{typ}" Target="{tgt}"{mode_attr}/>'

    return _REL_TAG_RE.sub(_repl, xml)


def make_erp_compatible(path, log_fn=None) -> bool:
    """把数据表转换为 ERP 站群可读格式（原文件被原子替换，幂等）

    见上方模块注释。返回是否发生修改；已是兼容格式返回 False。
    转换失败（校验不一致/文件被占用等）抛出异常，原文件保持不变。
    """
    import os
    import zipfile

    import pandas as pd

    path = Path(path)
    with zipfile.ZipFile(winpath.long_path(path)) as z:
        names = z.namelist()
        infos = {i.filename: i for i in z.infolist()}
        contents = {n: z.read(n) for n in names}

    sheet_names = [n for n in names
                   if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    sheet_xmls = {n: contents[n].decode("utf-8") for n in sheet_names}
    needs_strings = any('t="inlineStr"' in x for x in sheet_xmls.values())
    ss_name = "xl/sharedStrings.xml"
    # 修复路径：已存在的 sharedStrings 中含数字字符引用 -> 解码为原始字符
    # （早期版本转换残留的坏形态；解码不改变语义与索引，安全）
    fix_ss_refs = (ss_name in names
                   and _DEC_CHAR_REF_RE.search(contents[ss_name]
                                               .decode("utf-8")) is not None)
    if needs_strings and ss_name in names:
        # 文件同时含共享字符串与内联字符串（混合格式，本链路不会产生）：
        # 不动字符串部分，避免破坏已有索引（引用解码仍执行）
        if log_fn:
            log_fn(f"{path.name}: 已存在 sharedStrings 且含内联字符串，"
                   f"跳过格式转换", "warning")
        needs_strings = False
    if fix_ss_refs and log_fn:
        log_fn(f"{path.name}: sharedStrings 含数字字符引用，"
               f"解码为原始字符（ERP 字符串比较需要）")

    wb_xml = contents["xl/workbook.xml"].decode("utf-8")
    sheet_tags = _SHEET_TAG_RE.findall(wb_xml)
    rename_sheet = (len(sheet_tags) == 1
                    and 'name="Sheet"' not in sheet_tags[0])

    # ── rels 规范化：属性顺序（Id, Type, Target）+ 绝对 Target → 相对 ──
    rels_parts: dict[str, str] = {}
    rels_changed = False
    for n in names:
        if not n.endswith(".rels"):
            continue
        xml = _ensure_decl(contents[n].decode("utf-8"))
        fixed = _normalize_rels(xml, _rels_base(n))
        if fixed != xml:
            rels_changed = True
            if log_fn:
                log_fn(f"{path.name}: {n} Relationship 已规范化"
                       f"（Id/Type/Target 顺序与相对路径）")
        rels_parts[n] = fixed

    if (not needs_strings and not rename_sheet and not fix_ss_refs
            and not rels_changed):
        return False

    # ── 1. 内联字符串 -> 共享字符串 ──
    strings: list[str] = []
    index: dict[str, int] = {}
    total_refs = 0

    def _repl(m):
        nonlocal total_refs
        # 数字实体 -> 原始 UTF-8 字符（ERP 按字符串比较列名）
        text = _decode_char_refs(m.group(3))
        idx = index.get(text)
        if idx is None:
            idx = len(strings)
            strings.append(text)
            index[text] = idx
        total_refs += 1
        return f'<c{m.group(1)} t="s"{m.group(2)}><v>{idx}</v></c>'

    new_sheets = {}
    if needs_strings:
        for n, xml in sheet_xmls.items():
            converted, _ = _INLINE_CELL_RE.subn(_repl, xml)
            # 空内联单元格 -> 普通空单元格（<c r="C2"/>，与 Excel 写法一致）
            converted = _EMPTY_INLINE_CELL_RE.sub(
                lambda m: f'<c{m.group(1).rstrip()}/>', converted)
            if 't="inlineStr"' in converted:
                raise ValueError(f"{path.name}: 存在无法识别的内联字符串单元格")
            new_sheets[n] = _ensure_decl(converted)

    # ── 2. 工作表名 -> Sheet ──
    new_wb = wb_xml
    if rename_sheet:
        new_wb = _SHEET_NAME_RE.sub(r'\1 name="Sheet"\2', wb_xml, count=1)

    # ── 3. sharedStrings 部件与引用注册 ──
    parts = [f'<sst xmlns="{_SST_NS}" count="{total_refs}"'
             f' uniqueCount="{len(strings)}">']
    for text in strings:
        preserve = ' xml:space="preserve"' if text != text.strip() else ''
        parts.append(f'<si><t{preserve}>{text}</t></si>')
    parts.append("</sst>")
    ss_xml = _XML_DECL + "".join(parts)

    ct_xml = _ensure_decl(
        contents["[Content_Types].xml"].decode("utf-8"))
    if needs_strings and "/xl/sharedStrings.xml" not in ct_xml:
        ct_xml = ct_xml.replace(
            "</Types>",
            f'<Override PartName="/xl/sharedStrings.xml" ContentType="{_SST_CT}"/>'
            "</Types>")

    rels_name = "xl/_rels/workbook.xml.rels"
    rels_xml = rels_parts[rels_name]
    if needs_strings and "sharedStrings.xml" not in rels_xml:
        max_rid = max((int(m) for m in re.findall(r'Id="rId(\d+)"', rels_xml)),
                      default=0)
        rels_xml = rels_xml.replace(
            "</Relationships>",
            f'<Relationship Id="rId{max_rid + 1}" Type="{_SST_REL}"'
            f' Target="sharedStrings.xml"/></Relationships>')

    # ── 4. 写临时文件 -> 数据校验 -> 原子替换 ──
    tmp = path.with_name(path.name + ".erp_tmp")
    try:
        # 临时文件与原文件同目录：路径超 260 时同样需要扩展长度前缀
        with zipfile.ZipFile(winpath.long_path(tmp), "w") as zout:
            for n in names:
                if n in new_sheets:
                    data = new_sheets[n].encode("utf-8")
                elif n == "xl/workbook.xml":
                    data = _ensure_decl(new_wb).encode("utf-8")
                elif n == "[Content_Types].xml":
                    data = ct_xml.encode("utf-8")
                elif n == rels_name:
                    data = rels_xml.encode("utf-8")
                elif n in rels_parts:
                    data = rels_parts[n].encode("utf-8")
                elif n == ss_name and fix_ss_refs:
                    # 修复路径：解码 sharedStrings 中的数字字符引用
                    data = _ensure_decl(
                        _decode_char_refs(
                            contents[n].decode("utf-8"))).encode("utf-8")
                elif n.endswith((".xml", ".rels")):
                    # 其余 XML 部件统一补声明（老工具重存后的形态）
                    try:
                        data = _ensure_decl(
                            contents[n].decode("utf-8")).encode("utf-8")
                    except UnicodeDecodeError:
                        data = contents[n]
                else:
                    data = contents[n]
                info = zipfile.ZipInfo(n, date_time=infos[n].date_time)
                info.compress_type = infos[n].compress_type
                info.external_attr = infos[n].external_attr
                zout.writestr(info, data)
            if needs_strings:
                info = zipfile.ZipInfo("xl/sharedStrings.xml",
                                       date_time=infos[names[0]].date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(info, ss_xml.encode("utf-8"))

        # 逐值校验：转换前后 pandas 读取结果必须完全一致
        before = pd.read_excel(winpath.long_path(path), engine="openpyxl")
        after = pd.read_excel(winpath.long_path(tmp), engine="openpyxl")
        if before.shape != after.shape or not before.equals(after):
            raise ValueError("转换后数据校验不一致，放弃替换")

        # 文件可能被杀毒/同步软件瞬时占用：短暂重试后仍失败才放弃
        import time as _time
        for attempt in range(4):
            try:
                winpath.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == 3:
                    raise
                _time.sleep(0.5)
    finally:
        if winpath.exists(tmp):
            winpath.remove(tmp)


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
    # 分卷文件路径可能超过 Windows 260 字符限制，open 需扩展长度前缀
    with open(winpath.long_path(file_path), "rb") as f:
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


def _group_uploaded(target, uploaded: list) -> dict:
    """按数据文件夹分组上传结果

    返回 {文件夹: [(rank, 上传顺序, 数据ID), ...]}；rank: 0=主数据, 1=补充数据。

    识别规则（与数据分配/审核应用后的命名对应）：
    - data_ 前缀剥离后以 main 开头 = 主数据表；
    - 旧版结构（*_split 文件夹）: 补充数据文件夹 {X}_补充_split/ 归入
      对应主数据文件夹 {X}_split/；主数据文件夹中的非补充文件为主数据；
    - 其余为补充数据/额外补充。
    """
    target = Path(target)
    # 文件夹 -> [(rank, 上传顺序, ID)]；rank: 0=主数据, 1=补充数据
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
    return entries


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
    entries = _group_uploaded(target, uploaded)

    written = []
    for folder, items in entries.items():
        items.sort(key=lambda t: (t[0], t[1]))  # 主数据在前，同组按上传顺序
        txt = folder / ID_TXT_NAME
        # 网站文件夹路径可能超过 Windows 260 字符，写入用扩展长度前缀
        with open(winpath.long_path(txt), "w", encoding="utf-8") as f:
            f.write("\n".join(pid for _, _, pid in items) + "\n")
        written.append(txt)
    return written


def update_info_data_ids(root, uploaded: list, log_fn=None):
    """把上传返回的数据 ID 回写 网站信息.xlsx（主数据ID/补充数据ID 各一列）

    按数据文件夹分组（与 数据ID.txt 相同规则），组内 ID 按主数据在前、
    上传顺序（即分卷编号顺序）排列，主数据与补充数据分别以","连接写入
    「主数据ID」「补充数据ID」两列。行匹配：表格行「网站（文件夹）」
    （审核应用后为域名）或「域名」列等于数据文件夹名。

    只记录本次上传成功的 ID（覆盖旧值，与 数据ID.txt 的覆盖语义一致）；
    未找到 网站信息.xlsx 时静默跳过（返回 (None, 0)），上传不依赖信息表。

    Args:
        root: 上传目标根目录（网站信息.xlsx 在其中定位，可为任意上级目录）
        uploaded: [(文件路径, 数据ID), ...] 按上传顺序
        log_fn: 可选日志函数 fn(msg, level)

    Returns:
        (info_path, 更新行数)；无信息表/读取失败/无匹配行时更新行数为 0
    """
    from qmds.modules.web.services.site_info_generator import (
        INFO_FILE_NAME,
        _write_info_excel,
        read_site_info_excel,
    )
    from qmds.modules.web.services.site_review import locate_info_file

    info_path = locate_info_file(root)
    if info_path is None:
        return None, 0
    try:
        rows = read_site_info_excel(info_path)
    except Exception as e:
        if log_fn:
            log_fn(f"读取 {INFO_FILE_NAME} 失败（数据ID未回写）: {e}", "warning")
        return info_path, 0

    # 数据文件夹名 -> {"main": [ID...], "supp": [ID...]}（组内主数据在前）
    grouped: dict[str, dict[str, list[str]]] = {}
    for folder, items in _group_uploaded(root, uploaded).items():
        items.sort(key=lambda t: (t[0], t[1]))
        grouped[folder.name] = {
            "main": [pid for rank, _, pid in items if rank == 0],
            "supp": [pid for rank, _, pid in items if rank == 1],
        }

    updated = 0
    unmatched = set(grouped)
    for row in rows:
        # 「网站（文件夹）」优先（审核应用后同步为域名），「域名」列兜底
        keys = (str(row.get("网站（文件夹）") or "").strip(),
                str(row.get("域名") or "").strip())
        for key in keys:
            if key and key in unmatched:
                row["主数据ID"] = ",".join(grouped[key]["main"])
                row["补充数据ID"] = ",".join(grouped[key]["supp"])
                unmatched.discard(key)
                updated += 1
                break
    if unmatched and log_fn:
        log_fn(f"数据ID未能回写 {INFO_FILE_NAME}（表格中未找到对应网站行）: "
               + ", ".join(sorted(unmatched)), "warning")
    if updated:
        try:
            _write_info_excel(info_path, rows)
        except Exception as e:
            if log_fn:
                log_fn(f"回写 {INFO_FILE_NAME} 失败: {e}", "warning")
            return info_path, 0
    return info_path, updated


def run_upload_task(task_id: str, target_path, config: dict | None = None):
    """站群系统上传后台任务体：登录 -> 逐个上传 -> 数据ID保存到txt + 回写网站信息"""
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

        # ── ERP 兼容格式转换（inlineStr -> sharedStrings）──
        for fp in files:
            try:
                if make_erp_compatible(fp):
                    _log(f"已转为 ERP 兼容格式（共享字符串）: {fp.name}")
            except Exception as e:
                _log(f"{fp.name}: 兼容格式转换失败（按原样上传）: {e}", "warning")

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
        info_rows_updated = 0
        if uploaded:
            task_manager.update(task_id, progress=95,
                                message=f"保存 {len(uploaded)} 个数据ID到 txt 文件...")
            written_txts = save_upload_ids(target_path, uploaded)
            for txt in written_txts:
                _log(f"数据ID已保存: {txt.parent.name}/{txt.name}")
            # 数据 ID 回写 网站信息.xlsx（主数据ID/补充数据ID 两列，存在时）
            try:
                info_path, info_rows_updated = update_info_data_ids(
                    target_path, uploaded, _log)
                if info_rows_updated:
                    _log(f"数据ID已回写 {info_path.name}: {info_rows_updated} 行"
                         "（主数据ID/补充数据ID）")
            except Exception as e:
                _log(f"数据ID回写 网站信息.xlsx 失败: {e}", "warning")

        summary = (f"完成: 上传 {len(uploaded)}/{len(files)} 个表格"
                   f"（失败 {failed}），数据ID已保存到 {len(written_txts)} 个 txt 文件")
        if info_rows_updated:
            summary += f"，{info_rows_updated} 行网站信息已回写数据ID"
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
    上传文件夹内以 data_ 开头的数据表，且主数据表（data_main 前缀）先
    上传、补充数据表（data_*_supp_part*）后上传 —— ERP 要求先传主数据
    建立站点基础，再传补充数据。全部完成后在任务日志中按网站分组输出
    上传返回的数据 ID，并把各网站的数据 ID 写入其文件夹内的
    数据ID.txt（主数据 ID 在最前）；同时回写所选文件夹下的
    网站信息.xlsx（存在时）：每行的「主数据ID」「补充数据ID」两列
    分别记录本次上传的主数据/补充数据 ID（各逗号分隔）。
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
            n_main = sum(1 for t in tables
                         if t.name[len(DATA_PREFIX):].startswith("main"))
            _log(f"[{si}/{len(chosen)}] ▶ 网站 {name}: 开始上传 "
                 f"{len(tables)} 个数据表（主数据 {n_main} 表先传，"
                 f"补充数据 {len(tables) - n_main} 表）")

            # ── ERP 兼容格式转换（inlineStr -> sharedStrings）──
            for fp in tables:
                try:
                    if make_erp_compatible(fp):
                        _log(f"  ⚙ {fp.name}: 已转为 ERP 兼容格式（共享字符串）")
                except Exception as e:
                    _log(f"  ⚠ {fp.name}: 兼容格式转换失败（按原样上传）: {e}",
                         "warning")

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
                # 数据 ID 回写 网站信息.xlsx（主数据ID/补充数据ID 两列，存在时）
                try:
                    info_path, n_rows = update_info_data_ids(
                        root, site_uploaded, _log)
                    if n_rows:
                        _log(f"网站 {name}: 数据ID已回写 {info_path.name}"
                             "（主数据ID/补充数据ID）")
                except Exception as e:
                    _log(f"网站 {name}: 数据ID回写 网站信息.xlsx 失败: {e}",
                         "warning")
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
