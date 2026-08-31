"""T028: Telegram destination verifier maps API failures to typed outcomes (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)

from telegram_media_bot.domain.audit import DestinationProbeOutcome
from telegram_media_bot.infrastructure.telegram.audit_destination_verifier import (
    TelegramAuditDestinationVerifier,
)

CHANNEL = -1001234567890
BOT_ID = 111


class FakeBot:
    def __init__(self) -> None:
        self.me = SimpleNamespace(id=BOT_ID)
        self.chat = SimpleNamespace(type="channel")
        self.member = SimpleNamespace(status="administrator")
        self.sent = SimpleNamespace(message_id=7)
        self.deleted: list[int] = []
        self.errors: dict[str, Exception] = {}

    async def get_me(self) -> SimpleNamespace:
        return self.me

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        del chat_id
        if "get_chat" in self.errors:
            raise self.errors["get_chat"]
        return self.chat

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        del chat_id, user_id
        if "get_chat_member" in self.errors:
            raise self.errors["get_chat_member"]
        return self.member

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        del chat_id, text
        if "send_message" in self.errors:
            raise self.errors["send_message"]
        return self.sent

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        del chat_id
        self.deleted.append(message_id)


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=cast(Any, SimpleNamespace()), message="Bad Request: test")


async def _probe(bot: FakeBot) -> DestinationProbeOutcome:
    """Run a probe against the fake bot, returning only the typed outcome."""
    return (await TelegramAuditDestinationVerifier(cast(Bot, bot)).probe(CHANNEL)).outcome


async def test_probe_ok_sends_and_cleans_test_message() -> None:
    bot = FakeBot()
    assert await _probe(bot) is DestinationProbeOutcome.OK
    assert bot.deleted == [7]


async def test_probe_get_chat_forbidden() -> None:
    bot = FakeBot()
    bot.errors["get_chat"] = TelegramForbiddenError(
        method=cast(Any, SimpleNamespace()), message="Forbidden: bot was blocked"
    )
    assert await _probe(bot) is DestinationProbeOutcome.FORBIDDEN


async def test_probe_get_chat_bad_request_is_not_channel() -> None:
    bot = FakeBot()
    bot.errors["get_chat"] = _bad_request()
    assert await _probe(bot) is DestinationProbeOutcome.NOT_CHANNEL


async def test_probe_network_error_is_unreachable() -> None:
    bot = FakeBot()
    bot.errors["get_chat"] = TelegramNetworkError(
        method=cast(Any, SimpleNamespace()), message="NetworkError"
    )
    assert await _probe(bot) is DestinationProbeOutcome.UNREACHABLE


async def test_probe_non_channel_chat_rejected() -> None:
    bot = FakeBot()
    bot.chat = SimpleNamespace(type="group")
    assert await _probe(bot) is DestinationProbeOutcome.NOT_CHANNEL


async def test_probe_missing_bot_membership() -> None:
    bot = FakeBot()
    bot.member = SimpleNamespace(status="left")
    assert await _probe(bot) is DestinationProbeOutcome.BOT_NOT_MEMBER


async def test_probe_send_forbidden() -> None:
    bot = FakeBot()
    bot.errors["send_message"] = TelegramForbiddenError(
        method=cast(Any, SimpleNamespace()), message="Forbidden: bot can't send"
    )
    assert await _probe(bot) is DestinationProbeOutcome.FORBIDDEN


async def test_probe_unknown_api_error_is_ambiguous() -> None:
    bot = FakeBot()
    bot.errors["send_message"] = TelegramAPIError(
        method=cast(Any, SimpleNamespace()), message="Unexpected server answer"
    )
    assert await _probe(bot) is DestinationProbeOutcome.AMBIGUOUS


@pytest.mark.parametrize(
    ("error_key", "expected"),
    [
        ("get_chat", DestinationProbeOutcome.FORBIDDEN),
        ("get_chat_member", DestinationProbeOutcome.FORBIDDEN),
        ("send_message", DestinationProbeOutcome.FORBIDDEN),
    ],
)
async def test_probe_forbidden_mapping_per_stage(
    error_key: str, expected: DestinationProbeOutcome
) -> None:
    bot = FakeBot()
    bot.errors[error_key] = TelegramForbiddenError(
        method=cast(Any, SimpleNamespace()), message="Forbidden: test"
    )
    assert await _probe(bot) is expected
