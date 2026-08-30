from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aiogram.types import Chat, Message, Update, User

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
from telegram_media_bot.telegram.durable_polling import (
    replay_pending_updates,
    serialize_update,
)


def _message_update(update_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2023, 11, 14, tzinfo=UTC),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="T"),
            text="https://example.com/media",
        ),
    )


def test_serialize_round_trips_update(tmp_path: Path) -> None:
    update = _message_update(42)
    serialized = serialize_update(update)
    assert serialized.update_id == 42
    assert serialized.update_type == "message"
    reconstructed = json.loads(serialized.payload_json)
    assert reconstructed["update_id"] == 42
    assert reconstructed["message"]["text"] == "https://example.com/media"


def test_replay_pending_updates_processes_and_completes(tmp_path: Path) -> None:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    inbox = DurableUpdateInbox(repo, max_processing_attempts=3)
    serialized = serialize_update(_message_update(1))
    record = inbox.record(serialized.update_id, serialized.update_type, serialized.payload_json)
    assert record is not None
    inbox.start_processing(record)  # simulate a crash that left processing abandoned

    processed: list[int] = []

    class StubDispatcher:
        async def feed_update(self, bot: object, update: Update, **_kwargs: object) -> bool:
            processed.append(update.update_id)
            return True

    async def run() -> int:
        return await replay_pending_updates(cast(Any, object()), cast(Any, StubDispatcher()), inbox)

    count = asyncio.run(run())
    assert count == 1
    assert processed == [1]
    assert inbox.pending_count() == 0
    assert inbox.record(1, "message", '{"a": 1}') is None  # completed, never replayed


def test_replay_pending_bounded_retry_after_crash_loop(tmp_path: Path) -> None:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    inbox = DurableUpdateInbox(repo, max_processing_attempts=2)

    async def main() -> None:
        for _ in range(2):
            inbox.record(5, "message", "{}")
            for record in inbox.pending():
                failed = inbox.handler_failed(
                    inbox.start_processing(record), error_category="ValueError"
                )
                assert failed is not None
        assert inbox.pending_count() == 0

    asyncio.run(main())
