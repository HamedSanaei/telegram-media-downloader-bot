"""Regression coverage for durable Telegram update serialization.

The production bug: ``Update.model_dump_json()`` cannot serialize the aiogram ``Default``
sentinels that can appear in nested/default-valued fields (e.g. link-preview options), raising
``pydantic_core.PydanticSerializationError``. Because the raw dump runs before the update is
durably recorded, every redelivery of the same pending update crashed the bot into a permanent
restart loop.

These tests use the real installed aiogram types (including real ``Default`` sentinels) and the
real poll loop, so a similar regression cannot hide behind fake-update test doubles.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiogram import Bot
from aiogram.client.default import Default, DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    Chat,
    LinkPreviewOptions,
    Message,
    Update,
    User,
)
from pydantic_core import PydanticSerializationError

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.domain.inbound_updates import InboundUpdate, UpdateProcessingState
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
from telegram_media_bot.telegram import durable_polling
from telegram_media_bot.telegram.durable_polling import durable_poll, serialize_update


def _bot(*, link_preview_is_disabled: bool = True) -> Bot:
    return Bot(
        token="123:ABC",
        default=DefaultBotProperties(link_preview_is_disabled=link_preview_is_disabled),
    )


def _chat_user() -> tuple[Chat, User]:
    return (
        Chat(id=1, type="private"),
        User(id=99, is_bot=False, first_name="کاربر"),
    )


def _message_update(update_id: int, *, text: str = "https://example.com/media") -> Update:
    chat, user = _chat_user()
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2023, 11, 14, tzinfo=UTC),
            chat=chat,
            from_user=user,
            text=text,
        ),
    )


def _link_preview_update_with_default(update_id: int) -> Update:
    """An update whose nested ``LinkPreviewOptions`` carries a real aiogram ``Default`` sentinel."""
    chat, user = _chat_user()
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2023, 11, 14, tzinfo=UTC),
            chat=chat,
            from_user=user,
            text="https://example.com/p",
            link_preview_options=LinkPreviewOptions(
                is_disabled=Default("link_preview_is_disabled")
            ),
        ),
    )


def _callback_update(update_id: int) -> Update:
    chat, user = _chat_user()
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb{update_id}",
            from_user=user,
            chat_instance="instance",
            data="/resolve",
            message=Message(
                message_id=update_id,
                date=datetime(2023, 11, 14, tzinfo=UTC),
                chat=chat,
                from_user=user,
                text="original",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Serialization / round-trip (real aiogram Default sentinel, real installed version)
# ---------------------------------------------------------------------------


def test_raw_model_dump_json_fails_on_default_sentinel() -> None:
    """Prove the OLD behaviour reproduced the production PydanticSerializationError."""
    update = _link_preview_update_with_default(555)
    with pytest.raises(PydanticSerializationError):
        update.model_dump_json(exclude_none=True)


def test_serialize_update_handles_default_without_injecting_outbound_bot_defaults() -> None:
    bot = _bot(link_preview_is_disabled=True)
    update = _link_preview_update_with_default(555)
    serialized = serialize_update(update)

    # JSON-compatible, no Python / framework object representation leaked into the payload.
    payload = json.loads(serialized.payload_json)
    assert payload
    assert "Default" not in serialized.payload_json
    assert bot.default.link_preview_is_disabled is True
    assert "is_disabled" not in payload["message"]["link_preview_options"]

    # Round-trip: handler-visible inbound semantics retain aiogram's unresolved sentinel rather
    # than materializing the real Bot's outbound default into the Telegram snapshot.
    round_trip = Update.model_validate(payload)
    assert round_trip.update_id == 555
    assert round_trip.message is not None
    assert round_trip.message.text == "https://example.com/p"
    assert round_trip.message.link_preview_options is not None
    assert round_trip.message.link_preview_options.is_disabled == Default(
        "link_preview_is_disabled"
    )


def test_unicode_farsi_survives_serialization() -> None:
    update = _message_update(7, text="سلام دنیا دانلود")
    serialized = serialize_update(update)
    round_trip = Update.model_validate(json.loads(serialized.payload_json))
    assert round_trip.message is not None
    assert round_trip.message.text == "سلام دنیا دانلود"


def test_callback_query_round_trips() -> None:
    update = _callback_update(9)
    serialized = serialize_update(update)
    assert serialized.update_type == "callback_query"
    round_trip = Update.model_validate(json.loads(serialized.payload_json))
    assert round_trip.callback_query is not None
    assert round_trip.callback_query.id == "cb9"
    assert round_trip.callback_query.data == "/resolve"
    assert round_trip.callback_query.message is not None
    assert isinstance(round_trip.callback_query.message, Message)
    assert round_trip.callback_query.message.text == "original"


def test_replay_update_pipeline_preserves_inbound_default_semantics() -> None:
    """End-to-end replay path: serialized payload re-parses into an equivalent handler-visible update."""
    update = _link_preview_update_with_default(555)
    serialized = serialize_update(update)
    replayed = durable_polling._replay_update(
        _plain_update(serialized.update_id, serialized.payload_json)
    )
    assert replayed.update_id == 555
    assert replayed.message is not None
    assert replayed.message.link_preview_options is not None
    assert replayed.message.link_preview_options.is_disabled == Default("link_preview_is_disabled")


# ---------------------------------------------------------------------------
# Poll-loop ordering and fail-safe semantics (real Bot, real aiogram updates)
# ---------------------------------------------------------------------------


@dataclass
class _Controller:
    done: bool = False


class _StubDispatcher:
    def __init__(self) -> None:
        self.processed: list[int] = []

    def resolve_used_update_types(self) -> list[str]:
        return []

    async def feed_update(self, bot: Any, update: Update, **_kwargs: object) -> bool:
        self.processed.append(update.update_id)
        return True


class _FakeTelegram:
    """Models Telegram's offset semantics: returns all updates with id >= requested offset."""

    def __init__(self, updates: list[Update], controller: _Controller) -> None:
        self.updates = updates
        self.controller = controller
        self.offset_calls: list[int | None] = []

    async def get_updates(self, offset: int | None = None, **_kwargs: object) -> list[Update]:
        self.offset_calls.append(offset)
        result = [u for u in self.updates if u.update_id >= (offset or 0)]
        if not result:
            self.controller.done = True
        return result


