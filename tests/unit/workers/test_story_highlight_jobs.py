from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import InlineKeyboardMarkup

from telegram_media_bot.application.services.credential_resolution import CredentialResolver
from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import (
    ContentAccessScope,
    CredentialKind,
    CredentialPolicy,
    ResolvedCredential,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryMethod,
    DeliveryReceipt,
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    ErrorCategory,
    HighlightItem,
    JobId,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    SelectionToken,
    StoryDeliveryMode,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementSnapshot,
    GrantId,
    PlanId,
)
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.credentials.materializer import RestrictedCookieMaterializer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.workers.jobs import (
    process_download_job,
    process_highlight_tray_job,
    process_inspection_job,
)


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def edit_message_text(self, **kwargs: object) -> object:
        self.edits.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def send_message(self, **kwargs: object) -> object:
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.messages))


class StoryInspectionService:
    def inspect(self, url: str, *, credential: object | None = None) -> MediaInfo:
        asset = MediaAsset(
            index=1,
            asset_id="story-asset",
            kind=MediaKind.VIDEO,
            extension="mp4",
            mime_type="video/mp4",
            source_post_id="3964254748584813861",
            provider="instagram",
            duration_seconds=8,
        )
        return MediaInfo(
            media_id="3964254748584813861",
            title="Story",
            source="instagram",
            kind=MediaKind.VIDEO,
            webpage_url=url,
            item_count=1,
            format_options=(
                MediaFormatOption(
                    DownloadMode.VIDEO_ORIGINAL,
                    container_policy=ContainerPolicy.NATIVE_ONLY,
                    selected_format_ids=("story-asset",),
                ),
            ),
            assets=(asset,),
        )


class StoryImageInspectionService(StoryInspectionService):
    def inspect(self, url: str, *, credential: object | None = None) -> MediaInfo:
        info = super().inspect(url, credential=credential)
        asset = MediaAsset(
            index=1,
            asset_id="story-image",
            kind=MediaKind.IMAGE,
            extension="jpg",
            mime_type="image/jpeg",
            source_post_id="3964254748584813861",
            provider="instagram",
        )
        return MediaInfo(
            media_id=info.media_id,
            title=info.title,
            source=info.source,
            kind=MediaKind.IMAGE,
            webpage_url=info.webpage_url,
            item_count=1,
            format_options=(
                MediaFormatOption(
                    DownloadMode.IMAGE_ORIGINAL,
                    container_policy=ContainerPolicy.NATIVE_ONLY,
                    selected_format_ids=("story-image",),
                ),
            ),
            assets=(asset,),
        )


