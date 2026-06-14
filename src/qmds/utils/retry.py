import asyncio
import time
from functools import wraps
from typing import Callable, Type

from qmds.core.exceptions import RateLimitError, ScrapeError


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 120.0,
    exceptions: tuple[Type[Exception], ...] = (ScrapeError,),
):
    """指数退避重试装饰器"""

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except RateLimitError:
                    raise
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        time.sleep(delay)
            raise last_exception  # type: ignore

        return wrapper

    return decorator


def async_retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 120.0,
    exceptions: tuple[Type[Exception], ...] = (ScrapeError,),
):
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except RateLimitError:
                    raise
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        await asyncio.sleep(delay)
            raise last_exception  # type: ignore

        return wrapper

    return decorator
