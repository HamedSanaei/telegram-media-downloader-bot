from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DeliveryError,
    JobCancelledError,
    MediaTooLargeError,
    RateLimitedError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryMethod,
    DeliveryProgressEvent,
    DeliveryReceipt,
    DeliveryStage,
    DownloadMode,
    DownloadResult,
    ErrorCategory,
    JobId,
    JobStatus,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    ProgressEvent,
    SizeConfidence,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.storage.workspace import WorkspaceCleanupReport
from telegram_media_bot.workers import jobs as jobs_module
from telegram_media_bot.workers.jobs import process_download_job, process_inspection_job


class FakeDownloadService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0
        self.urls: list[str] = []

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
        del mode
        self.calls += 1
        self.urls.append(url)
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
        self.file_present_during_delivery = False

    async def deliver(self, **kwargs: object) -> DeliveryReceipt:
        self.deliveries += 1
        result = cast(DownloadResult, kwargs["result"])
        self.file_present_during_delivery = result.file_path.is_file()
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


class FakeYoutubeInspectionService:
    def inspect(self, url: str) -> MediaInfo:
        return MediaInfo(
            media_id="abcdefghijk",
            title="YouTube video",
            source="youtube",
            kind=MediaKind.VIDEO,
            webpage_url=url,
            format_options=(
                MediaFormatOption(
                    mode=DownloadMode.VIDEO_1080,
                    container=OutputContainer.MP4,
                    container_policy=ContainerPolicy.NATIVE_ONLY,
                    selected_format_ids=("137", "140"),
                    width=1920,
                    height=1080,
                    fps=30,
                    video_codec="avc1.640028",
                    audio_codec="mp4a.40.2",
                    size_bytes=7_000_000,
                    size_confidence=SizeConfidence.EXACT,
                ),
            ),
        )


class FakeTwitterInspectionWithoutNativeFormats:
    def inspect(self, url: str) -> MediaInfo:
        return MediaInfo(
            media_id="1951000000000000000",
            title="Twitter video",
            source="twitter",
            kind=MediaKind.VIDEO,
            webpage_url=url,
        )


class FakeInspectionBot:
    def __init__(self, *, fail_edit: bool) -> None:
        self.fail_edit = fail_edit
        self.edits: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def edit_message_text(self, **kwargs: object) -> object:
        self.edits.append(kwargs)
        if self.fail_edit:
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=cast(int, kwargs["chat_id"]),
                    message_id=cast(int, kwargs["message_id"]),
                    text=str(kwargs["text"]),
                ),
                message="message can't be edited",
            )
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def send_message(self, **kwargs: object) -> object:
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.messages))


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
    assert delivery.file_present_during_delivery
    with closing(sqlite3.connect(repository._path)) as connection:
        usage = connection.execute(
            "SELECT successful_download_count, delivered_bytes FROM users WHERE user_id = 20"
        ).fetchone()
    assert usage == (1, 5)
    assert not (cast(Settings, context["settings"]).storage.downloads_path() / job_id).exists()


async def test_recovered_youtube_mix_job_is_normalized_at_execution_boundary(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, _repository, service, _delivery = worker_context
    raw = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=raw,
        mode=DownloadMode.BEST.value,
    )

    assert service.urls == ["https://www.youtube.com/watch?v=DGbwtVtthu8"]


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
        user_id=99,
        url="https://example.test/reel/DbQqWqBDLXS",
    )
    repository.set_status_message(inspection.job_id, 30)
    queue = CapturingQueue()
    bot = FakeInspectionBot(fail_edit=True)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInspectionService(),
        "bot": bot,
        "metrics": MetricsRegistry(),
        "queue": queue,
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=99,
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
    assert persisted.status_message_id == 901
    assert len(bot.edits) == 1
    assert len(bot.messages) == 1


