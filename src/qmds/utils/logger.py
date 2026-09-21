import logging
import sys
from pathlib import Path
from typing import Optional

from loguru import logger

from qmds.config import settings

# urllib3 解析到个别服务器/代理（如本地 Clash 隧道）返回的"格式略不规范"的
# 响应头（MissingHeaderBodySeparatorDefect 等）时，会打一条
# "Failed to parse headers (url=...)" 警告并附完整 traceback（exc_info=True）。
# 该异常 urllib3 内部会捕获并继续正常使用响应，不影响任何功能，只是纯日志噪音。
# 把这个 stdlib logger 提到 ERROR 级别：保留真实错误，去掉这条带 traceback 的警告。
logging.getLogger("urllib3.connection").setLevel(logging.ERROR)


def setup_logger(
    level: Optional[str] = None,
    log_file: Optional[Path] = None,
    rotation: str = "100 MB",
    retention: str = "30 days",
):
    logger.remove()

    logger.add(
        sys.stderr,
        level=level or settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}",
    )

    log_path = log_file or settings.log_file
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            level="DEBUG",
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
        )


def get_logger(name: str):
    return logger.bind(name=name)
