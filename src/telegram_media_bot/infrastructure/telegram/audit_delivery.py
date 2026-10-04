"""Telegram-native Operator Logger transport (T030)."""

from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.utils.formatting import sizeof

from telegram_media_bot.application.services.audit_sanitizer import safe_failure_class
from telegram_media_bot.domain.audit import (
    AuditDeliveryOutcome,
    AuditDeliveryResult,
    AuditEvent,
    AuditEventType,
    LoggerOutboxItem,
)

_COPY_MESSAGES_LIMIT = 100
_CAPTION_LIMIT = 1024


class TelegramAuditDelivery:
    """Mirror successful output captions and deliver safe operational reports."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def deliver(self, item: LoggerOutboxItem) -> AuditDeliveryResult:
        side_effect_completed = False
        try:
            if item.event.event_type is AuditEventType.DOWNLOAD_OUTPUT_DELIVERED:
                event = item.event
                source = event.source
                if source is None:
                    return AuditDeliveryResult(
                        AuditDeliveryOutcome.FAILED_TERMINAL, "MissingSourceReference"
                    )
                if type(event.telegram_user_id) is not int or event.telegram_user_id <= 0:
                    return AuditDeliveryResult(
                        AuditDeliveryOutcome.FAILED_TERMINAL, "MissingOutputUserIdentity"
                    )
                output = event.output
                if output is None or len(output.captions) != len(source.message_ids):
                    return AuditDeliveryResult(
                        AuditDeliveryOutcome.FAILED_TERMINAL, "MissingOutputCaptionContext"
                    )
                offset = 0
                while offset < len(source.message_ids):
                    end = offset + 1
                    while (
                        end < len(source.message_ids)
                        and end - offset < _COPY_MESSAGES_LIMIT
                        and source.message_ids[end - 1] < source.message_ids[end]
                    ):
                        end += 1
                    if end - offset == 1:
                        await self._bot.copy_message(
                            chat_id=item.destination_chat_id,
                            from_chat_id=source.chat_id,
                            message_id=source.message_ids[offset],
                            caption=_output_caption(event, output.captions[offset]),
                            parse_mode="HTML",
                            show_caption_above_media=False,
                        )
                        side_effect_completed = True
                    else:
                        copied = await self._bot.copy_messages(
                            chat_id=item.destination_chat_id,
                            from_chat_id=source.chat_id,
                            message_ids=list(source.message_ids[offset:end]),
                            remove_caption=True,
                        )
                        side_effect_completed = True
                        if len(copied) != end - offset:
                            return AuditDeliveryResult(
                                AuditDeliveryOutcome.UNCERTAIN, "IncompleteOutputCopy"
                            )
                        for ordinal, message in enumerate(copied, start=offset):
                            await self._bot.edit_message_caption(
                                chat_id=item.destination_chat_id,
                                message_id=message.message_id,
                                caption=_output_caption(event, output.captions[ordinal]),
                                parse_mode="HTML",
                                show_caption_above_media=False,
                            )
                    offset = end
            elif item.event.event_type is AuditEventType.USER_SUBMISSION_RECEIVED:
                return AuditDeliveryResult(
                    AuditDeliveryOutcome.FAILED_TERMINAL, "SubmissionMirrorRetired"
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


def _output_caption(event: AuditEvent, caption: str) -> str:
    output = event.output
    assert output is not None
    identity = f"آیدی عددی: {event.telegram_user_id}"
    footer = f"آیدی عددی: <code>{event.telegram_user_id}</code>"
    if output.telegram_username is not None:
        username = f"یوزرنیم: @{output.telegram_username}"
        identity += f"\n{username}"
        footer += f"\n{escape(username)}"
    link_label = output.source_url
    visible_footer = f"{identity}\n🔗 لینک اصلی: {link_label}"
    if sizeof(visible_footer) > _CAPTION_LIMIT:
        link_label = "مشاهده پست"
        visible_footer = f"{identity}\n🔗 لینک اصلی: {link_label}"
    footer += f'\n🔗 لینک اصلی: <a href="{escape(output.source_url)}">{escape(link_label)}</a>'
    if not caption:
        return footer
    description_budget = _CAPTION_LIMIT - sizeof(visible_footer) - sizeof("\n\n")
    if description_budget <= 0:
        return footer
    if sizeof(caption) > description_budget:
        prefix_budget = description_budget - sizeof("…")
        prefix_size = 0
        end = 0
        for character in caption:
            character_size = sizeof(character)
            if prefix_size + character_size > prefix_budget:
                break
            prefix_size += character_size
            end += 1
        if end == 0:
            return footer
        caption = caption[:end] + "…"
    return f"{escape(caption)}\n\n{footer}"


__all__ = ["TelegramAuditDelivery"]
