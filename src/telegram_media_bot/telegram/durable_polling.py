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
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from aiogram.utils.serialization import deserialize_telegram_object_to_python

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.domain.inbound_updates import InboundUpdate

logger = structlog.get_logger(__name__)

_POLL_RETRY_BACKOFF_INITIAL = 1.0
_POLL_RETRY_BACKOFF_CAP = 30.0

#: How many consecutive poll cycles the same update may fail to serialize before it is
#: durably quarantined so the bot can keep polling without a permanent restart loop.
_SERIALIZE_FAILURE_MAX_CONSECUTIVE = 3


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


def serialize_update(
    update: Update, *, default: DefaultBotProperties | None = None
) -> SerializedUpdate:
    """Persist one Telegram ``Update`` as safe, replayable JSON.

    aiogram's ``Update.model_dump_json()`` cannot serialize the ``Default`` sentinels aiogram can
    embed in nested/default-valued fields, so raw Pydantic serialization can raise
    ``PydanticSerializationError``. Instead, aiogram's own Telegram-object serializer is used (the
    same round-trip path aiogram uses internally), which resolves framework ``Default`` values
    against the configured bot ``default`` properties into plain JSON-compatible data.
    """
    data = deserialize_telegram_object_to_python(
        update, default=default, include_api_method_name=False
    )
    return SerializedUpdate(
        update_id=update.update_id,
        update_type=_update_type(update),
        payload_json=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
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
    # update_id -> consecutive serialization failures in this process. Bounds how long a
    # permanently unserializable update can block the queue before it is durably quarantined.
    serialize_failures: dict[int, int] = {}
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

        # Phase 1: serialize and durably persist every delivered update before acknowledging any.
        batch_ids = [u.update_id for u in updates if u.update_id is not None]
        advance = max(0 if offset is None else offset, ((max(batch_ids) + 1) if batch_ids else 0))
        blockers: list[int] = []
        to_process: list[tuple[Update, InboundUpdate]] = []
        for update in updates:
            if update.update_id is None:
                continue
            try:
                serialized = serialize_update(update, default=bot.default)
            except Exception as exc:
                # A failing update is NOT acknowledged here: it stays pending for Telegram to
                # redeliver, so it can never be silently lost. Track it so a permanently broken
                # update cannot restart-loop the bot forever (see quarantine below).
                blockers.append(update.update_id)
                failures = serialize_failures.get(update.update_id, 0) + 1
                serialize_failures[update.update_id] = failures
                await logger.awarning(
                    "telegram_update_serialization_failed",
                    update_id=update.update_id,
                    update_type=_update_type(update),
                    error_category=type(exc).__name__,
                    consecutive_failures=failures,
                    max_consecutive_failures=_SERIALIZE_FAILURE_MAX_CONSECUTIVE,
                    quarantined=False,
                )
                if failures >= _SERIALIZE_FAILURE_MAX_CONSECUTIVE:
                    # Durable terminal quarantine: the update_id is recorded forever (never
                    # replayed, never silently dropped) so the Telegram offset may advance and the
                    # bot keeps serving fresh traffic.
                    quarantined = inbox.quarantine(
                        update.update_id,
                        _update_type(update),
                        type(exc).__name__,
                    )
                    blockers.remove(update.update_id)
                    serialize_failures.pop(update.update_id, None)
                    await logger.awarning(
                        "telegram_update_quarantined",
                        update_id=update.update_id,
                        update_type=_update_type(update),
                        error_category=type(exc).__name__,
                        newly_recorded=quarantined,
                    )
                continue
            record = inbox.record(
                serialized.update_id, serialized.update_type, serialized.payload_json
            )
            if record is not None:
                to_process.append((update, record))

        # Phase 2: advance only over updates that are durably accounted for. If an update could not
        # be serialized this cycle (and has not yet exhausted its quarantine bound), do not pass it.
        if blockers:
            advance = min(advance, min(blockers))
        offset = advance

        # Phase 3: process sequentially so updates from the same chat stay ordered.
        for update, record in to_process:
            prepared = inbox.start_processing(record)
            try:
                await dispatcher.feed_update(bot, update, durable_update_id=record.update_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await logger.awarning(
                    "telegram_update_handler_failed",
                    update_id=record.update_id,
                    processing_attempt=prepared.processing_attempts,
                    error_category=type(exc).__name__,
                )
                inbox.handler_failed(prepared, error_category=type(exc).__name__)
            else:
                inbox.mark_completed(prepared)


def _as_record_args(
    update: Update, *, default: DefaultBotProperties | None = None
) -> tuple[int, str, str]:
    serialized = serialize_update(update, default=default)
    return serialized.update_id, serialized.update_type, serialized.payload_json
