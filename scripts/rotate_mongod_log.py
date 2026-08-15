"""mongod 日志轮转与归档清理(计划任务每日执行)

1. 对 admin 库执行 logRotate 命令(mongod 原生轮转,安全,不需重启)
   配合 systemLog.logAppend:true 时,logRotate 会将当前日志重命名为
   mongod.log.<时间戳>,并新建 mongod.log 继续写入
2. 压缩所有历史轮转文件(mongod.log.*)为 .gz,减少磁盘占用
3. 删除超过保留天数的归档

用法: python scripts/rotate_mongod_log.py [保留天数,默认14]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gzip
import shutil

from qmds.config import settings

LOG_DIR = Path(r"E:\MongoDB\log")
CURRENT_LOG = LOG_DIR / "mongod.log"
RETENTION_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14


def rotate():
    from pymongo import MongoClient
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    res = client.admin.command("logRotate")
    client.close()
    return res.get("ok") == 1


def compress_rotated_files():
    """压缩所有未被压缩的历史轮转文件"""
    count = 0
    for f in LOG_DIR.glob("mongod.log.2*"):
        if f.suffix == ".gz" or not f.is_file():
            continue
        gz_path = f.with_suffix(f.suffix + ".gz")
        with open(f, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        f.unlink()
        count += 1
    return count


def cleanup_old_archives():
    """删除超过保留期的归档(按文件修改时间)"""
    import time
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for f in LOG_DIR.glob("mongod.log.*.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


if __name__ == "__main__":
    if not rotate():
        print("ERROR: logRotate 命令失败(MongoDB 不可达?)")
        sys.exit(1)
    compressed = compress_rotated_files()
    removed = cleanup_old_archives()
    print(f"OK: 轮转成功, 压缩 {compressed} 个历史文件, 清理 {removed} 个过期归档(>{RETENTION_DAYS}天)")
