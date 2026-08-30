"""Durable Telegram long-polling.

aiogram's ``Dispatcher.start_polling`` advances the polling offset as soon as a batch is fetched,
before handlers run, so a crash mid-handling can permanently lose an update that Telegram then
considers acknowledged. This loop instead:

1. fetches a batch from ``get_updates``,
2. *durably records every update in the bind-oriented inbox*,
3. only then advances the Telegram offset (via the next ``get_updates`` call),
4. feeds each new update to the dispatcher and marks it completed (or bounded-retry) in the inbox.

A crash leaves unpersisted updates unacknowledged (Telegram re-delivers them) and persisted updates
in the inbox for startup reconciliation, so unanswered requests are never lost.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.domain.inbound_updates import InboundUpdate

logger = structlog.get_logger(__name__)

_POLL_RETRY_BACKOFF_INITIAL = 1.0
_POLL_RETRY_BACKOFF_CAP = 30.0


@dataclass(frozen=True, slots=True)
class SerializedUpdate:
    update_id: int
    update_type: str
    payload_json: str


def _update_type(update: Update) -> str:
    for candidate in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
        "inline_query",
        "chosen_inline_result",
        "callback_query",
        "business_callback_query",
        "shipping_query",
        "pre_checkout_query",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "message_reaction",
        "chat_boost",
        "removed_chat_boost",
    ):
        if getattr(update, candidate, None) is not None:
            return candidate
    return "message"


def serialize_update(update: Update) -> SerializedUpdate:
    return SerializedUpdate(
        update_id=update.update_id,
        update_type=_update_type(update),
        payload_json=update.model_dump_json(exclude_none=True),
    )


def _replay_update(record: InboundUpdate) -> Update:
    return Update.model_validate(json.loads(record.payload_json))


async def replay_pending_updates(
    bot: Bot,
    dispatcher: Dispatcher,
    inbox: DurableUpdateInbox,
    *,
    limit: int = 500,
) -> int:
    """Recover abandoned RECEIVED/PROCESSING updates before live polling.

    Returns the number of pending updates fed to handlers this run.
    """
    replayed = 0
    for record in inbox.pending(limit):
        update = _replay_update(record)
        inbox.recovered(record)
        prepared = inbox.start_processing(record)
        try:
            await dispatcher.feed_update(bot, update, durable_update_id=record.update_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.awarning(
                "telegram_update_recovery_failed",
                update_id=record.update_id,
                error_category=type(exc).__name__,
            )
            inbox.handler_failed(prepared, error_category=type(exc).__name__)
        else:
            inbox.mark_completed(prepared)
        replayed += 1
    return replayed


async def durable_poll(
    bot: Bot,
    dispatcher: Dispatcher,
    inbox: DurableUpdateInbox,
    *,
    polling_timeout: int,
    stopped: Callable[[], bool],
) -> None:
    """Run the durable inbound-update polling loop until cancelled or ``stopped()``."""
    allowed_updates = list(dispatcher.resolve_used_update_types())
    offset: int | None = None
    backoff = _POLL_RETRY_BACKOFF_INITIAL
    while not stopped():
        updates: list[Update] = []
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=polling_timeout,
                allowed_updates=allowed_updates,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.awarning(
                "telegram_poll_retry",
                error_category=type(exc).__name__,
                backoff_seconds=round(backoff, 2),
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _POLL_RETRY_BACKOFF_CAP)
            continue
        backoff = _POLL_RETRY_BACKOFF_INITIAL
        if not updates:
            continue

        # Phase 1: persist every delivered update before acknowledging any of them.
        next_offset = 0 if offset is None else offset
        to_process: list[tuple[Update, InboundUpdate]] = []
        for update in updates:
            if update.update_id is None:
                continue
            if update.update_id + 1 > next_offset:
                next_offset = update.update_id + 1
            record = inbox.record(*_as_record_args(update))
            if record is not None:
                to_process.append((update, record))

        # Phase 2: every update in this batch is now durable; safe to advance the Telegram offset.
        offset = next_offset

        # Phase 3: process sequentially so updates from the same chat stay ordered.
        for update, record in to_process:
            prepared = inbox.start_processing(record)
            try:
                await dispatcher.feed_update(bot, update, durable_update_id=record.update_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                inbox.handler_failed(prepared, error_category=type(exc).__name__)
            else:
                inbox.mark_completed(prepared)


def _as_record_args(update: Update) -> tuple[int, str, str]:
    serialized = serialize_update(update)
    return serialized.update_id, serialized.update_type, serialized.payload_json
