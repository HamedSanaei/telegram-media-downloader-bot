import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import CopyMessage

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.delivery_output_audit import (
    DeliveredOutputAuditService,
)
from telegram_media_bot.domain.audit import AuditEventType
from telegram_media_bot.domain.models import (
    DeliveryItemRecord,
    DeliveryItemStatus,
    DeliveryMethod,
    DeliveryProvider,
    DownloadMode,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import (
    SqliteAuditRepository,
    deserialize_event,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.telegram.audit_delivery import TelegramAuditDelivery

_DESTINATION = -1001234567890
_DEFAULT_JOB_ID = JobId("download-1")


class _CopyingBot:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.copies: list[dict[str, object]] = []
        self.messages: list[tuple[int, str]] = []

    async def copy_message(self, **kwargs: object) -> None:
        if self.failure is not None:
            raise self.failure
        self.copies.append(kwargs)

    async def copy_messages(self, **_kwargs: object) -> None:
        if self.failure is not None:
            raise self.failure

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def _repositories(path: Path) -> tuple[SqliteJobRepository, SqliteAuditRepository]:
    jobs = SqliteJobRepository(path)
    jobs.initialize()
    audit = SqliteAuditRepository(path)
    audit.initialize()
    audit.reconcile_config((_DESTINATION,))
    return jobs, audit


def _job(job_id: JobId = _DEFAULT_JOB_ID) -> JobRecord:
    now = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    return JobRecord(
        job_id=job_id,
        kind=JobKind.DOWNLOAD,
        status=JobStatus.QUEUED,
        chat_id=4242,
        user_id=99,
        url="https://example.com/private-input",
        mode=DownloadMode.BEST,
        idempotency_key=f"key-{job_id}",
        created_at=now,
        updated_at=now,
    )


def _complete(jobs: SqliteJobRepository, job_id: JobId) -> None:
    jobs.complete_download(
        job_id,
        user_id=99,
        day=date(2026, 9, 1),
        source="youtube",
        delivery_file_id="file-1",
        delivery_file_unique_id="unique-1",
        attempt=1,
        delivered_bytes=10,
    )


def _service(
    jobs: SqliteJobRepository,
    audit: SqliteAuditRepository,
    *,
    enabled: bool = True,
) -> DeliveredOutputAuditService:
    return DeliveredOutputAuditService(AuditService(audit, enabled=True), jobs, enabled=enabled)


def test_completed_output_uses_only_ordered_durable_delivered_message_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    jobs, audit = _repositories(path)
    record = jobs.create_job(_job())
    service = _service(jobs, audit)

    assert service.prepare(record.job_id)
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            2,
            DeliveryProvider.MULTIPART,
            DeliveryItemStatus.DELIVERED,
            DeliveryMethod.DOCUMENT,
            recipient_message_id=202,
        )
    )
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            1,
            DeliveryProvider.MULTIPART,
            DeliveryItemStatus.DELIVERED,
            DeliveryMethod.DOCUMENT,
            recipient_message_id=101,
        )
    )
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            3,
            DeliveryProvider.MULTIPART,
            DeliveryItemStatus.UNCERTAIN,
            DeliveryMethod.DOCUMENT,
            recipient_message_id=303,
        )
    )
    _complete(jobs, record.job_id)

    assert service.finalize(record.job_id)
    item = audit.claim_pending(limit=10)[0]
    assert item.event.event_type is AuditEventType.DOWNLOAD_OUTPUT_DELIVERED
    assert item.event.correlation_id == "delivery-output:download-1"
    assert item.event.source is not None
    assert item.event.source.chat_id == 4242
    assert item.event.source.message_ids == (101, 202)
    assert "private-input" not in item.event.message
    assert audit.pending_delivery_outputs() == ()