class TrayFakeEngine:
    def __init__(
        self,
        items: tuple[HighlightItem, ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.items = items
        self.error = error
        self.calls: list[str] = []

    def fetch_highlight_tray(self, username: str, **kwargs: object) -> tuple[HighlightItem, ...]:
        self.calls.append(username)
        if self.error is not None:
            raise self.error
        return self.items or ()


class BatchDownloadService:
    def __init__(self, artifacts: tuple[Path, ...], fail: bool = False) -> None:
        self.artifacts = artifacts
        self.fail = fail
        self.calls = 0

    def download(self, **kwargs: Any) -> DownloadResult:
        self.calls += 1
        if self.fail:
            from telegram_media_bot.domain.errors import GalleryDlAuthenticationRequiredError

            raise GalleryDlAuthenticationRequiredError(
                "HTTP 401 login required",
                http_status=401,
                extractor="instagram",
            )
        paths = list(self.artifacts)
        first = paths[0]
        return DownloadResult(
            job_id=JobId(str(kwargs["job_id"])),
            media_id="stories",
            title="Stories",
            source="instagram",
            kind=MediaKind.PLAYLIST,
            file_path=first,
            file_size_bytes=sum(path.stat().st_size for path in paths),
            mime_type="image/jpeg",
            artifacts=tuple(
                DownloadArtifact(
                    file_path=path,
                    file_size_bytes=path.stat().st_size,
                    kind=MediaKind.IMAGE,
                    mime_type="image/jpeg",
                    title=f"item{index}",
                    inline_video_streamable=False,
                    source_index=index,
                )
                for index, path in enumerate(paths, start=1)
            ),
        )


class BatchDelivery:
    def __init__(self, fail_second: bool = False, fail_all: bool = False) -> None:
        self.fail_second = fail_second
        self.fail_all = fail_all
        self.batch_calls = 0
        self.edits: list[str] = []
        self.source_urls: list[str | None] = []
        self.captions: list[str] = []
        self.story_modes: list[StoryDeliveryMode | None] = []

    async def deliver_batch(self, **kwargs: object) -> object:
        from telegram_media_bot.application.ports.delivery import BatchDeliveryOutcome
        from telegram_media_bot.domain.models import DeliveryItemReceipt, DeliveryProvider

        self.batch_calls += 1
        result = cast(DownloadResult, kwargs["result"])
        self.source_urls.append(cast(str | None, kwargs.get("source_url")))
        self.captions.append(cast(str, kwargs["caption"]))
        self.story_modes.append(result.story_delivery_mode)
        total = len(result.delivery_artifacts)
        if self.fail_all:
            return BatchDeliveryOutcome(total=total, succeeded=0, failed=total, receipts=())
        succeeded = total - (1 if self.fail_second and total > 1 else 0)
        receipts = tuple(
            DeliveryItemReceipt(
                method=DeliveryMethod.PHOTO,
                message_id=index,
                file_id=f"f{index}",
                file_unique_id=f"u{index}",
                provider=DeliveryProvider.BOT_API,
                ordinal=index,
                caption=cast(str, kwargs["caption"]),
            )
            for index in range(1, succeeded + 1)
        )
        item_delivered = kwargs.get("item_delivered")
        if callable(item_delivered):
            for item in receipts:
                await item_delivered(item)
        return BatchDeliveryOutcome(
            total=total,
            succeeded=succeeded,
            failed=total - succeeded,
            receipts=receipts,
            delivered_bytes=succeeded * 5,
        )

    async def deliver(self, **kwargs: object) -> DeliveryReceipt:
        return DeliveryReceipt(DeliveryMethod.VIDEO, 3, "file-id", "unique-id")

    async def send_text(self, _chat_id: int, _text: str) -> int:
        return 4

    async def edit_text(self, _chat_id: int, _message_id: int, text: str) -> None:
        self.edits.append(text)
        return None


class CapturingQueue:
    def __init__(self) -> None:
        self.downloads: list[dict[str, object]] = []

    async def enqueue_download(self, **kwargs: object) -> JobId:
        self.downloads.append(kwargs)
        return cast(JobId, kwargs["job_id"])

    async def enqueue_highlight_tray(self, **kwargs: object) -> JobId:
        return cast(JobId, kwargs["job_id"])


def _settings(settings: Settings, tmp_path: Path, **changes: object) -> Settings:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    for key, value in changes.items():
        head, _, tail = key.partition(".")
        if head not in raw:
            raw[key] = value
        else:
            current = raw[head]
            current[tail] = value
    return Settings.model_validate(raw)


def _story_context(
    settings: Settings, tmp_path: Path, service: StoryInspectionService
) -> dict[str, Any]:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    inspection, _ = JobService(repository).create_inspection(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/3964254748584813861/?igsh=share",
    )
    repository.set_status_message(inspection.job_id, 30)
    return {
        "settings": configured,
        "repository": repository,
        "download_service": service,
        "bot": FakeBot(),
        "metrics": MetricsRegistry(),
        "job_id": str(inspection.job_id),
        "job_try": 1,
    }


async def test_story_inspection_publishes_single_and_all_buttons(
    settings: Settings, tmp_path: Path
) -> None:
    context = _story_context(settings, tmp_path, StoryInspectionService())
    await process_inspection_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/3964254748584813861/?igsh=share",
    )
    keyboard = cast(InlineKeyboardMarkup, context["bot"].edits[0]["reply_markup"])
    callbacks = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert callbacks[0].endswith(":single")
    assert callbacks[1].endswith(":all")
    persisted = context["repository"].get_job(JobId(str(context["job_id"])))
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED
    # The exact media id is preserved for the single choice.
    assert "3964254748584813861" in str(
        context["bot"].edits[0]["text"]
    ) or "3964254748584813861" in str(persisted.url)


async def test_story_image_still_offers_single_via_photo_file_flow(
    settings: Settings, tmp_path: Path
) -> None:
    context = _story_context(settings, tmp_path, StoryImageInspectionService())
    await process_inspection_job(
        context,
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/3964254748584813861/",
    )
    keyboard = cast(InlineKeyboardMarkup, context["bot"].edits[0]["reply_markup"])
    callbacks = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert callbacks[0].endswith(":single")
    assert callbacks[1].endswith(":all")


@pytest.mark.parametrize("service", [StoryInspectionService(), StoryImageInspectionService()])
async def test_direct_highlight_inspection_publishes_durable_delivery_choice(
    settings: Settings, tmp_path: Path, service: StoryInspectionService
) -> None:
    context = _story_context(settings, tmp_path, service)
    repository = context["repository"]
    url = "https://www.instagram.com/stories/highlights/18190966201404535/"
    job, _ = JobService(repository).create_inspection(chat_id=10, user_id=20, url=url)
    repository.set_status_message(job.job_id, 31)
    context["job_id"] = str(job.job_id)
    context["queue"] = CapturingQueue()
    await process_inspection_job(context, chat_id=10, user_id=20, url=url)
    keyboard = cast(InlineKeyboardMarkup, context["bot"].edits[0]["reply_markup"])
    callbacks = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert callbacks[0].startswith("h3:") and callbacks[0].endswith(":normal")
    assert callbacks[1].startswith("h3:") and callbacks[1].endswith(":file")
    assert all(len(value.encode()) <= 64 for value in callbacks)
    reopened = SqliteJobRepository(context["settings"].database_path())
    selection = reopened.get_selection(SelectionToken(callbacks[0].split(":")[1]), 20)
    assert selection.media.webpage_url == url
    assert selection.allowed_modes == (DownloadMode.INSTAGRAM_HIGHLIGHT,)
    assert selection.source_job_id == job.job_id
    completed = reopened.get_job(job.job_id)
    assert completed is not None and completed.status is JobStatus.SUCCEEDED
    assert context["queue"].downloads == []


@pytest.mark.parametrize(
    "mode,delivery_mode",
    [
        (DownloadMode.INSTAGRAM_ALL_STORIES, StoryDeliveryMode.NORMAL),
        (DownloadMode.INSTAGRAM_HIGHLIGHT, StoryDeliveryMode.FILE),
    ],
)
async def test_batch_download_partial_failure_keeps_successes_and_summarizes(
    settings: Settings, tmp_path: Path, mode: DownloadMode, delivery_mode: StoryDeliveryMode
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/highlights/222/"
        if mode is DownloadMode.INSTAGRAM_HIGHLIGHT
        else "https://www.instagram.com/stories/exampleuser/",
        mode=mode,
        story_delivery_mode=delivery_mode,
    )
    repository.set_status_message(record.job_id, 30)
    paths = [tmp_path / "1.jpg", tmp_path / "2.jpg", tmp_path / "3.jpg"]
    for path in paths:
        path.write_bytes(b"media")
    delivery = BatchDelivery(fail_second=True)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": SqliteJobRepository(configured.database_path()),
        "download_service": BatchDownloadService(artifacts=tuple(paths)),
        "bot": FakeBot(),
        "delivery": delivery,
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=mode.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED
    assert delivery.batch_calls == 1
    assert delivery.source_urls == [record.url]
    assert delivery.story_modes == [delivery_mode]
    assert "@telegram_media_bot" in delivery.captions[0]
    items = repository.delivery_items(record.job_id)
    # Only successful items are persisted; the failed item is isolated.
    assert len(items) == 2


async def test_cookie_remediated_story_file_job_keeps_durable_source_url(
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/?igsh=tracking",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
        story_delivery_mode=StoryDeliveryMode.FILE,
    )
    repository.transition(
        record.job_id,
        JobStatus.FAILED,
        error_category=ErrorCategory.AUTHENTICATION,
        error_summary="AuthenticationRequiredError",
    )
    repository.record_recoverable_failure(
        record.job_id,
        ErrorCategory.AUTHENTICATION,
        "1.3.7",
    )
    recovered = repository.mark_recovery_requeued(
        record.job_id,
        version="1.3.8",
        now=datetime.now(UTC),
    )
    assert recovered is not None
    path = tmp_path / "story.jpg"
    path.write_bytes(b"media")
    delivery = BatchDelivery()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": BatchDownloadService(artifacts=(path,)),
        "bot": FakeBot(),
        "delivery": delivery,
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
        story_delivery_mode=StoryDeliveryMode.FILE.value,
    )

    canonical = "https://www.instagram.com/stories/exampleuser/"
    assert record.url == canonical
    assert delivery.source_urls == [canonical]
    assert delivery.story_modes == [StoryDeliveryMode.FILE]
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED


