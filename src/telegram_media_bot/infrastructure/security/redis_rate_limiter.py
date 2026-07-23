from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from telegram_media_bot.application.ports.rate_limiter import RateLimiter
from telegram_media_bot.domain.errors import PolicyBackendError

_FIXED_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis: Redis, *, prefix: str = "media-bot:rate") -> None:
        self._redis = redis
        self._prefix = prefix

    @classmethod
    def create(cls, url: str) -> RedisRateLimiter:
        return cls(Redis.from_url(url, decode_responses=False))

    async def allow(self, user_id: int, limit: int) -> bool:
        key = f"{self._prefix}:{user_id}"
        try:
            operation = cast(Awaitable[Any], self._redis.eval(_FIXED_WINDOW_SCRIPT, 1, key, "60"))
            count = await operation
        except RedisError as exc:
            raise PolicyBackendError("Rate-limit backend is unavailable") from exc
        return int(count) <= limit

    async def close(self) -> None:
        await self._redis.aclose()
