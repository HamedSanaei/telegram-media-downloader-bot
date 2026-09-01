import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aiogram import Bot

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.submission_audit import (
    AcceptedSubmissionAuditService,
    mirroring_enabled,
)
from telegram_media_bot.domain.audit import TelegramSourceReference
from telegram_media_bot.infrastructure.persistence.sqlite_audit import (
    SqliteAuditRepository,
    deserialize_event,
)
from telegram_media_bot.infrastructure.telegram.audit_delivery import TelegramAuditDelivery


class CopyingBot:
    def __init__(self) -> None:
        self.copies: list[dict[str, object]] = []
        self.messages: list[tuple[int, str]] = []

    async def copy_message(self, **kwargs: object) -> None:
        self.copies.append(kwargs)

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


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


def test_operator_attestation_still_gates_mirroring() -> None:
    # logger.enabled AND submission_mirror_enabled AND operator_privacy_attested
    assert mirroring_enabled(
        logger_enabled=True,
        submission_mirror_enabled=True,
        operator_privacy_attested=True,
    )
    assert not mirroring_enabled(
        logger_enabled=False,
        submission_mirror_enabled=True,
        operator_privacy_attested=True,
    )
    assert not mirroring_enabled(
        logger_enabled=True,
        submission_mirror_enabled=False,
        operator_privacy_attested=True,
    )
    assert not mirroring_enabled(
        logger_enabled=True,
        submission_mirror_enabled=True,
        operator_privacy_attested=False,
    )
    assert not mirroring_enabled(
        logger_enabled=False,
        submission_mirror_enabled=False,
        operator_privacy_attested=False,
    )


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

    with closing(sqlite3.connect(tmp_path / "state.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_mirroring_requires_no_user_acknowledgement(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    audit = AuditService(repository, enabled=True)
    mirror = AcceptedSubmissionAuditService(audit, enabled=True)
    source = TelegramSourceReference(4242, (55,))

    # Zero user acknowledgement exists and none is ever required: the operator
    # attestation at configuration time is the only privacy gate.
    assert not audit.has_privacy_acknowledgement(4242, "logger-v1")
    assert _record(mirror, source) == 1


def test_legacy_acknowledgement_rows_remain_backward_compatible(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    audit = AuditService(repository, enabled=True)

    assert audit.acknowledge_privacy(4242, "logger-v1")
    assert audit.has_privacy_acknowledgement(4242, "logger-v1")

    # Historical rows never gate mirroring and survive a restart untouched.
    mirror = AcceptedSubmissionAuditService(audit, enabled=True)
    assert _record(mirror, TelegramSourceReference(4243, (56,))) == 1
    restarted = SqliteAuditRepository(tmp_path / "state.sqlite3")
    restarted.initialize()
    assert restarted.has_privacy_acknowledgement(4242, "logger-v1")
    assert restarted.acknowledge_privacy(4242, "logger-v1") is False  # idempotent row


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

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT event_json FROM audit_events").fetchall()
        outbox = connection.execute("SELECT COUNT(*) FROM logger_outbox").fetchone()
    assert len(rows) == 1
    assert outbox == (1,)
    event = deserialize_event(str(rows[0][0]))
    assert event.source is not None
    assert event.source.message_ids == (11, 12, 13)


async def test_accepted_submission_reaches_native_copy_through_durable_outbox(
    tmp_path: Path,
) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    destination = -1001234567890
    repository.reconcile_config((destination,))
    mirror = AcceptedSubmissionAuditService(AuditService(repository, enabled=True), enabled=True)
    source = TelegramSourceReference(4242, (55,))
    bot = CopyingBot()

    assert _record(mirror, source) == 1
    completed = await AuditOutboxProcessor(
        repository,
        TelegramAuditDelivery(cast(Bot, cast(Any, bot))),
    ).dispatch_batch()

    assert completed == 1
    assert bot.copies == [{"chat_id": destination, "from_chat_id": 4242, "message_id": 55}]
    assert "user_id: 4242" in bot.messages[0][1]
    assert repository.health_snapshot().pending_effects == 0
