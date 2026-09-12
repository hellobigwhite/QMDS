r"""Windows 长路径（>260 字符）文件 I/O 支持

数据分配的输出路径形如
  data/exports/{日期}/{数据集}/{数据集}_{id}_分配_{时间戳}/{分类名}/
  main{分类名}_part1_{随机}{时间戳}.xlsx
分类名来自商品分类（如 Pet_Bowls_&_Feeders_Pet_Bowls,_Feeders_&_Waterers），
路径很容易超过 Windows MAX_PATH 260 字符限制 —— 默认（未开启注册表
LongPathsEnabled）open()/os.makedirs 等会报
[Errno 2] No such file or directory，即使文件并不存在（写新文件也失败）。

解决：Windows 下给绝对路径加 \\?\ 扩展长度前缀（open/openpyxl/pandas/
os 模块均支持），其他平台原样返回。逻辑代码继续用普通 Path（.name/.stem/
relative_to 不受影响），只在最终文件 I/O 处转换：
    wb.save(long_path(out))          # openpyxl
    pd.read_excel(long_path(p))      # pandas
    winpath.makedirs(folder)         # 目录
    winpath.is_file(p)               # 存在性检查（Path.is_file 对超长路径
                                     # 会静默返回 False，导致文件"隐身"）
注意：COM 自动化（xlwings/Excel）不支持 \\?\ 前缀，勿用于此类调用。
"""

import os

_EXT_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def long_path(path) -> str:
    r"""返回可直接用于文件 I/O 的路径字符串

    - Windows 绝对路径 -> \\?\F:\a\b.xlsx（超长路径可正常读写）
    - Windows UNC 路径 -> \\?\UNC\server\share\...
    - 其他情况（非 Windows / 已带前缀）-> 规范化字符串
    """
    if os.name != "nt":
        return str(path)
    s = os.path.abspath(os.fspath(path))
    if s.startswith(_EXT_PREFIX):
        return s
    if s.startswith("\\\\"):  # UNC \\server\share\...
        return _UNC_PREFIX + s[2:]
    return _EXT_PREFIX + s


def normal_path(path) -> str:
    r"""去掉 \? / \?UNC 扩展长度前缀，返回普通路径字符串"""
    s = str(path)
    if s.startswith(_UNC_PREFIX):
        return "\\" + s[len(_UNC_PREFIX):]
    if s.startswith(_EXT_PREFIX):
        return s[len(_EXT_PREFIX):]
    return s


def is_file(path) -> bool:
    """超长路径安全的 isfile（Path.is_file 对 >260 路径静默返回 False）"""
    return os.path.isfile(long_path(path))


def is_dir(path) -> bool:
    """超长路径安全的 isdir"""
    return os.path.isdir(long_path(path))


def exists(path) -> bool:
    """超长路径安全的 exists"""
    return os.path.exists(long_path(path))


def makedirs(path, exist_ok=True) -> None:
    """超长路径安全的 makedirs"""
    os.makedirs(long_path(path), exist_ok=exist_ok)


def remove(path) -> None:
    """超长路径安全的删除文件"""
    os.remove(long_path(path))


def replace(src, dst) -> None:
    """超长路径安全的原子替换"""
    os.replace(long_path(src), long_path(dst))


def rename(src, dst) -> None:
    """超长路径安全的重命名（os.rename 语义）"""
    os.rename(long_path(src), long_path(dst))
