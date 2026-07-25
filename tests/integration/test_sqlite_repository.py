import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.domain.errors import SelectionExpiredError, SelectionOwnershipError
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryItemRecord,
    DeliveryItemStatus,
    DeliveryMethod,
    DeliveryProvider,
    DownloadMode,
    ErrorCategory,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
    SizeConfidence,
    UserProfile,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository


@pytest.fixture
def repository(tmp_path: Path) -> SqliteJobRepository:
    result = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    result.initialize()
    return result


def test_selection_enforces_owner_and_expiration(repository: SqliteJobRepository) -> None:
    now = datetime.now(UTC)
    selection = SelectionRecord(
        token=SelectionToken("opaque-selection"),
        owner_user_id=1,
        chat_id=2,
        media=_media(),
        allowed_modes=(DownloadMode.BEST,),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    repository.save_selection(selection)
    assert repository.get_selection(selection.token, 1) == selection
    with pytest.raises(SelectionOwnershipError):
        repository.get_selection(selection.token, 9)

    expired = SelectionRecord(
        token=SelectionToken("expired-selection"),
        owner_user_id=1,
        chat_id=2,
        media=_media(),
        allowed_modes=(DownloadMode.BEST,),
        created_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    repository.save_selection(expired)
    with pytest.raises(SelectionExpiredError):
        repository.get_selection(expired.token, 1)


def test_initialize_non_destructively_migrates_legacy_jobs_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                mode TEXT,
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status_message_id INTEGER,
                source TEXT,
                error_category TEXT,
                error_summary TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                delivery_file_id TEXT,
                delivery_file_unique_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, status, chat_id, user_id, url, mode, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "download",
                "queued",
                1,
                2,
                "https://example.com/legacy",
                "best",
                "legacy-key",
                now,
                now,
            ),
        )
        connection.commit()

    migrated = SqliteJobRepository(path)
    migrated.initialize()

    record = migrated.get_job(JobId("legacy-job"))
    assert record is not None
    assert record.container is None
    assert record.container_policy is ContainerPolicy.NATIVE_ONLY
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        user_tables = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name IN ('users', 'user_usage_daily', 'download_usage_events')
            """
        ).fetchone()
    assert {"container", "container_policy"} <= columns
    assert user_tables == (3,)


def test_concurrent_creation_deduplicates_active_jobs(repository: SqliteJobRepository) -> None:
    service = JobService(repository)

    def create() -> JobId:
        record, _created = service.create_download(
            chat_id=10,
            user_id=20,
            url="https://example.com/media",
            mode=DownloadMode.VIDEO_720,
        )
        return record.job_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        job_ids = list(executor.map(lambda _index: create(), range(32)))
    assert len(set(job_ids)) == 1


def test_cancel_transition_counts_and_dynamic_blocks(repository: SqliteJobRepository) -> None:
    record, created = JobService(repository).create_download(
        chat_id=1,
        user_id=2,
        url="https://example.com/media",
        mode=DownloadMode.BEST,
    )
    assert created
    assert repository.request_cancel(record.job_id, 2)
    assert repository.is_cancel_requested(record.job_id)
    assert not repository.request_cancel(record.job_id, 3)
    repository.transition(
        record.job_id,
        JobStatus.FAILED,
        error_category=ErrorCategory.INTERNAL,
        error_summary="test_failure",
    )
    assert repository.counts().failed == 1
    assert repository.failed_jobs()[0].error_summary == "test_failure"
    repository.block_user(2, blocked_by=99)
    assert repository.is_user_blocked(2)
    repository.unblock_user(2)
    assert not repository.is_user_blocked(2)


def test_restart_reconciliation_avoids_uncertain_duplicate_delivery(
    repository: SqliteJobRepository,
) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    running = _job(JobId("running"), JobStatus.RUNNING, old)
    repository.create_job(running)
    service = JobService(repository)
    delivering, created = service.create_download(
        chat_id=1,
        user_id=2,
        url="https://example.com/delivering",
        mode=DownloadMode.BEST,
    )
    assert created
    repository.transition(delivering.job_id, JobStatus.DELIVERING)
    assert not repository.request_cancel(delivering.job_id, delivering.user_id)
    recovered = repository.reconcile_abandoned(datetime.now(UTC) + timedelta(seconds=1))
    statuses = {record.job_id: record.status for record in recovered}
    assert statuses[JobId("running")] is JobStatus.QUEUED
    assert statuses[delivering.job_id] is JobStatus.DELIVERY_UNCERTAIN
    duplicate, created = service.create_download(
        chat_id=delivering.chat_id,
        user_id=delivering.user_id,
        url=delivering.url,
        mode=DownloadMode.BEST,
    )
    assert not created
    assert duplicate.job_id == delivering.job_id


def test_delivery_items_are_upserted_by_job_and_ordinal(
    repository: SqliteJobRepository,
) -> None:
    record = _job(JobId("delivery-items"), JobStatus.DELIVERING, datetime.now(UTC))
    repository.create_job(record)
    repository.upsert_delivery_item(
        DeliveryItemRecord(
            job_id=record.job_id,
            ordinal=1,
            provider=DeliveryProvider.MULTIPART,
            status=DeliveryItemStatus.PENDING,
            method=DeliveryMethod.DOCUMENT,
        )
    )
    repository.upsert_delivery_item(
        DeliveryItemRecord(
            job_id=record.job_id,
            ordinal=1,
            provider=DeliveryProvider.MULTIPART,
            status=DeliveryItemStatus.DELIVERED,
            method=DeliveryMethod.DOCUMENT,
            recipient_message_id=20,
            file_id="staged",
            file_unique_id="staged",
        )
    )

    items = repository.delivery_items(record.job_id)

    assert len(items) == 1
    assert items[0].status is DeliveryItemStatus.DELIVERED
    assert items[0].recipient_message_id == 20


def test_container_fields_and_format_options_survive_round_trip(
    repository: SqliteJobRepository,
) -> None:
    service = JobService(repository)
    record, _ = service.create_download(
        chat_id=1,
        user_id=2,
        url="https://example.com/container",
        mode=DownloadMode.VIDEO_1080,
        container=OutputContainer.WEBM,
        container_policy=ContainerPolicy.GUARANTEED,
    )
    loaded = repository.get_job(record.job_id)
    assert loaded is not None
    assert loaded.container is OutputContainer.WEBM
    assert loaded.container_policy is ContainerPolicy.GUARANTEED


def test_user_usage_is_idempotent_and_persistent(
    repository: SqliteJobRepository,
) -> None:
    profile = UserProfile(
        user_id=42,
        private_chat_id=42,
        username="tester",
        first_name="Test",
        last_name="User",
        language_code="fa",
        is_premium=True,
    )
    repository.upsert_user(profile, started=True)
    repository.record_request(42, date(2026, 7, 25))
    assert repository.record_download_outcome(
        job_id=JobId("usage-job"),
        user_id=42,
        day=date(2026, 7, 25),
        succeeded=True,
        delivered_bytes=1024,
    )
    assert not repository.record_download_outcome(
        job_id=JobId("usage-job"),
        user_id=42,
        day=date(2026, 7, 25),
        succeeded=True,
        delivered_bytes=1024,
    )
    connection = sqlite3.connect(repository._path)
    try:
        user = connection.execute(
            "SELECT request_count, successful_download_count, delivered_bytes FROM users "
            "WHERE user_id = 42"
        ).fetchone()
    finally:
        connection.close()
    assert user == (1, 1, 1024)


def test_atomic_download_completion_updates_job_and_usage_once(
    repository: SqliteJobRepository,
) -> None:
    record, _ = JobService(repository).create_download(
        chat_id=42,
        user_id=42,
        url="https://example.com/atomic-completion",
        mode=DownloadMode.BEST,
    )
    repository.transition(record.job_id, JobStatus.DELIVERING)

    assert repository.complete_download(
        record.job_id,
        user_id=42,
        day=date(2026, 7, 25),
        source="youtube",
        delivery_file_id="file-id",
        delivery_file_unique_id="unique-id",
        attempt=1,
        delivered_bytes=2048,
    )
    assert not repository.complete_download(
        record.job_id,
        user_id=42,
        day=date(2026, 7, 25),
        source="youtube",
        delivery_file_id="file-id",
        delivery_file_unique_id="unique-id",
        attempt=1,
        delivered_bytes=2048,
    )

    completed = repository.get_job(record.job_id)
    assert completed is not None
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.delivery_file_id == "file-id"
    with closing(sqlite3.connect(repository._path)) as connection:
        usage = connection.execute(
            """
            SELECT successful_download_count, failed_download_count, delivered_bytes
            FROM users WHERE user_id = 42
            """
        ).fetchone()
    assert usage == (1, 0, 2048)


def test_sqlite_wal_handles_concurrent_atomic_completions(
    repository: SqliteJobRepository,
) -> None:
    jobs: list[JobRecord] = []
    service = JobService(repository)
    for index in range(32):
        record, created = service.create_download(
            chat_id=77,
            user_id=77,
            url=f"https://example.com/contention/{index}",
            mode=DownloadMode.BEST,
        )
        assert created
        repository.transition(record.job_id, JobStatus.DELIVERING)
        jobs.append(record)

    def complete(record: JobRecord) -> bool:
        return repository.complete_download(
            record.job_id,
            user_id=77,
            day=date(2026, 7, 25),
            source="youtube",
            delivery_file_id=f"file-{record.job_id}",
            delivery_file_unique_id=f"unique-{record.job_id}",
            attempt=1,
            delivered_bytes=1024,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        recorded = list(executor.map(complete, jobs))

    assert all(recorded)
    with closing(sqlite3.connect(repository._path)) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        usage = connection.execute(
            """
            SELECT successful_download_count, failed_download_count, delivered_bytes
            FROM users WHERE user_id = 77
            """
        ).fetchone()
        daily = connection.execute(
            """
            SELECT successful_download_count, failed_download_count, delivered_bytes
            FROM user_usage_daily WHERE user_id = 77 AND usage_date = '2026-07-25'
            """
        ).fetchone()
    assert journal_mode == ("wal",)
    assert usage == (32, 0, 32 * 1024)
    assert daily == usage


def test_legacy_idempotency_key_is_preserved_without_container(
    repository: SqliteJobRepository,
) -> None:
    url = "https://example.com/legacy-idempotency"
    material = "\x00".join(("download", "55", url, "best"))
    legacy_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    existing = _job(JobId("legacy-active"), JobStatus.QUEUED, datetime.now(UTC))
    existing = replace(
        existing,
        user_id=55,
        url=url,
        idempotency_key=legacy_key,
    )
    repository.create_job(existing)

    found, created = JobService(repository).create_download(
        chat_id=55,
        user_id=55,
        url=url,
        mode=DownloadMode.BEST,
    )
    assert not created
    assert found.job_id == existing.job_id


def _media() -> MediaInfo:
    return MediaInfo(
        media_id="media-1",
        title="Title",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.com/media",
        estimated_size_bytes=123,
        format_options=(
            MediaFormatOption(
                mode=DownloadMode.VIDEO_1080,
                height=1080,
                size_bytes=456,
                size_confidence=SizeConfidence.ESTIMATED,
            ),
        ),
    )


def _job(job_id: JobId, status: JobStatus, updated: datetime) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        kind=JobKind.DOWNLOAD,
        status=status,
        chat_id=1,
        user_id=2,
        url=f"https://example.com/{job_id}",
        mode=DownloadMode.BEST,
        idempotency_key=f"key-{job_id}",
        created_at=updated,
        updated_at=updated,
    )