def _run_poll(
    bot: Bot, dispatcher: _StubDispatcher, inbox: DurableUpdateInbox, fake: _FakeTelegram
) -> None:
    bot.get_updates = fake.get_updates  # type: ignore[assignment]

    async def run() -> None:
        await durable_poll(
            bot,
            dispatcher,  # type: ignore[arg-type]
            inbox,
            polling_timeout=10,
            stopped=lambda: fake.controller.done,
        )

    asyncio.run(run())


def _inbox(tmp_path: Path) -> DurableUpdateInbox:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    return DurableUpdateInbox(repo)


def test_poll_persists_before_advancing_offset(tmp_path: Path) -> None:
    bot = _bot()
    inbox = _inbox(tmp_path)
    dispatcher = _StubDispatcher()
    controller = _Controller()
    fake = _FakeTelegram([_message_update(1), _message_update(2)], controller)
    bot.get_updates = fake.get_updates  # type: ignore[assignment]

    _run_poll(bot, dispatcher, inbox, fake)

    # All delivered updates durably recorded and completed.
    assert inbox.pending_count() == 0
    assert dispatcher.processed == [1, 2]
    # The offset advanced past the whole first batch (to 3) only once both were persisted.
    assert fake.offset_calls[0] is None
    assert fake.offset_calls[1] == 3


def test_completed_update_is_never_reprocessed(tmp_path: Path) -> None:
    bot = _bot()
    inbox = _inbox(tmp_path)
    controller = _Controller()
    fake = _FakeTelegram([_message_update(1)], controller)
    dispatcher = _StubDispatcher()
    _run_poll(bot, dispatcher, inbox, fake)
    assert dispatcher.processed == [1]
    assert inbox.pending_count() == 0
    # A redelivery of the already-completed update is a no-op at the inbox layer.
    assert inbox.record(1, "message", '{"update_id": 1}') is None


