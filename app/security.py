"""鉴权 + 限速：保护 polly-server 的 AI 端点不被白嫖。

设计取舍（demo / Phase B 阶段）：
- 鉴权用单一共享 Bearer token（POLLY_API_KEY），iOS 端从 xcconfig 注入。
  这不是防逆向（key 在 App 内可被提取），是防"路过的人 curl 几下烧光额度"。
  上架前换成 Apple ID / 设备签名验证。
- 限速窗口：默认进程内内存（单 worker dev 用）；REDIS_URL 配上后切到 Redis ZSET
  实现的跨进程滑动窗口（多 worker / 多机部署用）。
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from functools import lru_cache
from threading import Lock
from typing import Protocol

from fastapi import HTTPException, Request, status

from app.config import get_settings


async def require_api_key(request: Request) -> None:
    """FastAPI dependency：校验 Authorization: Bearer <key>。
    POLLY_API_KEY 为空时跳过校验（本地裸开发，绝不能用于生产）。
    """
    settings = get_settings()
    if not settings.polly_api_key:
        return

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth[7:].strip()
    if not hmac.compare_digest(token, settings.polly_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key",
        )


# ---------- 限速：内存滑动窗口 ----------

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_rule(rule: str) -> tuple[int, int]:
    """'60/hour' → (60, 3600)。"""
    n_str, _, unit = rule.partition("/")
    unit = unit.strip().rstrip("s")
    if unit not in _UNITS:
        raise ValueError(f"unknown rate limit unit: {unit!r} in {rule!r}")
    return int(n_str), _UNITS[unit]


class _SlidingWindowBackend(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """返回 (是否允许, retry_after_seconds)。"""
        ...


class _MemorySlidingWindow:
    """进程内内存实现。单 worker dev 够用；多 worker 数据不共享。"""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            dq = self._buckets[key]
            cutoff = now - window_seconds
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry = max(1, int(dq[0] + window_seconds - now))
                return False, retry
            dq.append(now)
            return True, 0


class _RedisSlidingWindow:
    """Redis ZSET 实现：score=timestamp，跨进程共享同一个滑动窗口。

    协议：
      ZREMRANGEBYSCORE key -inf (now - window)   # 清过期
      ZCARD key                                  # 当前计数
      ZADD key now now                            # 入桶（先 check 后 add，pipeline 原子化）
      EXPIRE key window                           # 自动清理过期 key
    """

    def __init__(self, redis_url: str) -> None:
        try:
            import redis  # 延迟导入
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "redis not installed. Run: pip install -e '.[redis]'"
            ) from exc
        self._r = redis.Redis.from_url(redis_url, decode_responses=False)
        # 启动时探活一次，让配置错误立刻可见
        self._r.ping()

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        cutoff = now_ms - window_ms
        full_key = f"ratelimit:{key}"

        pipe = self._r.pipeline()
        pipe.zremrangebyscore(full_key, 0, cutoff)
        pipe.zcard(full_key)
        pipe.zadd(full_key, {str(now_ms): now_ms})
        pipe.expire(full_key, window_seconds + 1)
        _, count, _, _ = pipe.execute()

        if count >= limit:
            # 拿到当前桶里最早的 timestamp，算 retry_after
            oldest = self._r.zrange(full_key, 0, 0, withscores=True)
            if oldest:
                oldest_ms = int(oldest[0][1])
                retry = max(1, (oldest_ms + window_ms - now_ms) // 1000)
            else:
                retry = 1
            # 回滚刚 ZADD（不算 hit）
            self._r.zrem(full_key, str(now_ms))
            return False, int(retry)
        return True, 0


@lru_cache
def _backend() -> _SlidingWindowBackend:
    url = get_settings().redis_url
    if url:
        return _RedisSlidingWindow(url)
    return _MemorySlidingWindow()


def _client_ip(request: Request) -> str:
    # 代理后部署时优先用 X-Forwarded-For 的最左 IP
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(rule_attr: str):
    """生成 FastAPI dependency：从 settings 读规则，做 per-IP 滑动窗口限速。
    用法：dependencies=[Depends(rate_limit('rate_limit_ai'))]
    """

    async def _dep(request: Request) -> None:
        settings = get_settings()
        rule = getattr(settings, rule_attr, "")
        if not rule:
            return
        try:
            limit, window = _parse_rule(rule)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        key = f"{rule_attr}:{_client_ip(request)}"
        ok, retry = _backend().hit(key, limit, window)
        if not ok:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded ({rule})",
                headers={"Retry-After": str(retry)},
            )

    return _dep
