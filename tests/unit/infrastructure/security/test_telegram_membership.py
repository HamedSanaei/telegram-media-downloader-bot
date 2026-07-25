from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from redis.asyncio import Redis

from telegram_media_bot.bootstrap.config import RequiredChannelsSection
from telegram_media_bot.domain.errors import PolicyBackendError
from telegram_media_bot.domain.models import RequiredChannel
from telegram_media_bot.infrastructure.security.telegram_membership import (
    TelegramMembershipChecker,
)


class FakeRedis:
    values: dict[str, bytes]
    ttls: dict[str, int]

    def __init__(self) -> None:
        self.values = {}
        self.ttls = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value.encode()
        self.ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def aclose(self) -> None:
        return None


class FakeBot:
    calls = 0
    status = "member"
    is_member = True
    unavailable = False

    async def get_chat_member(self, *, chat_id: int, user_id: int) -> object:
        del chat_id, user_id
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("telegram unavailable")
        return SimpleNamespace(status=self.status, is_member=self.is_member)


async def test_membership_checker_caches_positive_and_force_refreshes() -> None:
    bot = FakeBot()
    checker = TelegramMembershipChecker(
        cast(Bot, cast(Any, bot)),
        cast(Redis, cast(Any, FakeRedis())),
        RequiredChannelsSection(),
    )
    channels = (RequiredChannel(-1001, "Main", "https://t.me/main"),)

    assert await checker.missing_channels(42, channels) == ()
    assert await checker.missing_channels(42, channels) == ()
    assert bot.calls == 1
    assert await checker.missing_channels(42, channels, force_refresh=True) == ()
    assert bot.calls == 2


async def test_membership_checker_fails_closed_on_telegram_error() -> None:
    bot = FakeBot()
    bot.unavailable = True
    checker = TelegramMembershipChecker(
        cast(Bot, cast(Any, bot)),
        cast(Redis, cast(Any, FakeRedis())),
        RequiredChannelsSection(),
    )

    with pytest.raises(PolicyBackendError):
        await checker.missing_channels(
            42,
            (RequiredChannel(-1001, "Main", "https://t.me/main"),),
        )


@pytest.mark.parametrize(
    ("status", "is_member", "accepted"),
    [
        ("creator", True, True),
        ("administrator", True, True),
        ("member", True, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("left", False, False),
        ("kicked", False, False),
    ],
)
async def test_membership_statuses_and_positive_negative_cache_ttls(
    status: str,
    is_member: bool,
    accepted: bool,
) -> None:
    redis = FakeRedis()
    bot = FakeBot()
    bot.status = status
    bot.is_member = is_member
    settings = RequiredChannelsSection(
        positive_cache_ttl_seconds=300,
        negative_cache_ttl_seconds=30,
    )
    checker = TelegramMembershipChecker(
        cast(Bot, cast(Any, bot)),
        cast(Redis, cast(Any, redis)),
        settings,
    )
    channel = RequiredChannel(-1001, "Main", "https://t.me/main")

    missing = await checker.missing_channels(42, (channel,))
    assert missing == (() if accepted else (channel,))
    assert next(iter(redis.ttls.values())) == (300 if accepted else 30)
    assert await checker.missing_channels(42, (channel,)) == missing
    assert bot.calls == 1
