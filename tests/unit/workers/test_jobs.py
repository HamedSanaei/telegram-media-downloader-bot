from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import RateLimitedError
from telegram_media_bot.domain.models import (
    DeliveryMethod,
    DeliveryReceipt,
    DownloadMode,
    DownloadResult,
    JobId,
    JobStatus,
    MediaKind,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.workers.jobs import process_download_job


class FakeDownloadService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def download(
        self,
        *,
        job_id: JobId,
        url: str,
        mode: DownloadMode,
        output_directory: Path,
        temp_directory: Path,
        progress: Callable[[ProgressEvent], None] | None,
        is_cancelled: Callable[[], bool],
    ) -> DownloadResult:
        del url, mode
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert not is_cancelled()
        output_directory.mkdir(parents=True, exist_ok=True)
        temp_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / "media.mp4"
        path.write_bytes(b"media")
        if progress is not None:
            progress(
                ProgressEvent(
                    job_id=job_id,
                    status="downloading",
                    downloaded_bytes=5,
                    total_bytes=5,
                )
            )
        return DownloadResult(
            job_id=job_id,
            media_id="media",
            title="Title",
            source="youtube",
            kind=MediaKind.VIDEO,
            file_path=path,
            file_size_bytes=5,
        )


class FakeDelivery:
    def __init__(self) -> None:
        self.deliveries = 0
        self.edits: list[str] = []

    async def deliver(self, **_kwargs: object) -> DeliveryReceipt:
        self.deliveries += 1
        return DeliveryReceipt(DeliveryMethod.VIDEO, 3, "file-id", "unique-id")

    async def send_text(self, _chat_id: int, _text: str) -> int:
        return 4

    async def edit_text(self, _chat_id: int, _message_id: int, text: str) -> None:
        self.edits.append(text)


@pytest.fixture
def worker_context(
    settings: Settings, tmp_path: Path
) -> tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery]:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST,
    )
    repository.set_status_message(record.job_id, 30)
    service = FakeDownloadService()
    delivery = FakeDelivery()
    context = {
        "settings": configured,
        "repository": repository,
        "download_service": service,
        "delivery": cast(DeliveryGateway, delivery),
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    return context, repository, service, delivery


async def test_worker_download_persists_receipt_and_cleans(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, service, delivery = worker_context
    job_id = await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )
    record = repository.get_job(JobId(job_id))
    assert record is not None
    assert record.status is JobStatus.SUCCEEDED
    assert record.delivery_file_id == "file-id"
    assert service.calls == 1
    assert delivery.deliveries == 1
    assert not (cast(Settings, context["settings"]).storage.downloads_path() / job_id).exists()


async def test_worker_honors_pre_start_cancellation(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, service, delivery = worker_context
    job_id = str(context["job_id"])
    assert repository.request_cancel(JobId(job_id), 20)
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )
    record = repository.get_job(JobId(job_id))
    assert record is not None and record.status is JobStatus.CANCELLED
    assert service.calls == 0
    assert delivery.deliveries == 0


async def test_retryable_failure_is_deferred_without_delivery(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, service, delivery = worker_context
    service.failure = RateLimitedError("remote throttled")
    with pytest.raises(Retry):
        await process_download_job(
            context,
            chat_id=10,
            user_id=20,
            url="https://example.com/media",
            mode=DownloadMode.BEST.value,
        )
    record = repository.get_job(JobId(str(context["job_id"])))
    assert record is not None and record.status is JobStatus.RETRYING
    assert delivery.deliveries == 0
