import sys
from pathlib import Path
from typing import Optional

from loguru import logger

from qmds.config import settings


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
