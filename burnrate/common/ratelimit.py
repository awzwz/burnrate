"""Fixed-window rate limits. Redis (Valkey) in the cluster so every replica shares the counters; memory locally."""
from __future__ import annotations

import time

from burnrate.common.config import settings
from burnrate.common.errors import RateLimited

_mem: dict[str, tuple[int, float]] = {}
_redis = None


async def _client():
    global _redis
    if _redis is None:
        import redis.asyncio as redis

        _redis = redis.from_url(settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _redis


async def hit(key: str, limit: int, window_s: int) -> bool:
    """Count one request. False when the limit for the current window is exceeded. Fails open if Redis is down."""
    st = settings()
    if st.is_test:
        return True
    bucket = f"rl:{key}:{int(time.time() // window_s)}"
    if st.redis_url:
        try:
            r = await _client()
            n = await r.incr(bucket)
            if n == 1:
                await r.expire(bucket, window_s + 5)
            return n <= limit
        except Exception:  # noqa: BLE001 - a broken limiter must not take the product down
            return True
    n, exp = _mem.get(bucket, (0, time.time() + window_s))
    if len(_mem) > 20_000:
        now = time.time()
        for k in [k for k, (_, e) in _mem.items() if e < now]:
            _mem.pop(k, None)
    _mem[bucket] = (n + 1, exp)
    return n + 1 <= limit


async def check(key: str, limit: int, window_s: int, message: str = "Too many requests. Try again in a minute.") -> None:
    if not await hit(key, limit, window_s):
        raise RateLimited(message)
