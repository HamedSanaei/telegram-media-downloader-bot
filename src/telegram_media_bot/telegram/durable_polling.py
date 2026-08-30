"""Durable Telegram long-polling.

aiogram's ``Dispatcher.start_polling`` advances the polling offset as soon as a batch is fetched,
before handlers run, so a crash mid-handling can permanently lose an update that Telegram then
considers acknowledged. This loop instead:

1. fetches a batch from ``get_updates``,
2. serializes and durably records updates in Telegram order until the first unresolved gap,
3. advances the Telegram offset only through that durable prefix,
4. feeds each recorded update to the dispatcher in the same order and marks it completed (or
   bounded-retry) in the inbox.

A crash leaves unpersisted updates unacknowledged (Telegram re-delivers them) and replayable
persisted updates in the inbox for startup reconciliation. After the bounded serialization-failure
threshold, a non-replayable terminal tombstone records the update ID and failure while deliberately
abandoning handler processing so one impossible update cannot block all subsequent traffic forever.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from aiogram import Bot, Dispatcher
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


def serialize_update(update: Update) -> SerializedUpdate:
    """Persist one Telegram ``Update`` as safe, replayable JSON.

    aiogram's ``Update.model_dump_json()`` cannot serialize the ``Default`` sentinels aiogram can
    embed in nested/default-valued fields, so raw Pydantic serialization can raise
    ``PydanticSerializationError``. Instead, aiogram's own Telegram-object serializer is used (the
    same round-trip path aiogram uses internally). No outbound bot defaults are supplied: aiogram's
    fake bot resolves/excludes framework ``Default`` sentinels using empty default properties, so
    the durable snapshot contains only inbound Telegram semantics.
    """
    data = deserialize_telegram_object_to_python(update, include_api_method_name=False)
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

        # Phase 1: serialize and persist in increasing Telegram order. A transient serialization
        # failure is a hard barrier: no later update may become durable or handler-visible until
        # the gap succeeds or is durably terminal-quarantined.
        advance = 0 if offset is None else offset
        to_process: list[tuple[Update, InboundUpdate]] = []
        for update in sorted(updates, key=lambda item: item.update_id):
            if update.update_id is None:
                continue
            try:
                serialized = serialize_update(update)
            except Exception as exc:
                # Until the bound is reached, the update remains unacknowledged and Telegram must
                # redeliver it together with every later update from this batch.
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
                    # The ID and failure are durably recorded, but no replayable payload exists:
                    # handler processing is deliberately abandoned so this impossible update does
                    # not block all subsequent traffic forever.
                    quarantined = inbox.quarantine(
                        update.update_id,
                        _update_type(update),
                        type(exc).__name__,
                    )
                    serialize_failures.pop(update.update_id, None)
                    await logger.awarning(
                        "telegram_update_quarantined",
                        update_id=update.update_id,
                        update_type=_update_type(update),
                        error_category=type(exc).__name__,
                        newly_recorded=quarantined,
                    )
                    advance = max(advance, update.update_id + 1)
                    continue
                advance = update.update_id
                break
            serialize_failures.pop(update.update_id, None)
            record = inbox.record(
                serialized.update_id, serialized.update_type, serialized.payload_json
            )
            if record is not None:
                to_process.append((update, record))
            advance = max(advance, update.update_id + 1)

        # Phase 2: acknowledge exactly the durable prefix discovered above. When Phase 1 stopped at
        # update N, the next getUpdates request uses offset=N so Telegram redelivers N and later.
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


def _as_record_args(update: Update) -> tuple[int, str, str]:
    serialized = serialize_update(update)
    return serialized.update_id, serialized.update_type, serialized.payload_json
