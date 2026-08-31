"""Telegram channel probe proving bot posting permission for logger destinations (T028)."""

from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)

from telegram_media_bot.domain.audit import DestinationProbeOutcome, DestinationProbeResult

logger = structlog.get_logger(__name__)

# Fixed sanitized probe text: no secrets, no user content, no channel references.
PROBE_MESSAGE_TEXT = "🧾 پیام آزمایشی کانال لاگر"

_ACCEPTED_MEMBER_STATUSES = frozenset({"administrator", "member", "creator"})


class TelegramAuditDestinationVerifier:
    """Probe one channel: existence, type, bot membership, and a real posting test."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._bot_id: int | None = None
        self._bot_id_lock = asyncio.Lock()

    async def probe(self, chat_id: int) -> DestinationProbeResult:
        try:
            chat = await self._bot.get_chat(chat_id)
        except TelegramForbiddenError:
            return DestinationProbeResult(DestinationProbeOutcome.FORBIDDEN, "Forbidden")
        except TelegramBadRequest:
            return DestinationProbeResult(DestinationProbeOutcome.NOT_CHANNEL, "BadRequest")
        except TelegramNetworkError:
            return DestinationProbeResult(DestinationProbeOutcome.UNREACHABLE, "NetworkError")
        except TelegramAPIError as exc:
            return DestinationProbeResult(DestinationProbeOutcome.AMBIGUOUS, type(exc).__name__)
        if chat.type != "channel":
            return DestinationProbeResult(DestinationProbeOutcome.NOT_CHANNEL, "NotChannel")
        bot_id = await self._bot_id_value()
        try:
            member = await self._bot.get_chat_member(chat_id, bot_id)
        except TelegramForbiddenError:
            return DestinationProbeResult(DestinationProbeOutcome.FORBIDDEN, "Forbidden")
        except TelegramBadRequest:
            return DestinationProbeResult(DestinationProbeOutcome.BOT_NOT_MEMBER, "BadRequest")
        except TelegramNetworkError:
            return DestinationProbeResult(DestinationProbeOutcome.UNREACHABLE, "NetworkError")
        except TelegramAPIError as exc:
            return DestinationProbeResult(DestinationProbeOutcome.AMBIGUOUS, type(exc).__name__)
        if member.status not in _ACCEPTED_MEMBER_STATUSES:
            return DestinationProbeResult(DestinationProbeOutcome.BOT_NOT_MEMBER, "BotNotMember")
        try:
            sent = await self._bot.send_message(chat_id, PROBE_MESSAGE_TEXT)
        except TelegramForbiddenError:
            return DestinationProbeResult(DestinationProbeOutcome.FORBIDDEN, "Forbidden")
        except TelegramBadRequest:
            return DestinationProbeResult(DestinationProbeOutcome.FORBIDDEN, "BadRequest")
        except TelegramNetworkError:
            return DestinationProbeResult(DestinationProbeOutcome.UNREACHABLE, "NetworkError")
        except TelegramAPIError as exc:
            return DestinationProbeResult(DestinationProbeOutcome.AMBIGUOUS, type(exc).__name__)
        # Best-effort cleanup of the probe message; a failed deletion never fails the probe.
        try:
            await self._bot.delete_message(chat_id, sent.message_id)
        except Exception:
            await logger.ainfo("logger_probe_test_message_not_deleted", chat_id=chat_id)
        return DestinationProbeResult(DestinationProbeOutcome.OK)

    async def _bot_id_value(self) -> int:
        if self._bot_id is None:
            async with self._bot_id_lock:
                if self._bot_id is None:
                    me = await self._bot.get_me()
                    self._bot_id = me.id
        return self._bot_id


__all__ = ["PROBE_MESSAGE_TEXT", "TelegramAuditDestinationVerifier"]
