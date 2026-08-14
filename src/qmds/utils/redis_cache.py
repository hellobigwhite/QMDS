"""Redis 缓存工具 — 支持降级到内存缓存"""

import functools
import hashlib
import json
from typing import Any, Optional

from qmds.config import settings
from qmds.utils.logger import get_logger

log = get_logger("redis_cache")


class _InMemoryCache:
    """Redis 不可用时的内存缓存降级方案"""

    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[str]:
        item = self._store.get(key)
        if item is None:
            return None
        value, expire_ts = item
        import time
        if expire_ts and time.time() > expire_ts:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ex: int = 300):
        import time
        self._store[key] = (value, time.time() + ex if ex else 0)

    def delete(self, key: str):
        self._store.pop(key, None)

    def keys(self, pattern: str) -> list[str]:
        import fnmatch
        return [k for k in self._store if fnmatch.fnmatch(k, pattern)]

    def ping(self) -> bool:
        return True

    def flushdb(self):
        self._store.clear()


class RedisCache:
    """统一缓存接口，自动降级到内存缓存"""

    def __init__(self):
        self._redis = None
        self._fallback = _InMemoryCache()
        self._connected = False
        self._tried = False

    def _get_redis(self):
        if self._tried:
            return self._redis
        self._tried = True
        try:
            import redis
            self._redis = redis.Redis(
                host=getattr(settings, 'redis_host', 'localhost'),
                port=getattr(settings, 'redis_port', 6379),
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            self._redis.ping()
            self._connected = True
            log.info("Redis 连接成功")
        except Exception as e:
            log.warning(f"Redis 不可用，降级到内存缓存: {e}")
            self._redis = None
            self._connected = False
        return self._redis

    @property
    def backend(self):
        if self._get_redis() is not None:
            return self._redis
        return self._fallback

    @property
    def is_redis(self) -> bool:
        self._get_redis()
        return self._connected

    def get(self, key: str) -> Optional[Any]:
        try:
            raw = self.backend.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: int = 300):
        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
            self.backend.set(key, raw, ex=ttl)
        except Exception as e:
            log.debug(f"缓存写入失败: {e}")

    def delete(self, key: str):
        try:
            self.backend.delete(key)
        except Exception:
            pass

    def clear_prefix(self, prefix: str):
        try:
            pattern = f"qmds:{prefix}*"
            if self._connected and self._redis:
                cursor = 0
                while True:
                    cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                    if keys:
                        self._redis.delete(*keys)
                    if cursor == 0:
                        break
            else:
                keys = self._fallback.keys(pattern)
                for k in keys:
                    self._fallback.delete(k)
        except Exception as e:
            log.debug(f"缓存清理失败: {e}")

    def clear_all(self):
        self.clear_prefix("")


_cache = RedisCache()


def get_cache() -> RedisCache:
    return _cache


def cache_key(*parts: Any) -> str:
    raw = ":".join(str(p) for p in parts)
    return f"qmds:{hashlib.md5(raw.encode()).hexdigest()}"


def cached(prefix: str, ttl: int = 300):
    """装饰器：自动缓存函数返回值"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            c = get_cache()
            skip_cache = kwargs.pop('_skip_cache', False)
            key = cache_key(prefix, *args, *sorted(kwargs.items()))
            if not skip_cache:
                result = c.get(key)
                if result is not None:
                    return result
            result = func(*args, **kwargs)
            if result is not None:
                c.set(key, result, ttl=ttl)
            return result
        wrapper.cache_clear = lambda: get_cache().clear_prefix(prefix)
        return wrapper
    return decorator


def invalidate_stats():
    """清除所有统计缓存"""
    c = get_cache()
    c.clear_prefix("stats")
    c.clear_prefix("dashboard")