@pytest.mark.parametrize(("user_id", "fail_edit"), [(99, True), (20, False)])
async def test_youtube_inspection_publishes_selection_for_admin_and_regular_user(
    settings: Settings,
    tmp_path: Path,
    user_id: int,
    fail_edit: bool,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=user_id,
        url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    repository.set_status_message(inspection.job_id, 30)
    bot = FakeInspectionBot(fail_edit=fail_edit)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeYoutubeInspectionService(),
        "bot": bot,
        "metrics": MetricsRegistry(),
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=user_id,
        url=inspection.url,
    )
    await process_inspection_job(
        context,
        chat_id=10,
        user_id=user_id,
        url=inspection.url,
    )

    persisted = repository.get_job(inspection.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED
    assert persisted.status_message_id == (901 if fail_edit else 30)
    assert len(bot.edits) == 1
    assert len(bot.messages) == int(fail_edit)
    published = bot.messages[0] if fail_edit else bot.edits[0]
    keyboard = cast(InlineKeyboardMarkup, published["reply_markup"])
    assert any(
        button.callback_data is not None and button.callback_data.startswith("c2:")
        for row in keyboard.inline_keyboard
        for button in row
    )


async def test_existing_instagram_download_is_reenqueued_for_redis_recovery(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    url = "https://example.test/reel/DbQqWqBDLXS"
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=99,
        url=url,
    )
    existing, created = JobService(repository).create_download(
        chat_id=10,
        user_id=99,
        url=url,
        mode=DownloadMode.BEST_ORIGINAL,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.NATIVE_ONLY,
    )
    assert created
    queue = CapturingQueue()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInspectionService(),
        "bot": FakeInspectionBot(fail_edit=False),
        "metrics": MetricsRegistry(),
        "queue": queue,
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=99,
        url=url,
    )

    assert queue.download is not None
    assert queue.download["job_id"] == existing.job_id


async def test_twitter_native_planning_failure_has_distinct_category_and_source(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=99,
        url="https://x.com/example/status/1951000000000000000?s=20",
    )
    repository.set_status_message(inspection.job_id, 30)
    delivery = FakeDelivery()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeTwitterInspectionWithoutNativeFormats(),
        "bot": FakeInspectionBot(fail_edit=False),
        "delivery": delivery,
        "metrics": MetricsRegistry(),
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=99,
        url=inspection.url,
    )

    persisted = repository.get_job(inspection.job_id)
    assert persisted is not None
    assert persisted.status is JobStatus.FAILED
    assert persisted.source == "twitter"
    assert persisted.error_category is ErrorCategory.FORMAT_UNAVAILABLE
    assert delivery.edits


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
    configured = cast(Settings, context["settings"])
    assert not (configured.storage.downloads_path() / str(record.job_id)).exists()
    assert not (configured.storage.temp_path() / str(record.job_id)).exists()
    assert any("دانلود کامل شد" in text for text in delivery.edits)


async def test_partial_download_failure_removes_every_job_file(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, delivery = worker_context
    configured = cast(Settings, context["settings"])

    class PartialFailureService(FakeDownloadService):
        def download(self, **kwargs: Any) -> DownloadResult:
            output = cast(Path, kwargs["output_directory"])
            temporary = cast(Path, kwargs["temp_directory"])
            output.mkdir(parents=True)
            temporary.mkdir(parents=True)
            (output / "video.part").write_bytes(b"partial")
            (output / "metadata.info.json").write_text("{}", encoding="utf-8")
            (temporary / "ffmpeg.tmp.mp4").write_bytes(b"incomplete")
            raise MediaTooLargeError("source exceeded limit")

    context["download_service"] = PartialFailureService()
    job_id = JobId(str(context["job_id"]))

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(job_id)
    assert record is not None and record.status is JobStatus.FAILED
    assert delivery.deliveries == 0
    assert not (configured.storage.downloads_path() / str(job_id)).exists()
    assert not (configured.storage.temp_path() / str(job_id)).exists()


async def test_cleanup_failure_does_not_hide_the_original_job_failure(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, repository, service, _delivery = worker_context
    service.failure = MediaTooLargeError("original failure")
    monkeypatch.setattr(
        jobs_module,
        "cleanup_job_workspace",
        lambda *_args, **_kwargs: WorkspaceCleanupReport(failed_paths_count=1),
    )

    result = await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(JobId(result))
    assert record is not None and record.status is JobStatus.FAILED
    assert record.error_category is ErrorCategory.TOO_LARGE


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