def test_serialization_failure_does_not_advance_offset_then_recovers(tmp_path: Path) -> None:
    bot = _bot()
    inbox = _inbox(tmp_path)
    inspection_repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    controller = _Controller()
    dispatcher = _StubDispatcher()
    real_serialize = durable_polling.serialize_update
    failures = {"remaining": 1}
    after_first_cycle: dict[str, object] = {}

    class SnapshotTelegram(_FakeTelegram):
        async def get_updates(self, offset: int | None = None, **kwargs: object) -> list[Update]:
            if self.offset_calls and not after_first_cycle:
                rows = {update_id: inspection_repo.get(update_id) for update_id in (1, 2, 3)}
                after_first_cycle.update(
                    offset=offset,
                    processed=list(dispatcher.processed),
                    states={
                        update_id: None if row is None else row.state.value
                        for update_id, row in rows.items()
                    },
                )
            return await super().get_updates(offset=offset, **kwargs)

    fake = SnapshotTelegram(
        [_message_update(1), _message_update(2), _message_update(3)], controller
    )

    def flaky_serialize(update: Update) -> Any:
        if update.update_id == 2 and failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise PydanticSerializationError("unable to serialize")
        return real_serialize(update)

    with mock.patch.object(durable_polling, "serialize_update", side_effect=flaky_serialize):
        _run_poll(bot, dispatcher, inbox, fake)

    # The first failure is a hard barrier: 1 may complete, but neither 2 nor 3 is durable or
    # handler-visible before Telegram is polled again from the unresolved update ID.
    assert after_first_cycle == {
        "offset": 2,
        "processed": [1],
        "states": {1: "completed", 2: None, 3: None},
    }

    # Retry preserves exact dispatcher order and advances only after 2 and 3 become durable.
    assert dispatcher.processed == [1, 2, 3]
    assert inbox.pending_count() == 0
    assert fake.offset_calls == [None, 2, 4]


def test_serialization_gap_cannot_leapfrog_across_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jobs.sqlite3"
    first_inbox = _inbox(tmp_path)
    first_controller = _Controller()
    first_dispatcher = _StubDispatcher()
    real_serialize = durable_polling.serialize_update
    failures = {"remaining": 1}

    class OneBatchTelegram(_FakeTelegram):
        async def get_updates(self, offset: int | None = None, **kwargs: object) -> list[Update]:
            result = await super().get_updates(offset=offset, **kwargs)
            self.controller.done = True
            return result

    first_fake = OneBatchTelegram(
        [_message_update(1), _message_update(2), _message_update(3)], first_controller
    )

    def fail_update_two_once(update: Update) -> Any:
        if update.update_id == 2 and failures["remaining"]:
            failures["remaining"] -= 1
            raise PydanticSerializationError("unable to serialize")
        return real_serialize(update)

    with mock.patch.object(durable_polling, "serialize_update", side_effect=fail_update_two_once):
        _run_poll(_bot(), first_dispatcher, first_inbox, first_fake)

    before_restart = SqliteInboundUpdateRepository(state_path)
    completed_before_restart = before_restart.get(1)
    assert first_dispatcher.processed == [1]
    assert completed_before_restart is not None
    assert completed_before_restart.state is UpdateProcessingState.COMPLETED
    assert before_restart.get(2) is None
    assert before_restart.get(3) is None

    restart_repo = SqliteInboundUpdateRepository(state_path)
    restart_repo.initialize()
    restart_inbox = DurableUpdateInbox(restart_repo)
    restart_dispatcher = _StubDispatcher()
    restart_controller = _Controller()
    restart_fake = _FakeTelegram(
        [_message_update(1), _message_update(2), _message_update(3)], restart_controller
    )
    restart_bot = _bot()
    restart_bot.get_updates = restart_fake.get_updates  # type: ignore[assignment]

    async def restart() -> int:
        replayed = await durable_polling.replay_pending_updates(
            restart_bot,
            restart_dispatcher,  # type: ignore[arg-type]
            restart_inbox,
        )
        await durable_poll(
            restart_bot,
            restart_dispatcher,  # type: ignore[arg-type]
            restart_inbox,
            polling_timeout=10,
            stopped=lambda: restart_controller.done,
        )
        return replayed

    assert asyncio.run(restart()) == 0
    assert restart_dispatcher.processed == [2, 3]
    assert restart_fake.offset_calls == [None, 4]