def test_crash_after_completion_is_reconciled_once_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    jobs, audit = _repositories(path)
    record = jobs.create_job(_job())
    service = _service(jobs, audit)
    assert service.prepare(record.job_id)
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            1,
            DeliveryProvider.BOT_API,
            DeliveryItemStatus.DELIVERED,
            DeliveryMethod.VIDEO,
            recipient_message_id=777,
        )
    )
    _complete(jobs, record.job_id)

    restarted_jobs = SqliteJobRepository(path)
    restarted_jobs.initialize()
    restarted_audit = SqliteAuditRepository(path)
    restarted_audit.initialize()
    restarted = _service(restarted_jobs, restarted_audit)
    assert restarted.reconcile_pending() == 1
    assert restarted.reconcile_pending() == 0

    with closing(sqlite3.connect(path)) as connection:
        events = connection.execute(
            "SELECT event_json FROM audit_events WHERE event_json LIKE ?",
            ('%"event_type":"download_output_delivered"%',),
        ).fetchall()
        effects = connection.execute(
            "SELECT COUNT(*) FROM logger_outbox WHERE event_id IN "
            "(SELECT event_id FROM audit_events WHERE event_json LIKE ?)",
            ('%"event_type":"download_output_delivered"%',),
        ).fetchone()
    assert len(events) == 1
    assert deserialize_event(str(events[0][0])).source is not None
    assert effects == (1,)


async def test_text_instagram_url_mirrors_actual_delivered_video_message(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    jobs, audit = _repositories(path)
    record = jobs.create_job(replace(_job(), url="https://www.instagram.com/reel/example/"))
    service = _service(jobs, audit)
    assert service.prepare(record.job_id)
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            1,
            DeliveryProvider.BOT_API,
            DeliveryItemStatus.DELIVERED,
            DeliveryMethod.VIDEO,
            recipient_message_id=500,
        )
    )
    _complete(jobs, record.job_id)
    assert service.finalize(record.job_id)
    bot = _CopyingBot()

    assert (
        await AuditOutboxProcessor(
            audit, TelegramAuditDelivery(cast(Bot, cast(Any, bot)))
        ).dispatch_batch()
        == 1
    )
    assert bot.copies == [{"chat_id": _DESTINATION, "from_chat_id": 4242, "message_id": 500}]
    assert "instagram.com/reel" not in bot.messages[0][1]


async def test_output_copy_ambiguity_is_uncertain_and_never_retried(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    jobs, audit = _repositories(path)
    record = jobs.create_job(_job())
    service = _service(jobs, audit)
    assert service.prepare(record.job_id)
    jobs.upsert_delivery_item(
        DeliveryItemRecord(
            record.job_id,
            1,
            DeliveryProvider.BOT_API,
            DeliveryItemStatus.DELIVERED,
            DeliveryMethod.VIDEO,
            recipient_message_id=500,
        )
    )
    _complete(jobs, record.job_id)
    assert service.finalize(record.job_id)
    method = CopyMessage(chat_id=_DESTINATION, from_chat_id=4242, message_id=500)
    bot = _CopyingBot(TelegramNetworkError(method=method, message="connection lost"))
    processor = AuditOutboxProcessor(audit, TelegramAuditDelivery(cast(Bot, cast(Any, bot))))

    assert await processor.dispatch_batch() == 0
    assert audit.health_snapshot().uncertain_effects == 1
    assert await processor.dispatch_batch() == 0


def test_non_success_or_missing_receipt_never_guesses_a_message(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    jobs, audit = _repositories(path)
    failed = jobs.create_job(_job(JobId("download-failed")))
    missing = jobs.create_job(_job(JobId("download-missing")))
    service = _service(jobs, audit)
    assert service.prepare(failed.job_id)
    assert service.prepare(missing.job_id)
    jobs.transition(failed.job_id, JobStatus.DELIVERY_UNCERTAIN)
    _complete(jobs, missing.job_id)

    assert service.reconcile_pending() == 2
    assert audit.health_snapshot().pending_effects == 0
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_disabled_output_mirror_does_not_prepare_intent(tmp_path: Path) -> None:
    jobs, audit = _repositories(tmp_path / "state.sqlite3")
    record = jobs.create_job(_job())

    assert not _service(jobs, audit, enabled=False).prepare(record.job_id)
    _complete(jobs, record.job_id)
    assert not _service(jobs, audit).finalize(record.job_id)
    assert audit.pending_delivery_outputs() == ()
    assert audit.health_snapshot().pending_effects == 0


def test_additive_intent_migration_preserves_existing_logger_state(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    audit = SqliteAuditRepository(path)
    audit.initialize()
    audit.reconcile_config((_DESTINATION,))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE logger_delivery_output_intents")

    restarted = SqliteAuditRepository(path)
    restarted.initialize()

    assert restarted.list_destinations()[0].chat_id == _DESTINATION
    assert restarted.pending_delivery_outputs() == ()
