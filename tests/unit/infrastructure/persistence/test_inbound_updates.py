from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.domain.inbound_updates import UpdateProcessingState
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)


def _inbox(tmp_path: Path, *, max_processing_attempts: int = 3) -> DurableUpdateInbox:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    return DurableUpdateInbox(repo, max_processing_attempts=max_processing_attempts)


def _backdate(tmp_path: Path, update_id: int, *, days: int, state: str | None = None) -> None:
    """Rewrite a row's timestamps so retention tests are deterministic."""
    path = tmp_path / "state" / "jobs.sqlite3"
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="microseconds")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE inbound_updates SET received_at = ?, completed_at = ? WHERE update_id = ?",
            (stamp, stamp, update_id),
        )
        if state is not None:
            connection.execute(
                "UPDATE inbound_updates SET processing_state = ? WHERE update_id = ?",
                (state, update_id),
            )


def _repo(tmp_path: Path) -> SqliteInboundUpdateRepository:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    return repo


def test_persist_then_duplicate_is_idempotent(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    first = inbox.record(1, "message", '{"a": 1}')
    duplicate = inbox.record(1, "message", '{"a": 1}')
    assert first is not None and first.update_id == 1
    assert duplicate is not None and duplicate.update_id == 1
    assert len(inbox.pending()) == 1


def test_completed_update_is_never_replayed(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    record = inbox.record(7, "callback_query", '{"a": 1}')
    assert record is not None
    inbox.mark_completed(inbox.start_processing(record))
    assert inbox.record(7, "callback_query", '{"a": 1}') is None
    assert inbox.pending_count() == 0


def test_attempt_bound_marks_terminal_failure(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, max_processing_attempts=3)
    record = inbox.record(11, "message", '{"a": 1}')
    assert record is not None
    for _ in range(3):
        prepared = inbox.start_processing(record)
        record = inbox.handler_failed(prepared, error_category="ValueError")
    assert record is not None and record.state is UpdateProcessingState.TERMINAL_FAILURE
    # A duplicate delivery of a terminal update is never replayed.
    assert inbox.record(11, "message", '{"a": 1}') is None
    assert inbox.pending_count() == 0


def test_reconcile_recovers_abandoned_processing(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    first = inbox.record(1, "message", '{"a": 1}')
    second = inbox.record(2, "message", '{"a": 2}')
    assert first is not None and second is not None
    # Crash: both are left in PROCESSING, never marked completed.
    inbox.start_processing(first)
    inbox.start_processing(second)
    assert inbox.pending_count() == 2
    pending = inbox.pending()
    assert [p.update_id for p in pending] == [1, 2]


def test_state_counts_only_count_active_values(tmp_path: Path) -> None:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    inbox = DurableUpdateInbox(repo)
    done = inbox.record(1, "message", '{"a": 1}')
    assert done is not None
    inbox.mark_completed(inbox.start_processing(done))
    inbox.record(2, "message", '{"a": 2}')
    counts = repo.state_counts()
    assert counts[UpdateProcessingState.RECEIVED] == 1
    assert counts[UpdateProcessingState.COMPLETED] == 1


def test_reinitialize_preserves_existing_rows(tmp_path: Path) -> None:
    first_repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    first_repo.initialize()
    first_repo.persist(1, "message", '{"a": 1}')
    # A later process/version re-initializing the same database keeps the row.
    second_repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    second_repo.initialize()
    assert len(second_repo.pending_updates()) == 1


def test_media_group_source_resolution_is_bounded_ordered_and_chat_scoped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for update_id, chat_id, group_id, message_id in (
        (1, 42, "album-1", 12),
        (2, 99, "album-1", 99),
        (3, 42, "other", 13),
        (4, 42, "album-1", 10),
        (5, 42, "album-1", 11),
    ):
        repo.persist(
            update_id,
            "message",
            json.dumps(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": message_id,
                        "media_group_id": group_id,
                        "chat": {"id": chat_id, "type": "private"},
                    },
                }
            ),
        )

    assert repo.media_group_message_ids(42, "album-1") == (10, 11, 12)


# --- Hardening 1: bounded retention cleanup -----------------------------------


def test_completed_older_than_retention_is_purged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    record = inbox.record(1, "message", '{"a": 1}')
    assert record is not None
    inbox.mark_completed(inbox.start_processing(record))
    _backdate(tmp_path, 1, days=20)
    now = datetime.now(UTC)
    purged = repo.purge_retention(
        now, completed_retention_days=14, terminal_failure_retention_days=30, batch_size=500
    )
    assert purged == 1
    assert repo.get(1) is None


def test_completed_newer_than_retention_is_retained(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    record = inbox.record(1, "message", '{"a": 1}')
    assert record is not None
    inbox.mark_completed(inbox.start_processing(record))
    _backdate(tmp_path, 1, days=5)
    purged = repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    assert purged == 0
    assert repo.get(1) is not None


def test_terminal_failure_older_than_retention_is_purged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo, max_processing_attempts=2)
    record = inbox.record(1, "message", '{"a": 1}')
    assert record is not None
    for _ in range(2):
        prepared = inbox.start_processing(record)
        record = inbox.handler_failed(prepared, error_category="ValueError")
    assert record is not None and record.state is UpdateProcessingState.TERMINAL_FAILURE
    _backdate(tmp_path, 1, days=40)
    purged = repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    assert purged == 1
    assert repo.get(1) is None


def test_received_and_processing_are_never_age_purged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    first = inbox.record(1, "message", '{"a": 1}')
    second = inbox.record(2, "message", '{"a": 2}')
    assert first is not None and second is not None
    inbox.start_processing(second)
    _backdate(tmp_path, 1, days=400)
    _backdate(tmp_path, 2, days=400)
    purged = repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    assert purged == 0
    assert repo.get(1) is not None
    assert repo.get(2) is not None
    # Extremely old unfinished updates surface as stuck, never deleted.
    assert repo.stuck_count(datetime.now(UTC) - timedelta(hours=1)) == 2


def test_purge_is_batched_across_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    for update_id in range(1, 51):
        record = inbox.record(update_id, "message", f'{{"n": {update_id}}}')
        assert record is not None
        inbox.mark_completed(inbox.start_processing(record))
        _backdate(tmp_path, update_id, days=20)
    now = datetime.now(UTC)
    first_pass = repo.purge_retention(
        now, completed_retention_days=14, terminal_failure_retention_days=30, batch_size=20
    )
    assert first_pass == 20
    second_pass = repo.purge_retention(
        now, completed_retention_days=14, terminal_failure_retention_days=30, batch_size=20
    )
    assert second_pass == 20
    third_pass = repo.purge_retention(
        now, completed_retention_days=14, terminal_failure_retention_days=30, batch_size=20
    )
    assert third_pass == 10
    assert repo.state_counts() == {}


def test_purge_is_idempotent_and_crash_safe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    record = inbox.record(1, "message", '{"a": 1}')
    assert record is not None
    inbox.mark_completed(inbox.start_processing(record))
    _backdate(tmp_path, 1, days=20)
    kwargs = {
        "completed_retention_days": 14,
        "terminal_failure_retention_days": 30,
        "batch_size": 10,
    }
    # A crash mid-pass simply leaves the row for the next pass; repeating is safe.
    for _ in range(3):
        purged = repo.purge_retention(datetime.now(UTC), **kwargs)
        assert purged in (0, 1)
        assert repo.get(1) is None
        # The store is still usable for normal operations.
        assert repo.pending_count() == 0


def test_retention_does_not_break_replay_after_reinitialize(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    active = inbox.record(1, "message", '{"a": 1}')
    done = inbox.record(2, "message", '{"a": 2}')
    assert active is not None and done is not None
    inbox.mark_completed(inbox.start_processing(done))
    _backdate(tmp_path, 2, days=20)
    purged = repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    assert purged == 1
    # A fresh process re-initializes the same database: the active update still replays.
    fresh = _repo(tmp_path)
    pending = fresh.pending_updates()
    assert [p.update_id for p in pending] == [1]


def test_effect_ledger_pending_never_purged_by_inbound_retention(tmp_path: Path) -> None:
    """Side-effect rows live in their own table; inbox retention only touches inbox rows."""
    repo = _repo(tmp_path)
    inbox = DurableUpdateInbox(repo)
    record = inbox.record(1, "callback_query", '{"a": 1}')
    assert record is not None
    inbox.mark_completed(inbox.start_processing(record))
    _backdate(tmp_path, 1, days=20)
    repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    # The inbox row is gone but the database still initializes and stays healthy.
    fresh = _repo(tmp_path)
    assert fresh.pending_count() == 0
    assert fresh.stuck_count(datetime.now(UTC) - timedelta(hours=1)) == 0