def test_serialization_failure_is_bounded_and_quarantined(tmp_path: Path) -> None:
    bot = _bot()
    inbox = _inbox(tmp_path)
    controller = _Controller()
    # update 1 works; update 2 never serializes; update 3 waits behind it until quarantine.
    fake = _FakeTelegram([_message_update(1), _message_update(2), _message_update(3)], controller)
    dispatcher = _StubDispatcher()

    def poison_serialize(update: Update) -> Any:
        if update.update_id == 2:
            raise PydanticSerializationError("permanently unserializable")
        return serialize_update(update)

    with mock.patch.object(durable_polling, "serialize_update", side_effect=poison_serialize):
        _run_poll(bot, dispatcher, inbox, fake)

    # Update 3 advances only after update 2 has a durable terminal tombstone.
    assert dispatcher.processed == [1, 3]
    # The poison update was durably quarantined as terminal (never replayed, never a crash loop).
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    quarantined = repo.get(2)
    assert quarantined is not None
    assert quarantined.state.value == "terminal_failure"
    # Its payload is an audit marker, never user content, and never replayed/parsed.
    assert json.loads(quarantined.payload_json)["_unserializable"] is True
    # Offset eventually advanced past the quarantined update so the loop terminates.
    assert fake.offset_calls == [None, 2, 2, 4]
    assert inbox.pending_count() == 0


def test_live_handler_failure_is_logged_and_bounded(tmp_path: Path) -> None:
    bot = _bot()
    inbox = _inbox(tmp_path)
    controller = _Controller()
    fake = _FakeTelegram([_message_update(1)], controller)

    class FailingDispatcher(_StubDispatcher):
        async def feed_update(self, bot: Any, update: Update, **_kwargs: object) -> bool:
            raise RuntimeError("boom")

    events: list[dict[str, Any]] = []

    async def run() -> None:
        bot.get_updates = fake.get_updates  # type: ignore[assignment]
        with mock.patch("telegram_media_bot.telegram.durable_polling.logger.awarning") as warn:

            def _capture(event: str, **kw):  # type: ignore[no-untyped-def]
                events.append({"event": event, **kw})

            warn.side_effect = _capture
            await durable_poll(
                bot,
                FailingDispatcher(),  # type: ignore[arg-type]
                inbox,
                polling_timeout=10,
                stopped=lambda: fake.controller.done,
            )

    asyncio.run(run())
    handler_failures = [e for e in events if e["event"] == "telegram_update_handler_failed"]
    assert len(handler_failures) == 1
    assert handler_failures[0]["update_id"] == 1
    assert handler_failures[0]["error_category"] == "RuntimeError"
    # No payload / message text / user ids are ever included.
    assert not any("payload" in str(k) for e in events for k in e)


# ---------------------------------------------------------------------------
# Quarantine durable-state invariants at the store level
# ---------------------------------------------------------------------------


def test_quarantine_is_atomic_terminal_never_replayed(tmp_path: Path) -> None:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    inbox = DurableUpdateInbox(repo)
    assert inbox.quarantine(77, "message", "PydanticSerializationError") is True
    assert inbox.quarantine(77, "message", "PydanticSerializationError") is False  # idempotent
    assert inbox.pending_count() == 0  # never surfaces to replay
    row = repo.get(77)
    assert row is not None
    assert row.state.value == "terminal_failure"


def _plain_update(update_id: int, payload_json: str) -> InboundUpdate:
    return InboundUpdate(
        update_id=update_id,
        received_at=datetime.now(UTC),
        update_type="message",
        payload_json=payload_json,
        state=UpdateProcessingState.RECEIVED,
    )
