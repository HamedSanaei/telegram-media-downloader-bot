from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DeliveryError, JobCancelledError, RateLimitedError
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryMethod,
    DeliveryProgressEvent,
    DeliveryReceipt,
    DeliveryStage,
    DownloadMode,
    DownloadResult,
    JobId,
    JobStatus,
    MediaInfo,
    MediaKind,
    OutputContainer,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.workers.jobs import process_download_job, process_inspection_job


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
        self.failure: Exception | None = None

    async def deliver(self, **kwargs: object) -> DeliveryReceipt:
        self.deliveries += 1
        if self.failure is not None:
            progress = kwargs.get("progress")
            if callable(progress):
                progress(
                    DeliveryProgressEvent(
                        job_id=JobId("job"),
                        stage=DeliveryStage.FINALIZING,
                        transferred_bytes=5,
                        total_bytes=5,
                        elapsed_seconds=601,
                    )
                )
            raise self.failure
        receipt = DeliveryReceipt(DeliveryMethod.VIDEO, 3, "file-id", "unique-id")
        item_delivered = kwargs.get("item_delivered")
        if callable(item_delivered):
            await item_delivered(receipt.primary)
        return receipt

    async def send_text(self, _chat_id: int, _text: str) -> int:
        return 4

    async def edit_text(self, _chat_id: int, _message_id: int, text: str) -> None:
        self.edits.append(text)


class FakeInspectionService:
    def inspect(self, url: str) -> MediaInfo:
        return MediaInfo(
            media_id="DbQqWqBDLXS",
            title="Instagram Reel",
            source="instagram",
            kind=MediaKind.VIDEO,
            webpage_url=url,
        )


class CapturingQueue:
    def __init__(self) -> None:
        self.download: dict[str, object] | None = None

    async def enqueue_download(self, **kwargs: object) -> JobId:
        self.download = kwargs
        return cast(JobId, kwargs["job_id"])


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
    with closing(sqlite3.connect(repository._path)) as connection:
        usage = connection.execute(
            "SELECT successful_download_count, delivered_bytes FROM users WHERE user_id = 20"
        ).fetchone()
    assert usage == (1, 5)
    assert not (cast(Settings, context["settings"]).storage.downloads_path() / job_id).exists()


@pytest.mark.parametrize(
    ("force_mp4", "expected_container"),
    [(True, OutputContainer.MP4), (False, None)],
)
async def test_instagram_auto_download_create_and_enqueue_share_native_policy(
    settings: Settings,
    tmp_path: Path,
    force_mp4: bool,
    expected_container: OutputContainer | None,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["media"]["instagram"]["force_mp4"] = force_mp4
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=20,
        url="https://example.test/reel/DbQqWqBDLXS",
    )
    queue = CapturingQueue()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInspectionService(),
        "bot": object(),
        "metrics": MetricsRegistry(),
        "queue": queue,
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=20,
        url=inspection.url,
    )

    assert queue.download is not None
    assert queue.download["mode"] is DownloadMode.BEST_ORIGINAL
    assert queue.download["container"] is expected_container
    assert queue.download["container_policy"] is ContainerPolicy.NATIVE_ONLY
    persisted = repository.get_job(cast(JobId, queue.download["job_id"]))
    assert persisted is not None
    assert persisted.container is expected_container
    assert persisted.container_policy is ContainerPolicy.NATIVE_ONLY


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


async def test_user_cancel_during_worker_shutdown_is_not_requeued(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, delivery = worker_context
    started = threading.Event()
    pause = threading.Event()

    class BlockingDownloadService(FakeDownloadService):
        def download(self, **kwargs: Any) -> DownloadResult:
            started.set()
            is_cancelled = cast(Callable[[], bool], kwargs["is_cancelled"])
            while not is_cancelled():
                pause.wait(0.01)
            raise JobCancelledError("cancelled")

    context["download_service"] = BlockingDownloadService()
    task = asyncio.create_task(
        process_download_job(
            context,
            chat_id=10,
            user_id=20,
            url="https://example.com/media",
            mode=DownloadMode.BEST.value,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    job_id = JobId(str(context["job_id"]))
    assert repository.request_cancel(job_id, 20)
    task.cancel()

    assert await task == str(job_id)
    current = repository.get_job(job_id)
    assert current is not None and current.status is JobStatus.CANCELLED
    assert delivery.deliveries == 0
    assert delivery.edits == []


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


async def test_ambiguous_delivery_is_quarantined_with_specific_user_message(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, delivery = worker_context
    delivery.failure = DeliveryError("ambiguous response")

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(JobId(str(context["job_id"])))
    assert record is not None
    assert record.status is JobStatus.DELIVERY_UNCERTAIN
    assert any("دانلود کامل شد" in text for text in delivery.edits)


async def test_completion_persistence_failure_never_retries_delivery(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, repository, _service, delivery = worker_context

    def fail_completion(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(repository, "complete_download", fail_completion)
    result = await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    assert result == str(context["job_id"])
    record = repository.get_job(JobId(result))
    assert record is not None and record.status is JobStatus.DELIVERY_UNCERTAIN
    assert delivery.deliveries == 1
