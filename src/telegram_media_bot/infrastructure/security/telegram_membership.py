from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from aiogram import Bot
from redis.asyncio import Redis
from redis.exceptions import RedisError

from telegram_media_bot.application.ports.membership import MembershipChecker
from telegram_media_bot.bootstrap.config import RequiredChannelsSection
from telegram_media_bot.domain.errors import PolicyBackendError
from telegram_media_bot.domain.models import RequiredChannel


class TelegramMembershipChecker(MembershipChecker):
    def __init__(
        self,
        bot: Bot,
        redis: Redis,
        settings: RequiredChannelsSection,
        *,
        prefix: str = "media-bot:membership",
    ) -> None:
        self._bot = bot
        self._redis = redis
        self._settings = settings
        self._prefix = prefix

    @classmethod
    def create(
        cls,
        bot: Bot,
        redis_url: str,
        settings: RequiredChannelsSection,
    ) -> TelegramMembershipChecker:
        return cls(bot, Redis.from_url(redis_url, decode_responses=False), settings)

    async def missing_channels(
        self,
        user_id: int,
        channels: tuple[RequiredChannel, ...],
        *,
        force_refresh: bool = False,
    ) -> tuple[RequiredChannel, ...]:
        missing: list[RequiredChannel] = []
        for channel in channels:
            if not await self._is_member(user_id, channel.chat_id, force_refresh=force_refresh):
                missing.append(channel)
        return tuple(missing)

    async def close(self) -> None:
        await self._redis.aclose()

    async def _is_member(self, user_id: int, chat_id: int, *, force_refresh: bool) -> bool:
        key = f"{self._prefix}:{chat_id}:{user_id}"
        try:
            if force_refresh:
                await cast(Awaitable[Any], self._redis.delete(key))
            else:
                cached = await cast(Awaitable[Any], self._redis.get(key))
                if cached in {b"1", "1"}:
                    return True
                if cached in {b"0", "0"}:
                    return False
            member = await self._bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            status = getattr(member.status, "value", str(member.status))
            accepted = status in {"creator", "administrator", "member"} or (
                status == "restricted" and bool(getattr(member, "is_member", False))
            )
            ttl = (
                self._settings.positive_cache_ttl_seconds
                if accepted
                else self._settings.negative_cache_ttl_seconds
            )
            await cast(Awaitable[Any], self._redis.setex(key, ttl, "1" if accepted else "0"))
            return accepted
        except RedisError as exc:
            raise PolicyBackendError("Membership cache is unavailable") from exc
        except PolicyBackendError:
            raise
        except Exception as exc:
            raise PolicyBackendError("Telegram membership check failed") from exc
