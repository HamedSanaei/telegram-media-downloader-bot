"""Telegram-native Operator Logger transport (T030)."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from telegram_media_bot.application.services.audit_sanitizer import safe_failure_class
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditDeliveryResult,
    AuditEvent,
    LoggerOutboxItem,
)


class TelegramAuditDelivery:
    """Copy original submissions and send safe metadata without re-uploading user media."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def deliver(self, item: LoggerOutboxItem) -> AuditDeliveryResult:
        side_effect_completed = False
        try:
            if item.event.category is AuditCategory.USER_SUBMISSION:
                source = item.event.source
                if source is None:
                    return AuditDeliveryResult(
                        AuditDeliveryOutcome.FAILED_TERMINAL, "MissingSourceReference"
                    )
                if len(source.message_ids) == 1:
                    await self._bot.copy_message(
                        chat_id=item.destination_chat_id,
                        from_chat_id=source.chat_id,
                        message_id=source.message_ids[0],
                    )
                else:
                    await self._bot.copy_messages(
                        chat_id=item.destination_chat_id,
                        from_chat_id=source.chat_id,
                        message_ids=list(source.message_ids),
                    )
                side_effect_completed = True
                await self._bot.send_message(
                    item.destination_chat_id,
                    _metadata_text(item.event),
                )
            else:
                await self._bot.send_message(item.destination_chat_id, item.event.message)
        except TelegramRetryAfter as exc:
            outcome = (
                AuditDeliveryOutcome.UNCERTAIN
                if side_effect_completed
                else AuditDeliveryOutcome.RETRYABLE
            )
            return AuditDeliveryResult(outcome, safe_failure_class(exc))
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if side_effect_completed:
                return AuditDeliveryResult(AuditDeliveryOutcome.UNCERTAIN, safe_failure_class(exc))
            return AuditDeliveryResult(
                AuditDeliveryOutcome.FAILED_TERMINAL, safe_failure_class(exc)
            )
        except (TelegramNetworkError, TelegramServerError, TimeoutError) as exc:
            return AuditDeliveryResult(AuditDeliveryOutcome.UNCERTAIN, safe_failure_class(exc))
        except TelegramAPIError as exc:
            return AuditDeliveryResult(AuditDeliveryOutcome.UNCERTAIN, safe_failure_class(exc))
        return AuditDeliveryResult(AuditDeliveryOutcome.SUCCEEDED)


def _metadata_text(event: AuditEvent) -> str:
    fields = [
        "🧾 Accepted download submission",
        f"user_id: {event.telegram_user_id}" if event.telegram_user_id is not None else None,
        f"update_id: {event.update_id}" if event.update_id is not None else None,
        f"job_id: {event.job_id}" if event.job_id is not None else None,
        f"content_type: {event.content_type}" if event.content_type is not None else None,
        f"provider: {event.provider}" if event.provider is not None else None,
        f"correlation_id: {event.correlation_id}",
        f"occurred_at: {event.occurred_at.isoformat()}",
    ]
    return "\n".join(field for field in fields if field is not None)


__all__ = ["TelegramAuditDelivery"]
