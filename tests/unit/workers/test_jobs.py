from __future__ import annotations

import asyncio
import errno
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.cookie_health_service import (
    CookieHealthAlert,
    CookieHealthService,
)
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.audit import AuditCategory, AuditEventType
from telegram_media_bot.domain.cookie_health import (
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DeliveryError,
    DownloadFailedError,
    GalleryDlCookiesExpiredError,
    JobCancelledError,
    LocalRuntimeError,
    MediaTooLargeError,
    RateLimitedError,
)
from telegram_media_bot.domain.failures import (
    FailureContext,
    FailureStage,
    render_failure_notification,
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
    JobKind,
    JobRecord,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    ProgressEvent,
    SizeConfidence,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementSnapshot,
    GrantId,
    PlanId,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.storage.workspace import WorkspaceCleanupReport
from telegram_media_bot.workers import jobs as jobs_module
from telegram_media_bot.workers.jobs import process_download_job, process_inspection_job

LOGGER_CHANNEL = -1001234567890


def _load_audit_events(audit_store: SqliteAuditRepository) -> list[dict[str, object]]:
    import json
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(audit_store._path)) as connection:
        rows = connection.execute("SELECT event_json FROM audit_events").fetchall()
    return [json.loads(str(row[0])) for row in rows]


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
        self.last_caption: str | None = None
        self.last_source_url: str | None = None

    async def deliver(self, **kwargs: object) -> DeliveryReceipt:
        self.deliveries += 1
        result = cast(DownloadResult, kwargs["result"])
        self.last_caption = cast(str, kwargs["caption"])
        self.last_source_url = cast(str | None, kwargs.get("source_url"))
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