async def test_highlight_delivery_keeps_durable_highlight_url(
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/highlights/123/?igsh=tracking",
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
    )
    path = tmp_path / "highlight.jpg"
    path.write_bytes(b"media")
    delivery = BatchDelivery()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": BatchDownloadService(artifacts=(path,)),
        "bot": FakeBot(),
        "delivery": delivery,
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }

    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT.value,
    )

    assert delivery.source_urls == ["https://www.instagram.com/stories/highlights/123/"]
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED


async def test_batch_download_all_failed_is_terminal_failure(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/highlights/123/",
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
    )
    repository.set_status_message(record.job_id, 30)
    paths = [tmp_path / "1.jpg"]
    paths[0].write_bytes(b"media")
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": BatchDownloadService(artifacts=tuple(paths)),
        "bot": FakeBot(),
        "delivery": BatchDelivery(fail_all=True),
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.FAILED
    assert persisted.error_category is ErrorCategory.DELIVERY


async def test_highlight_tray_job_publishes_browser(settings: Settings, tmp_path: Path) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    from telegram_media_bot.domain.models import HighlightItem

    items = (
        HighlightItem("111", "\u0633\u0641\u0631", 2),
        HighlightItem("222", "\u0632\u0646\u062f\u06af\u06cc", 1),
    )
    tray_record, _ = JobService(repository).create_highlight_tray(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/exampleuser/highlights/",
        username="exampleuser",
    )
    repository.set_status_message(tray_record.job_id, 30)
    bot = FakeBot()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "gallery_engine": TrayFakeEngine(items=items),
        "bot": bot,
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "job_id": str(tray_record.job_id),
        "job_try": 1,
    }
    await process_highlight_tray_job(
        context,
        chat_id=10,
        user_id=20,
        url=tray_record.url,
        username="exampleuser",
    )
    persisted = repository.get_job(tray_record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED
    assert len(bot.edits) == 1
    text = str(bot.edits[0]["text"])
    assert "\u0633\u0641\u0631" in text
    keyboard = cast(InlineKeyboardMarkup, bot.edits[0]["reply_markup"])
    callbacks = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert any(callback.startswith("h2:") and ":pick:111" in callback for callback in callbacks)


async def test_highlight_tray_empty_is_unavailable(settings: Settings, tmp_path: Path) -> None:

    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    tray_record, _ = JobService(repository).create_highlight_tray(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/exampleuser/highlights/",
        username="exampleuser",
    )
    repository.set_status_message(tray_record.job_id, 30)
    bot = FakeBot()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "gallery_engine": TrayFakeEngine(items=()),
        "bot": bot,
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "job_id": str(tray_record.job_id),
        "job_try": 1,
    }
    await process_highlight_tray_job(
        context,
        chat_id=10,
        user_id=20,
        url=tray_record.url,
        username="exampleuser",
    )
    persisted = repository.get_job(tray_record.job_id)
    assert persisted is not None and persisted.status is JobStatus.FAILED
    assert persisted.error_category is ErrorCategory.MEDIA_UNAVAILABLE


