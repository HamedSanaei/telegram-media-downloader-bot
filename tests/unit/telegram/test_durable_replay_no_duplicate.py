from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aiogram.types import Chat, Message, Update, User

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
from telegram_media_bot.telegram.durable_polling import replay_pending_updates, serialize_update


def _message_update(update_id: int, url: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2023, 11, 14, tzinfo=UTC),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="T"),
            text=url,
        ),
    )


class IdempotentJobCreator:
    """Stand-in for JobService: dedupes by a stable key like the real repository."""

    def __init__(self) -> None:
        self._created: set[tuple[str, str]] = set()
        self.effective_jobs = 0

    def create_download(self, user_id: int, url: str) -> bool:
        key = (str(user_id), url)
        if key in self._created:
            return False
        self._created.add(key)
        self.effective_jobs += 1
        return True


def test_replay_after_crash_in_job_creation_creates_exactly_one_job(tmp_path: Path) -> None:
    # Scenario B: the update is durably recorded, the handler created one job, then the process
    # died before marking the inbound update completed. After restart the update is replayed, and
    # the idempotent job creator must NOT enqueue a second identical job.
    url = "https://example.com/media"
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    creator = IdempotentJobCreator()

    class Handler:
        def __init__(self) -> None:
            self.feed_count = 0

        async def feed_update(self, bot: object, update: Update, **_kwargs: object) -> bool:
            self.feed_count += 1
            return True

    inbox = DurableUpdateInbox(repo, max_processing_attempts=3)
    serialized = serialize_update(_message_update(7, url))
    inbox.record(serialized.update_id, serialized.update_type, serialized.payload_json)
    # Handler runs and creates the durable job before the update is marked completed.
    creator.create_download(1, url)
    # Crash: the update is left in `processing` (abandoned) instead of `completed`.

    # --- simulated restart (new process, same SQLite DB) ---
    restarted_repo = SqliteInboundUpdateRepository(repo._path)
    restarted_repo.initialize()
    restarted_inbox = DurableUpdateInbox(restarted_repo, max_processing_attempts=3)

    class StubDispatcher:
        async def feed_update(self, bot: object, update: Update, **_kwargs: object) -> bool:
            # Replaying the handler tries to create the job again.
            creator.create_download(1, url)
            return True

    async def main() -> None:
        # Old process created one effective job.
        assert creator.effective_jobs == 1
        # Restart reconciles the abandoned update.
        assert restarted_inbox.pending_count() == 1
        count = await replay_pending_updates(
            cast(Any, object()), cast(Any, StubDispatcher()), restarted_inbox
        )
        assert count == 1
        assert creator.effective_jobs == 1
        assert restarted_inbox.pending_count() == 0

    asyncio.run(main())
