import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.submission_audit import AcceptedSubmissionAuditService
from telegram_media_bot.domain.audit import TelegramSourceReference
from telegram_media_bot.infrastructure.persistence.sqlite_audit import (
    SqliteAuditRepository,
    deserialize_event,
)


def _record(
    mirror: AcceptedSubmissionAuditService,
    source: TelegramSourceReference,
) -> int:
    return mirror.record_accepted(
        source=source,
        telegram_user_id=4242,
        update_id=1001,
        job_id="inspection-1",
        content_type="photo" if source.media_group_id else "text",
        provider="example.com",
        occurred_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
    )


def test_submission_event_is_idempotent_across_restart_and_fans_out_per_destination(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890, -1001234567891))
    mirror = AcceptedSubmissionAuditService(AuditService(repository, enabled=True), enabled=True)
    source = TelegramSourceReference(4242, (55,))

    assert _record(mirror, source) == 2
    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    replay = AcceptedSubmissionAuditService(AuditService(restarted, enabled=True), enabled=True)
    assert _record(replay, source) == 0

    items = restarted.claim_pending(limit=20)
    assert [item.destination_chat_id for item in items] == [-1001234567891, -1001234567890]
    assert {item.event.telegram_user_id for item in items} == {4242}


def test_logger_and_mirror_flags_are_independent(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    source = TelegramSourceReference(4242, (55,))

    logger_off = AcceptedSubmissionAuditService(
        AuditService(repository, enabled=False), enabled=True
    )
    mirror_off = AcceptedSubmissionAuditService(
        AuditService(repository, enabled=True), enabled=False
    )

    assert _record(logger_off, source) == 0
    assert _record(mirror_off, source) == 0
    assert repository.health_snapshot().pending_effects == 0


def test_no_effective_destination_means_no_submission_event(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    mirror = AcceptedSubmissionAuditService(AuditService(repository, enabled=True), enabled=True)

    assert _record(mirror, TelegramSourceReference(4242, (55,))) == 0

    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_album_extension_preserves_order_and_one_logical_event(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    mirror = AcceptedSubmissionAuditService(AuditService(repository, enabled=True), enabled=True)
    first = TelegramSourceReference(4242, (12,), "album-1")

    assert _record(mirror, first) == 1
    assert (
        mirror.observe_media_group_member(TelegramSourceReference(4242, (11, 13), "album-1")) == 3
    )
    assert _record(mirror, first) == 0

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT event_json FROM audit_events").fetchall()
        outbox = connection.execute("SELECT COUNT(*) FROM logger_outbox").fetchone()
    assert len(rows) == 1
    assert outbox == (1,)
    event = deserialize_event(str(rows[0][0]))
    assert event.source is not None
    assert event.source.message_ids == (11, 12, 13)