async def test_highlight_tray_cancelled_during_fetch_never_publishes(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_highlight_tray(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/exampleuser/highlights/",
        username="exampleuser",
    )
    repository.set_status_message(record.job_id, 30)
    bot = FakeBot()

    class CancelDuringFetch(TrayFakeEngine):
        def fetch_highlight_tray(
            self, username: str, **kwargs: object
        ) -> tuple[HighlightItem, ...]:
            repository.request_cancel(record.job_id, record.user_id)
            return (HighlightItem("111", "Trip", 2),)

    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "gallery_engine": CancelDuringFetch(),
        "bot": bot,
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_highlight_tray_job(
        context, chat_id=10, user_id=20, url=record.url, username="exampleuser"
    )

    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.CANCELLED
    assert bot.messages == bot.edits == []


class ConnectedStoryEngine(StoryInspectionService):
    def __init__(self) -> None:
        self.credentials: list[ResolvedCredential] = []
        self.public = False

    def _observe(self, credential: ResolvedCredential | None) -> None:
        assert credential is not None
        if self.public:
            assert credential.context.kind is CredentialKind.USER_INSTAGRAM
            assert credential.context.policy is CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK
            assert credential.materialized_cookie_path is not None
            assert Path(credential.materialized_cookie_path).read_bytes() == b"story-owner-session"
        else:
            assert credential.context.kind is CredentialKind.USER_INSTAGRAM
            assert credential.context.user_generation == 1
            assert credential.context.policy is CredentialPolicy.USER_ONLY
            assert credential.materialized_cookie_path is not None
            assert Path(credential.materialized_cookie_path).read_bytes() == b"story-owner-session"
        self.credentials.append(credential)

    def inspect(self, url: str, *, credential: object | None = None) -> MediaInfo:
        self._observe(cast(ResolvedCredential | None, credential))
        return super().inspect(url, credential=credential)

    def fetch_highlight_tray(self, username: str, **kwargs: Any) -> tuple[HighlightItem, ...]:
        self._observe(kwargs.get("credential"))
        assert username == "exampleuser"
        return (HighlightItem("111", "Trip", 2),)

    def download(self, request: DownloadRequest, **kwargs: Any) -> DownloadResult:
        self._observe(kwargs.get("credential"))
        request.output_directory.mkdir(parents=True, exist_ok=True)
        paths = (request.output_directory / "1.jpg", request.output_directory / "2.jpg")
        for path in paths:
            path.write_bytes(b"media")
        return BatchDownloadService(paths).download(job_id=request.job_id)


def _connected_context(
    settings: Settings, tmp_path: Path, *, public: bool = False
) -> dict[str, Any]:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    credentials = SqliteInstagramCredentialRepository(configured.database_path())
    credentials.initialize()
    ring = VaultKeyRing.from_hex_material(active_key_id="test", active_key="42" * 32)
    cryptor = CredentialCryptor(ring)
    vault = CredentialVault(credentials, cryptor)
    vault.store_session(20, b"story-owner-session")
    vault.store_session(99, b"wrong-owner-session")
    engine = ConnectedStoryEngine()
    engine.public = public
    accepted = datetime.now(UTC) - timedelta(days=2)
    return {
        "settings": configured,
        "repository": repository,
        "gallery_engine": engine,
        "download_service": DownloadService(engine, frozenset({"instagram"})),
        "credential_resolver": CredentialResolver(
            vault, RestrictedCookieMaterializer(credentials, cryptor)
        ),
        "credential_context": ResolvedCredential.operator_public(),
        "snapshot": EntitlementSnapshot(
            capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
            accepted_at=accepted,
            authorized_until=accepted + timedelta(days=1),
            plan_id=PlanId("vip"),
            grant_id=GrantId("grant"),
            owner_user_id=20,
        ),
        "engine": engine,
        "vault": vault,
        "bot": FakeBot(),
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "job_try": 1,
    }


def _authorization(context: dict[str, Any]) -> dict[str, Any]:
    if context["engine"].public:
        return {}
    return {
        "entitlement_snapshot": context["snapshot"],
        "credential_policy": CredentialPolicy.USER_ONLY,
        "content_access_scope": ContentAccessScope.USER_RESTRICTED,
        "user_credential_generation": 1,
    }


@pytest.mark.parametrize("public", [False, True])
async def test_story_choice_binds_source_job_and_collection_preserves_access(
    settings: Settings, tmp_path: Path, public: bool
) -> None:
    context = _connected_context(settings, tmp_path, public=public)
    service = JobService(context["repository"])
    parent, _ = service.create_inspection(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/3964254748584813861/",
        **_authorization(context),
    )
    context["job_id"] = str(parent.job_id)
    await process_inspection_job(context, chat_id=10, user_id=20, url=parent.url)
    keyboard = context["bot"].messages[0]["reply_markup"]
    token = keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    selection = context["repository"].get_selection(SelectionToken(token), 20)
    assert selection.source_job_id == parent.job_id
    child, _ = service.create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
        **_authorization(context),
    )
    context["job_id"] = str(child.job_id)
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=child.url,
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
    )
    assert context["repository"].get_job(child.job_id).status is JobStatus.SUCCEEDED
    assert context["delivery"].batch_calls == 1
    assert all(
        credential.materialized_cookie_path is None
        or not Path(credential.materialized_cookie_path).parent.exists()
        for credential in context["engine"].credentials
    )


