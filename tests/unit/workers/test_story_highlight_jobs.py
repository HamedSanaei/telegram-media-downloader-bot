from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from aiogram.types import InlineKeyboardMarkup

from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.cookie_health import CookieHealthState, StaticCookieCheck
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryMethod,
    DeliveryReceipt,
    DownloadArtifact,
    DownloadMode,
    DownloadResult,
    ErrorCategory,
    HighlightItem,
    JobId,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_cookie_health import (
    SqliteCookieHealthRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.telegram.texts import (
    INSTAGRAM_COOKIES_BLOCKED_TEXT,
)
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
    def inspect(self, url: str) -> MediaInfo:
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
    def inspect(self, url: str) -> MediaInfo:
        info = super().inspect(url)
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

    async def deliver_batch(self, **kwargs: object) -> object:
        from telegram_media_bot.application.ports.delivery import BatchDeliveryOutcome
        from telegram_media_bot.domain.models import DeliveryItemReceipt, DeliveryProvider

        self.batch_calls += 1
        result = cast(DownloadResult, kwargs["result"])
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


async def test_collection_cookie_gating_fails_early_when_instagram_blocked(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    store = SqliteCookieHealthRepository(configured.database_path())
    store.initialize()

    class BlockedChecker:
        def check(self, provider: CookieService, **kwargs: object) -> StaticCookieCheck:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.EXPIRED,
                file_ok=True,
                safe_reason="cookies expired",
            )

    health = CookieHealthService(
        store=store,
        checker=BlockedChecker(),
    )
    health.refresh_static()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
    )
    repository.set_status_message(record.job_id, 30)
    bot = FakeBot()
    delivery = BatchDelivery()
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": BatchDownloadService(artifacts=()),
        "bot": bot,
        "delivery": delivery,
        "metrics": MetricsRegistry(),
        "cookie_health_service": health,
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.FAILED
    assert persisted.error_category is ErrorCategory.AUTHENTICATION
    # The user-facing text explains the cookie problem instead of running a doomed job.
    assert any(INSTAGRAM_COOKIES_BLOCKED_TEXT in text for text in delivery.edits)
    assert delivery.batch_calls == 0


async def test_collection_unverified_status_does_not_block(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    store = SqliteCookieHealthRepository(configured.database_path())
    store.initialize()

    class UnverifiedChecker:
        def check(self, provider: CookieService, **kwargs: object) -> StaticCookieCheck:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.UNVERIFIED,
                file_ok=True,
            )

    health = CookieHealthService(store=store, checker=UnverifiedChecker())
    health.refresh_static()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
    )
    repository.set_status_message(record.job_id, 30)
    paths = [tmp_path / "1.jpg", tmp_path / "2.jpg"]
    for path in paths:
        path.write_bytes(b"media")
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": BatchDownloadService(artifacts=tuple(paths)),
        "bot": FakeBot(),
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "cookie_health_service": health,
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED


async def test_batch_download_partial_failure_keeps_successes_and_summarizes(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
    )
    repository.set_status_message(record.job_id, 30)
    paths = [tmp_path / "1.jpg", tmp_path / "2.jpg", tmp_path / "3.jpg"]
    for path in paths:
        path.write_bytes(b"media")
    delivery = BatchDelivery(fail_second=True)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
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
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.SUCCEEDED
    assert delivery.batch_calls == 1
    items = repository.delivery_items(record.job_id)
    # Only successful items are persisted; the failed item is isolated.
    assert len(items) == 2


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


async def test_runtime_auth_failure_updates_instagram_cookie_health(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings(settings, tmp_path)
    configured.create_runtime_directories()
    raw = configured.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    repository = SqliteJobRepository(configured.database_path())
    repository.initialize()
    store = SqliteCookieHealthRepository(configured.database_path())
    store.initialize()
    health = CookieHealthService(
        store=store,
        checker=SimpleNamespace(
            check=lambda provider, **kwargs: StaticCookieCheck(
                provider=provider, status=CookieHealthState.HEALTHY, file_ok=True
            )
        ),
    )
    record, _ = JobService(repository).create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
    )
    repository.set_status_message(record.job_id, 30)
    paths = [tmp_path / "1.jpg"]
    paths[0].write_bytes(b"media")
    download_service = BatchDownloadService(artifacts=tuple(paths), fail=True)
    context: dict[str, Any] = {
        "settings": configured,
        "repository": repository,
        "download_service": download_service,
        "bot": FakeBot(),
        "delivery": BatchDelivery(),
        "metrics": MetricsRegistry(),
        "cookie_health_service": health,
        "job_id": str(record.job_id),
        "job_try": 1,
    }
    await process_download_job(
        context,
        chat_id=10,
        user_id=20,
        url=record.url,
        mode=DownloadMode.INSTAGRAM_ALL_STORIES.value,
    )
    persisted = repository.get_job(record.job_id)
    assert persisted is not None and persisted.status is JobStatus.FAILED
    health_row = store.load(CookieService.INSTAGRAM)
    assert health_row is not None
    assert health_row.status is CookieHealthState.AUTH_FAILED
    assert download_service.calls == 1


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
