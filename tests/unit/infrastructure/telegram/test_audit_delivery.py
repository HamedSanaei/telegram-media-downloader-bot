from datetime import UTC, datetime
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import CopyMessage

from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    LoggerOutboxItem,
    LoggerOutboxState,
    TelegramSourceReference,
)
from telegram_media_bot.infrastructure.telegram.audit_delivery import TelegramAuditDelivery


class FakeBot:
    def __init__(
        self, failure: Exception | None = None, metadata_failure: Exception | None = None
    ) -> None:
        self.failure = failure
        self.metadata_failure = metadata_failure
        self.copies: list[dict[str, object]] = []
        self.groups: list[dict[str, object]] = []
        self.messages: list[tuple[int, str]] = []

    async def copy_message(self, **kwargs: object) -> None:
        if self.failure is not None:
            raise self.failure
        self.copies.append(kwargs)

    async def copy_messages(self, **kwargs: object) -> None:
        if self.failure is not None:
            raise self.failure
        self.groups.append(kwargs)

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.metadata_failure is not None:
            raise self.metadata_failure
        self.messages.append((chat_id, text))


def _item(
    message_ids: tuple[int, ...] = (10,),
    *,
    event_type: AuditEventType = AuditEventType.USER_SUBMISSION_RECEIVED,
    source_chat_id: int = 4242,
) -> LoggerOutboxItem:
    event = AuditEvent(
        event_id="event-1",
        event_type=event_type,
        category=AuditCategory.USER_SUBMISSION,
        severity=AuditSeverity.INFO,
        occurred_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        correlation_id="submission:update:77",
        message="Accepted Telegram download submission",
        telegram_user_id=4242,
        update_id=77,
        job_id="inspection-1",
        content_type="photo",
        provider="example.com",
        source=TelegramSourceReference(source_chat_id, message_ids),
    )
    return LoggerOutboxItem(
        event=event,
        destination_chat_id=-1001234567890,
        state=LoggerOutboxState.LEASED,
        attempt_count=1,
        lease_token="lease",
    )


async def test_single_submission_uses_native_copy_and_safe_numeric_metadata() -> None:
    bot = FakeBot()

    result = await TelegramAuditDelivery(cast(Bot, cast(Any, bot))).deliver(_item())

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert bot.copies == [{"chat_id": -1001234567890, "from_chat_id": 4242, "message_id": 10}]
    assert bot.groups == []
    assert "user_id: 4242" in bot.messages[0][1]
    assert "username" not in bot.messages[0][1]


async def test_album_uses_copy_messages_once_and_preserves_source_order() -> None:
    bot = FakeBot()

    result = await TelegramAuditDelivery(cast(Bot, cast(Any, bot))).deliver(_item((10, 11, 12)))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert bot.copies == []
    assert bot.groups == [
        {
            "chat_id": -1001234567890,
            "from_chat_id": 4242,
            "message_ids": [10, 11, 12],
        }
    ]


async def test_download_output_copies_recipient_messages_not_submission_source() -> None:
    bot = FakeBot()

    result = await TelegramAuditDelivery(cast(Bot, cast(Any, bot))).deliver(
        _item(
            (901, 902),
            event_type=AuditEventType.DOWNLOAD_OUTPUT_DELIVERED,
            source_chat_id=-1007770001112,
        )
    )

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert bot.groups == [
        {
            "chat_id": -1001234567890,
            "from_chat_id": -1007770001112,
            "message_ids": [901, 902],
        }
    ]
    assert bot.messages[0][1].startswith("📦 Delivered download output")


async def test_large_output_copy_is_bounded_and_preserves_global_order() -> None:
    bot = FakeBot()
    message_ids = tuple(range(1, 207))

    result = await TelegramAuditDelivery(cast(Bot, cast(Any, bot))).deliver(
        _item(message_ids, event_type=AuditEventType.DOWNLOAD_OUTPUT_DELIVERED)
    )

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    copied_groups = [cast(list[int], group["message_ids"]) for group in bot.groups]
    assert [len(group) for group in copied_groups] == [100, 100, 6]
    assert [item for group in copied_groups for item in group] == list(message_ids)


async def test_forbidden_is_terminal_and_network_ambiguity_is_uncertain() -> None:
    method = CopyMessage(chat_id=-1001234567890, from_chat_id=4242, message_id=10)
    forbidden = FakeBot(TelegramForbiddenError(method=method, message="forbidden"))
    uncertain = FakeBot(TelegramNetworkError(method=method, message="connection lost"))

    forbidden_result = await TelegramAuditDelivery(cast(Bot, cast(Any, forbidden))).deliver(_item())
    uncertain_result = await TelegramAuditDelivery(cast(Bot, cast(Any, uncertain))).deliver(_item())

    assert forbidden_result.outcome is AuditDeliveryOutcome.FAILED_TERMINAL
    assert uncertain_result.outcome is AuditDeliveryOutcome.UNCERTAIN


async def test_metadata_failure_after_copy_is_uncertain_and_never_retryable() -> None:
    method = CopyMessage(chat_id=-1001234567890, from_chat_id=4242, message_id=10)
    bot = FakeBot(metadata_failure=TelegramForbiddenError(method=method, message="forbidden"))

    result = await TelegramAuditDelivery(cast(Bot, cast(Any, bot))).deliver(_item())

    assert len(bot.copies) == 1
    assert result.outcome is AuditDeliveryOutcome.UNCERTAIN