@pytest.mark.parametrize("public", [False, True])
async def test_highlight_tray_source_and_selected_download_preserve_owner_boundary(
    settings: Settings, tmp_path: Path, public: bool
) -> None:
    context = _connected_context(settings, tmp_path, public=public)
    service = JobService(context["repository"])
    parent, _ = service.create_highlight_tray(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/exampleuser/highlights/",
        username="exampleuser",
        **_authorization(context),
    )
    context["job_id"] = str(parent.job_id)
    await process_highlight_tray_job(
        context, chat_id=10, user_id=20, url=parent.url, username="exampleuser"
    )
    keyboard = context["bot"].messages[0]["reply_markup"]
    token = keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    tray = context["repository"].get_highlight_tray(SelectionToken(token), 20)
    assert tray.source_job_id == parent.job_id
    assert tray.owner_user_id == 20
    child, _ = service.create_download(
        chat_id=10,
        user_id=20,
        url=f"https://www.instagram.com/stories/highlights/{tray.highlights[0].highlight_id}/",
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
        **_authorization(context),
    )
    context["job_id"] = str(child.job_id)
    await process_download_job(
        context, chat_id=10, user_id=20, url=child.url, mode=DownloadMode.INSTAGRAM_HIGHLIGHT.value
    )
    assert context["repository"].get_job(child.job_id).status is JobStatus.SUCCEEDED
    assert context["delivery"].batch_calls == 1
    assert all(
        credential.materialized_cookie_path is None
        or not Path(credential.materialized_cookie_path).parent.exists()
        for credential in context["engine"].credentials
    )


@pytest.mark.parametrize("forgery", ["owner", "target"])
async def test_private_highlight_tray_rejects_forged_queue_owner_or_target(
    settings: Settings, tmp_path: Path, forgery: str
) -> None:
    context = _connected_context(settings, tmp_path)
    record, _ = JobService(context["repository"]).create_highlight_tray(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/exampleuser/highlights/",
        username="exampleuser",
        **_authorization(context),
    )
    context["job_id"] = str(record.job_id)
    await process_highlight_tray_job(
        context,
        chat_id=10,
        user_id=99 if forgery == "owner" else 20,
        url=record.url,
        username="wronguser" if forgery == "target" else "exampleuser",
    )
    assert context["repository"].get_job(record.job_id).status is JobStatus.FAILED
    assert context["engine"].credentials == []
    assert context["bot"].messages == []
