from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.domain.errors import SelectionExpiredError, SelectionOwnershipError
from telegram_media_bot.domain.models import (
    DownloadMode,
    ErrorCategory,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaInfo,
    MediaKind,
    SelectionRecord,
    SelectionToken,
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


def _media() -> MediaInfo:
    return MediaInfo(
        media_id="media-1",
        title="Title",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.com/media",
        estimated_size_bytes=123,
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
