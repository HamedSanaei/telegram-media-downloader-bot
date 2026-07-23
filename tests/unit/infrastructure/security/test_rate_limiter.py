from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from telegram_media_bot.domain.errors import PolicyBackendError
from telegram_media_bot.infrastructure.security.redis_rate_limiter import RedisRateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.count = 0
        self.closed = False

    async def eval(self, *_args: object) -> int:
        self.count += 1
        return self.count

    async def aclose(self) -> None:
        self.closed = True


async def test_rate_limiter_enforces_fixed_window_limit() -> None:
    redis = FakeRedis()
    limiter = RedisRateLimiter(cast(Redis, cast(Any, redis)))
    assert await limiter.allow(42, 2)
    assert await limiter.allow(42, 2)
    assert not await limiter.allow(42, 2)
    await limiter.close()
    assert redis.closed


async def test_rate_limiter_fails_closed_when_redis_is_unavailable() -> None:
    class BrokenRedis(FakeRedis):
        async def eval(self, *_args: object) -> int:
            raise RedisConnectionError("offline")

    limiter = RedisRateLimiter(cast(Redis, cast(Any, BrokenRedis())))
    with pytest.raises(PolicyBackendError):
        await limiter.allow(42, 2)