class FakeInstagramImageInspectionService:
    def inspect(self, url: str) -> MediaInfo:
        assets = (
            MediaAsset(1, "image", MediaKind.IMAGE, "jpg", "image/jpeg", "post", "instagram"),
            MediaAsset(2, "video", MediaKind.VIDEO, "mp4", "video/mp4", "post", "instagram"),
        )
        return MediaInfo(
            "post",
            "Instagram mixed post",
            "instagram",
            MediaKind.PLAYLIST,
            url,
            item_count=2,
            format_options=(
                MediaFormatOption(
                    DownloadMode.ALL_ORIGINAL_MEDIA,
                    selected_format_ids=("image", "video"),
                ),
            ),
            assets=assets,
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
    def __init__(
        self,
        *,
        fail_edit: bool,
        fail_send_chat_ids: tuple[int, ...] = (),
    ) -> None:
        self.fail_edit = fail_edit
        self.fail_send_chat_ids = frozenset(fail_send_chat_ids)
        self.edits: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self.send_attempts: list[dict[str, object]] = []

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
        self.send_attempts.append(kwargs)
        if kwargs["chat_id"] in self.fail_send_chat_ids:
            raise RuntimeError("sensitive admin transport detail")
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
    assert delivery.last_source_url == "https://example.com/media"
    assert delivery.last_caption is not None and "@telegram_media_bot" in delivery.last_caption
    with closing(sqlite3.connect(repository._path)) as connection:
        usage = connection.execute(
            "SELECT successful_download_count, delivered_bytes FROM users WHERE user_id = 20"
        ).fetchone()
    assert usage == (1, 5)
    assert not (cast(Settings, context["settings"]).storage.downloads_path() / job_id).exists()
    assert not (cast(Settings, context["settings"]).storage.temp_path() / job_id).exists()


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
    assert _delivery.last_source_url == "https://example.com/media"


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


async def test_instagram_auto_download_inherits_parent_entitlement_snapshot(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """An automatically created child download inherits the already-accepted parent snapshot.

    This is a system-generated continuation of an already-authorized user intent, so it must NOT be
    reauthorized as a new request. Public inspections carry no snapshot, so the child is unchanged.
    """
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    now = datetime.now(UTC)
    snapshot = EntitlementSnapshot(
        capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
        accepted_at=now,
        authorized_until=now,
        plan_id=PlanId("vip-1"),
        grant_id=GrantId("g1"),
    )
    parent = JobRecord(
        job_id=JobId("parent-inspection"),
        kind=JobKind.INSPECTION,
        status=JobStatus.QUEUED,
        chat_id=10,
        user_id=99,
        url="https://example.test/reel/DbQqWqBDLXS",
        mode=None,
        idempotency_key="parent-key",
        created_at=now,
        updated_at=now,
        entitlement_snapshot=snapshot,
    )
    repository.create_job(parent)
    repository.set_status_message(parent.job_id, 30)
    queue = CapturingQueue()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInspectionService(),
        "bot": FakeInspectionBot(fail_edit=True),
        "metrics": MetricsRegistry(),
        "queue": queue,
        "job_id": str(parent.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=99,
        url=parent.url,
    )

    assert queue.download is not None
    child = repository.get_job(cast(JobId, queue.download["job_id"]))
    assert child is not None
    assert child.entitlement_snapshot == snapshot


async def test_public_instagram_download_child_has_no_snapshot(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A public inspection produces a child download with no entitlement snapshot (unchanged flow)."""
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
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
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInspectionService(),
        "bot": FakeInspectionBot(fail_edit=True),
        "metrics": MetricsRegistry(),
        "queue": queue,
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }
    await process_inspection_job(context, chat_id=10, user_id=99, url=inspection.url)
    assert queue.download is not None
    child = repository.get_job(cast(JobId, queue.download["job_id"]))
    assert child is not None
    assert child.entitlement_snapshot is None


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


async def test_instagram_image_inspection_requires_photo_or_file_confirmation(
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
        user_id=20,
        url="https://www.instagram.com/p/post/",
    )
    repository.set_status_message(inspection.job_id, 30)
    bot = FakeInspectionBot(fail_edit=False)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeInstagramImageInspectionService(),
        "bot": bot,
        "metrics": MetricsRegistry(),
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(
        context,
        chat_id=10,
        user_id=20,
        url=inspection.url,
    )

    keyboard = cast(InlineKeyboardMarkup, bot.edits[0]["reply_markup"])
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert all(callback is not None for callback in callbacks)
    safe_callbacks = [cast(str, callback) for callback in callbacks]
    assert safe_callbacks[0].startswith("i2:") and safe_callbacks[0].endswith(":photo")
    assert safe_callbacks[1].startswith("i2:") and safe_callbacks[1].endswith(":document")
    assert safe_callbacks[2].startswith("n2:")
    assert all(not callback.startswith("m2:") for callback in safe_callbacks)


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
    bot = FakeInspectionBot(fail_edit=False)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeTwitterInspectionWithoutNativeFormats(),
        "bot": bot,
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
    assert not bot.messages


async def test_terminal_controlled_inspection_failure_alerts_each_unique_admin(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["admin_ids"] = [99, 100, 99]
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    url = "https://x.com/example/status/1951000000000000000?s=20"
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=20,
        url=url,
    )
    repository.set_status_message(inspection.job_id, 30)
    bot = FakeInspectionBot(fail_edit=False)
    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FakeTwitterInspectionWithoutNativeFormats(),
        "bot": bot,
        "delivery": FakeDelivery(),
        "metrics": MetricsRegistry(),
        "audit": AuditService(audit_store, enabled=True),
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }

    await process_inspection_job(context, chat_id=10, user_id=20, url=url)

    # Terminal failures route to the logger; no administrator direct messages.
    assert bot.messages == []
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    text = str(events[0]["message"])
    assert str(inspection.job_id) in text
    assert "inspection" in text
    assert "twitter" in text
    assert ErrorCategory.FORMAT_UNAVAILABLE.value in text
    assert "تلاش: 1/2" in text
    assert url not in text
    assert "user_id" not in text
    assert "chat_id" not in text
    assert "Twitter without native formats" not in text


async def test_terminal_unexpected_inspection_failure_alert_is_redacted(
    settings: Settings,
    tmp_path: Path,
) -> None:
    class FailingInspectionService:
        def inspect(self, _url: str) -> MediaInfo:
            raise RuntimeError("unexpected inspection detail")

    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    url = "https://example.com/private-query?token=secret"
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=20,
        url=url,
    )
    repository.set_status_message(inspection.job_id, 30)
    bot = FakeInspectionBot(fail_edit=False)
    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": FailingInspectionService(),
        "bot": bot,
        "delivery": FakeDelivery(),
        "metrics": MetricsRegistry(),
        "audit": AuditService(audit_store, enabled=True),
        "job_id": str(inspection.job_id),
        "job_try": configured.queue.max_tries,
    }

    await process_inspection_job(context, chat_id=10, user_id=20, url=url)

    persisted = repository.get_job(inspection.job_id)
    assert persisted is not None and persisted.status is JobStatus.FAILED
    # No automatic administrator direct messages.
    assert bot.messages == []
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    alert = str(events[0]["message"])
    assert ErrorCategory.INTERNAL.value in alert
    assert url not in alert
    # The sanitized safe reason is shown to operators (never the raw URL/query).
    assert "unexpected inspection detail" in alert
    assert "token=" not in alert


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
            output = cast(Path, kwargs["output_directory"])
            temporary = cast(Path, kwargs["temp_directory"])
            output.mkdir(parents=True)
            temporary.mkdir(parents=True)
            (output / "video.part").write_bytes(b"partial")
            (temporary / "ffmpeg.tmp.mp4").write_bytes(b"partial")
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
    configured = cast(Settings, context["settings"])
    assert not (configured.storage.downloads_path() / str(job_id)).exists()
    assert not (configured.storage.temp_path() / str(job_id)).exists()


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


async def test_retryable_failure_alerts_only_after_retries_are_exhausted(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, service, delivery = worker_context
    configured = cast(Settings, context["settings"])
    raw = configured.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    context["settings"] = configured
    bot = FakeInspectionBot(fail_edit=False)
    context["bot"] = bot
    audit_store = SqliteAuditRepository(Path(configured.database_path()).parent / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    context["audit"] = AuditService(audit_store, enabled=True)
    service.failure = RateLimitedError("remote throttled with token=abc1234567890")

    with pytest.raises(Retry):
        await process_download_job(
            context,
            chat_id=10,
            user_id=20,
            url="https://example.com/media",
            mode=DownloadMode.BEST.value,
        )
    assert bot.send_attempts == []
    assert _load_audit_events(audit_store) == []  # no alert while retries remain

    context["job_try"] = configured.queue.max_tries
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(JobId(str(context["job_id"])))
    assert record is not None and record.status is JobStatus.FAILED
    assert delivery.deliveries == 0
    assert bot.messages == []
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    alert = str(events[0]["message"])
    assert ErrorCategory.RATE_LIMITED.value in alert
    assert "token=abc1234567890" not in alert


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


async def test_terminal_unexpected_download_failure_alert_is_redacted(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, service, delivery = worker_context
    configured = cast(Settings, context["settings"])
    raw = configured.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    context["settings"] = configured
    context["job_try"] = configured.queue.max_tries
    bot = FakeInspectionBot(fail_edit=False)
    context["bot"] = bot
    audit_store = SqliteAuditRepository(Path(configured.database_path()).parent / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    context["audit"] = AuditService(audit_store, enabled=True)
    service.failure = RuntimeError("unexpected download private detail")

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media?token=secret",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(JobId(str(context["job_id"])))
    assert record is not None and record.status is JobStatus.FAILED
    assert delivery.deliveries == 0
    assert bot.messages == []
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    alert = str(events[0]["message"])
    assert ErrorCategory.INTERNAL.value in alert
    assert "token=secret" not in alert
    # Sanitized reason is shown to operators; the query secret stays redacted.
    assert "unexpected download private detail" in alert


async def test_delivery_uncertain_alerts_admins_without_changing_cleanup(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, delivery = worker_context
    configured = cast(Settings, context["settings"])
    raw = configured.model_dump()
    raw["telegram"]["admin_ids"] = [99, 100]
    configured = Settings.model_validate(raw)
    context["settings"] = configured
    bot = FakeInspectionBot(fail_edit=False)
    context["bot"] = bot
    audit_store = SqliteAuditRepository(Path(configured.database_path()).parent / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    context["audit"] = AuditService(audit_store, enabled=True)
    delivery.failure = DeliveryError("ambiguous response with private detail")

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )

    record = repository.get_job(JobId(str(context["job_id"])))
    assert record is not None and record.status is JobStatus.DELIVERY_UNCERTAIN
    # No administrator direct messages; the uncertainty is a typed logger event.
    assert bot.messages == []
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    assert events[0]["category"] == AuditCategory.ERROR.value
    assert ErrorCategory.DELIVERY_UNCERTAIN.value in str(events[0]["message"])
    assert "ambiguous response with private detail" in str(events[0]["message"])
    assert not (configured.storage.downloads_path() / str(record.job_id)).exists()
    assert not (configured.storage.temp_path() / str(record.job_id)).exists()


async def test_terminal_failure_emits_audit_event_without_admin_dm(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99, 100]
    configured = Settings.model_validate(raw)
    bot = FakeInspectionBot(fail_edit=False)
    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    audit = AuditService(audit_store, enabled=True)

    jobs_module._emit_terminal_failure_event(
        {"settings": configured, "bot": bot, "audit": audit},
        context=FailureContext(
            job_id=JobId("opaque-job"),
            job_kind=JobKind.DOWNLOAD,
            source="instagram",
            error_category=ErrorCategory.INTERNAL,
            attempt=3,
        ),
        status=JobStatus.FAILED,
    )

    # No automatic administrator direct messages.
    assert bot.send_attempts == []
    assert bot.messages == []
    snapshot = audit_store.health_snapshot()
    assert snapshot.pending_effects == 1
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    assert events[0]["category"] == AuditCategory.ERROR.value
    assert events[0]["job_id"] == "opaque-job"
    assert "opaque-job" in str(events[0]["message"])
    assert "sensitive" not in str(events[0]["message"])


def _cookie_alert(
    provider: CookieService = CookieService.INSTAGRAM,
    *,
    new_state: CookieHealthState = CookieHealthState.EXPIRED,
    recovery: bool = False,
) -> CookieHealthAlert:
    now = datetime.now(UTC)
    return CookieHealthAlert(
        provider=provider,
        previous_state=CookieHealthState.HEALTHY if not recovery else CookieHealthState.EXPIRED,
        new_state=new_state,
        health=ProviderCookieHealth(
            provider=provider,
            status=new_state,
            static=StaticCookieCheck(provider, new_state, file_ok=False),
            last_notified_state=new_state,
            last_reminder_at=now,
        ),
        recovery=recovery,
    )


def test_cookie_health_alert_emits_cookie_health_event(tmp_path: Path) -> None:
    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    audit = AuditService(audit_store, enabled=True)
    alert = _cookie_alert()

    jobs_module._emit_cookie_health_event({"audit": audit}, alert)

    snapshot = audit_store.health_snapshot()
    assert snapshot.pending_effects == 1
    events = _load_audit_events(audit_store)
    assert len(events) == 1
    assert events[0]["category"] == AuditCategory.COOKIE_HEALTH.value
    assert events[0]["event_type"] == AuditEventType.COOKIE_HEALTH_CHANGED.value
    assert events[0]["provider"] == "instagram"
    assert "Instagram" in str(events[0]["message"])


def test_runtime_auth_failure_routes_cookie_health_event(
    settings: Settings, tmp_path: Path
) -> None:
    """A real auth failure updates Cookie Health and emits exactly one COOKIE_HEALTH event."""
    from telegram_media_bot.infrastructure.cookies.health import MissingCookieChecker
    from telegram_media_bot.infrastructure.persistence.sqlite_cookie_health import (
        SqliteCookieHealthRepository,
    )

    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    health_store = SqliteCookieHealthRepository(tmp_path / "health.db")
    health_store.initialize()
    health = CookieHealthService(
        health_store,
        MissingCookieChecker(),
    )

    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    ctx = {
        "settings": Settings.model_validate(raw),
        "cookie_health_service": health,
        "audit": AuditService(audit_store, enabled=True),
        "delivery": FakeDelivery(),
        "metrics": MetricsRegistry(),
    }

    async def run() -> None:
        await jobs_module._record_runtime_auth_failure(
            ctx, GalleryDlCookiesExpiredError("Instagram session expired")
        )

    asyncio.run(run())

    events = _load_audit_events(audit_store)
    assert len(events) == 1
    assert events[0]["category"] == AuditCategory.COOKIE_HEALTH.value
    assert events[0]["provider"] == "instagram"
    # The same transition never storms the outbox: re-running the same alert is a no-op.
    asyncio.run(run())
    assert len(_load_audit_events(audit_store)) == 1


def test_cookie_health_identical_alert_enqueues_once(tmp_path: Path) -> None:
    audit_store = SqliteAuditRepository(tmp_path / "audit.db")
    audit_store.initialize()
    audit_store.reconcile_config((LOGGER_CHANNEL,))
    audit = AuditService(audit_store, enabled=True)
    alert = _cookie_alert()

    jobs_module._emit_cookie_health_event({"audit": audit}, alert)
    jobs_module._emit_cookie_health_event({"audit": audit}, alert)

    snapshot = audit_store.health_snapshot()
    assert snapshot.pending_effects == 1
    assert len(_load_audit_events(audit_store)) == 1


def test_cookie_health_emit_failure_never_breaks_job_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStore:
        def health_snapshot(self) -> object:
            raise RuntimeError("storage unavailable")

        def list_destinations(self) -> tuple[object, ...]:
            return ()

        def enqueue(self, _event: object) -> int:
            raise RuntimeError("storage unavailable")

    audit = AuditService(cast(Any, BrokenStore()), enabled=True)
    alert = _cookie_alert()

    class CapturingLogger:
        def __init__(self) -> None:
            self.exceptions: list[tuple[str, dict[str, object]]] = []

        def exception(self, event: str, **kwargs: object) -> None:
            self.exceptions.append((event, kwargs))

        def info(self, _event: str, **_kwargs: object) -> None:
            return None

        def warning(self, _event: str, **_kwargs: object) -> None:
            return None

    captured = CapturingLogger()
    monkeypatch.setattr(jobs_module, "logger", captured)

    # Must not raise: the user job path is never broken by logger storage failures.
    jobs_module._emit_cookie_health_event({"audit": audit}, alert)
    assert captured.exceptions[0][0] == "cookie_health_audit_emit_failed"


async def test_terminal_failure_without_audit_service_logs_only(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, object]]] = []

        def warning(self, event: str, **kwargs: object) -> None:
            self.warnings.append((event, kwargs))

        def info(self, _event: str, **_kwargs: object) -> None:
            return None

    captured = CapturingLogger()
    monkeypatch.setattr(jobs_module, "logger", captured)
    jobs_module._emit_terminal_failure_event(
        {"settings": settings},
        context=FailureContext(
            job_id=JobId("opaque-job"),
            job_kind=JobKind.DOWNLOAD,
            source="instagram",
            error_category=ErrorCategory.INTERNAL,
            attempt=1,
        ),
        status=JobStatus.FAILED,
    )

    event, _fields = captured.warnings[0]
    assert event == "terminal_failure_audit_unavailable"


async def test_success_and_cancellation_do_not_alert_admins(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, _delivery = worker_context
    configured = cast(Settings, context["settings"])
    raw = configured.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    context["settings"] = configured
    bot = FakeInspectionBot(fail_edit=False)
    context["bot"] = bot

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://example.com/media",
        mode=DownloadMode.BEST.value,
    )
    assert bot.send_attempts == []

    cancelled, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=21,
        url="https://example.com/cancelled",
        mode=DownloadMode.BEST,
    )
    repository.request_cancel(cancelled.job_id, 21)
    context["job_id"] = str(cancelled.job_id)
    await process_download_job(
        context,
        chat_id=10,
        user_id=21,
        url=cancelled.url,
        mode=DownloadMode.BEST.value,
    )
    assert bot.send_attempts == []


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


async def test_terminal_timeout_removes_download_and_temp_workspaces(
    worker_context: tuple[dict[str, Any], SqliteJobRepository, FakeDownloadService, FakeDelivery],
) -> None:
    context, repository, _service, delivery = worker_context
    configured = cast(Settings, context["settings"])
    context["job_try"] = configured.queue.max_tries

    class TimedOutService(FakeDownloadService):
        def download(self, **kwargs: Any) -> DownloadResult:
            output = cast(Path, kwargs["output_directory"])
            temporary = cast(Path, kwargs["temp_directory"])
            output.mkdir(parents=True)
            temporary.mkdir(parents=True)
            (output / "video.part").write_bytes(b"partial")
            (temporary / "socket.tmp").write_bytes(b"partial")
            raise DownloadFailedError("download timed out")

    context["download_service"] = TimedOutService()
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
    configured = cast(Settings, context["settings"])
    assert not (configured.storage.downloads_path() / result).exists()


def test_failure_stage_prefers_specialized_classification_over_adapter_hint() -> None:
    exc = AuthenticationRequiredError("Authentication is required")
    exc.failure_stage = FailureStage.INSPECTION

    assert jobs_module._failure_stage_for_exception(exc) is FailureStage.AUTHENTICATION


def test_failure_stage_uses_attached_adapter_hint_when_unclassified() -> None:
    attached = DownloadFailedError("Media download failed")
    attached.failure_stage = FailureStage.INSPECTION

    assert jobs_module._failure_stage_for_exception(attached) is FailureStage.INSPECTION
    assert (
        jobs_module._failure_stage_for_exception(DownloadFailedError("anonymous"))
        is FailureStage.UNKNOWN
    )


def test_local_runtime_failure_context_preserves_the_real_safe_cause(
    settings: Settings,
) -> None:
    """The production admin alert must show the local filesystem cause, not a remote one."""
    exc = LocalRuntimeError(
        "Local temporary workspace is not writable: read-only filesystem [Errno 30]",
        os_errno=errno.EROFS,
        adapter="yt-dlp",
    )
    exc.failure_stage = FailureStage.INSPECTION

    context = jobs_module._build_failure_context(
        {"settings": settings},
        job_id=JobId("job-erofs"),
        kind=JobKind.INSPECTION,
        exc=exc,
        attempt=1,
        stage=jobs_module._failure_stage_for_exception(exc),
        started=None,
    )
    text = render_failure_notification(context)

    assert context.failure_stage is FailureStage.INSPECTION
    assert context.adapter == "yt-dlp"
    assert context.error_category is ErrorCategory.LOCAL_RUNTIME
    assert context.exception_type == "LocalRuntimeError"
    assert context.retryable is False
    assert context.http_status is None
    assert context.safe_error_reason is not None
    assert "Errno 30" in context.safe_error_reason
    assert "read-only" in context.safe_error_reason.casefold()
    assert "Media download failed" not in text
    assert "local_runtime" in text
    assert "inspection" in text
    assert "unknown" not in text
