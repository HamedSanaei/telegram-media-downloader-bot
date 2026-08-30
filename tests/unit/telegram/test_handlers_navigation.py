from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import SelectionExpiredError
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ImageDeliveryMode,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    NativeVideoCodec,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
    SizeConfidence,
)
from telegram_media_bot.telegram.handlers import build_router


class FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=10, type="private")
        self.message_id = 30
        self.edits: list[tuple[str, object | None]] = []

    async def edit_text(self, text: str, reply_markup: object | None = None) -> None:
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(
            id=20,
            username="user",
            first_name="User",
            last_name=None,
            language_code="fa",
            is_premium=False,
        )
        self.message = FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeRepository:
    def __init__(self, selection: SelectionRecord | Exception) -> None:
        self.selection = selection
        self.selection_reads = 0

    def get_selection(self, token: SelectionToken, owner_user_id: int) -> SelectionRecord:
        self.selection_reads += 1
        assert token == "opaque-token-123"
        assert owner_user_id == 20
        if isinstance(self.selection, Exception):
            raise self.selection
        return self.selection

    def set_status_message(self, _job_id: JobId, _message_id: int) -> None:
        return None


class FakeAccessPolicy:
    async def authorize_request(self, _user_id: int, **_kwargs: object) -> None:
        return None


class FakeJobs:
    def __init__(self) -> None:
        self.downloads = 0
        self.inspections = 0

    def create_download(self, **_kwargs: object) -> object:
        self.downloads += 1
        raise AssertionError("navigation must not create a download")

    def create_inspection(self, **_kwargs: object) -> object:
        self.inspections += 1
        raise AssertionError("navigation must not create an inspection")


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued = 0

    async def queue_depth(self) -> int:
        return 0

    async def enqueue_download(self, **_kwargs: object) -> object:
        self.enqueued += 1
        raise AssertionError("navigation must not enqueue a download")


class CapturingQueue(FakeQueue):
    async def enqueue_download(self, **kwargs: object) -> object:
        self.enqueued += 1
        self.kwargs = kwargs
        return kwargs["job_id"]


class FakeUsers:
    def upsert_user(self, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.mark.parametrize(
    ("data", "expected_text"),
    [
        ("n2:opaque-token-123:t", "نوع خروجی را انتخاب کنید"),
        ("n2:opaque-token-123:s", "لینک"),
    ],
)
async def test_back_reuses_selection_without_inspection_or_enqueue(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    data: str,
    expected_text: str,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    repository = FakeRepository(_selection())
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback(data)

    await _callback_handler(router, "navigate_selection")(callback)

    assert repository.selection_reads == 1
    assert jobs.inspections == 0
    assert jobs.downloads == 0
    assert queue.enqueued == 0
    assert expected_text in callback.message.edits[-1][0]


async def test_expired_back_callback_does_not_crash(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    repository = FakeRepository(SelectionExpiredError("expired"))
    router = build_router(
        settings=settings,
        queue=FakeQueue(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=FakeJobs(),  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback("n2:opaque-token-123:t")

    await _callback_handler(router, "navigate_selection")(callback)

    assert callback.answers[-1][1] is True
    assert callback.message.edits


@pytest.mark.parametrize(
    "data",
    [
        "container:opaque-token-123:mp4",
        "container:opaque-token-123:webm",
        "fmt:opaque-token-123:mp4:video_1080",
        "fmt:opaque-token-123:webm:video_1080",
        "fmt:opaque-token-123:mp4:explicit_transcode:video_2160",
    ],
)
async def test_legacy_callbacks_redirect_safely_without_job(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(_selection()),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback(data)

    await _callback_handler(router, "reject_legacy_selection")(callback)

    assert jobs.downloads == 0
    assert queue.enqueued == 0
    assert "قدیمی شده" in (callback.answers[-1][0] or "")
    assert "Native" in callback.message.edits[-1][0]


async def test_tampered_option_id_is_rejected_before_job_creation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(_selection()),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback("o2:opaque-token-123:aaaaaaaaaaaaaaaa")

    await _callback_handler(router, "choose_native_option")(callback)

    assert jobs.downloads == 0
    assert queue.enqueued == 0
    assert callback.answers[-1][1] is True


async def test_native_option_is_revalidated_before_enqueue(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    selection = _selection()
    option = build_native_option_catalog(selection.media).for_container(OutputContainer.MP4)[0]
    now = datetime.now(UTC)

    class CreatingJobs(FakeJobs):
        def create_download(self, **kwargs: object) -> tuple[JobRecord, bool]:
            self.downloads += 1
            return (
                JobRecord(
                    job_id=JobId("download-job"),
                    kind=JobKind.DOWNLOAD,
                    status=JobStatus.QUEUED,
                    chat_id=cast(int, kwargs["chat_id"]),
                    user_id=cast(int, kwargs["user_id"]),
                    url=str(kwargs["url"]),
                    mode=kwargs["mode"],  # type: ignore[arg-type]
                    container=kwargs["container"],  # type: ignore[arg-type]
                    container_policy=kwargs["container_policy"],  # type: ignore[arg-type]
                    native_video_codec=kwargs["native_video_codec"],  # type: ignore[arg-type]
                    selected_format_ids=kwargs["selected_format_ids"],  # type: ignore[arg-type]
                    idempotency_key="key",
                    created_at=now,
                    updated_at=now,
                ),
                True,
            )

    jobs = CreatingJobs()
    queue = CapturingQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(selection),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback(f"o2:opaque-token-123:{option.option_id}")

    await _callback_handler(router, "choose_native_option")(callback)

    assert jobs.downloads == 1
    assert queue.enqueued == 1
    assert queue.kwargs["container"] is OutputContainer.MP4
    assert queue.kwargs["container_policy"] is ContainerPolicy.GUARANTEED
    assert queue.kwargs["native_video_codec"] is NativeVideoCodec.AV1
    assert queue.kwargs["selected_format_ids"] == option.selected_format_ids


async def test_instagram_document_choice_is_persisted_and_enqueued(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    now = datetime.now(UTC)

    class CreatingJobs(FakeJobs):
        def create_download(self, **kwargs: object) -> tuple[JobRecord, bool]:
            self.downloads += 1
            self.kwargs = kwargs
            return (
                JobRecord(
                    JobId("instagram-job"),
                    JobKind.DOWNLOAD,
                    JobStatus.QUEUED,
                    cast(int, kwargs["chat_id"]),
                    cast(int, kwargs["user_id"]),
                    str(kwargs["url"]),
                    cast(DownloadMode, kwargs["mode"]),
                    "key",
                    now,
                    now,
                    selected_format_ids=cast(tuple[str, ...], kwargs["selected_format_ids"]),
                    image_delivery_mode=cast(ImageDeliveryMode, kwargs["image_delivery_mode"]),
                ),
                True,
            )

    jobs = CreatingJobs()
    queue = CapturingQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(_instagram_selection()),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback("i2:opaque-token-123:document")

    await _callback_handler(router, "choose_instagram_image_delivery")(callback)

    assert jobs.downloads == 1
    assert jobs.kwargs["mode"] is DownloadMode.ALL_ORIGINAL_MEDIA
    assert jobs.kwargs["image_delivery_mode"] is ImageDeliveryMode.DOCUMENT
    assert queue.kwargs["image_delivery_mode"] is ImageDeliveryMode.DOCUMENT
    assert queue.kwargs["selected_format_ids"] == ("image", "video")


@pytest.mark.parametrize(
    "data",
    ["i2:opaque-token-123:raw", "m2:opaque-token-123:all_original_media"],
)
async def test_instagram_choice_tampering_cannot_bypass_confirmation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(_instagram_selection()),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    callback = FakeCallback(data)
    handler = "choose_instagram_image_delivery" if data.startswith("i2:") else "choose_media_bundle"

    await _callback_handler(router, handler)(callback)

    assert jobs.downloads == 0
    assert queue.enqueued == 0
    assert callback.answers[-1][1] is True


def _callback_handler(router: object, name: str) -> Any:
    observer = router.observers["callback_query"]  # type: ignore[attr-defined]
    return next(item.callback for item in observer.handlers if item.callback.__name__ == name)


def _selection() -> SelectionRecord:
    now = datetime.now(UTC)
    return SelectionRecord(
        token=SelectionToken("opaque-token-123"),
        owner_user_id=20,
        chat_id=10,
        media=MediaInfo(
            media_id="id",
            title="Title",
            source="youtube",
            kind=MediaKind.VIDEO,
            webpage_url="https://example.test/video",
            format_options=(
                MediaFormatOption(
                    mode=DownloadMode.VIDEO_1080,
                    container=OutputContainer.MP4,
                    container_policy=ContainerPolicy.GUARANTEED,
                    width=1920,
                    height=1080,
                    fps=30,
                    size_bytes=10_000,
                    size_confidence=SizeConfidence.EXACT,
                    selected_format_ids=("137", "140"),
                    video_codec="avc1.640028",
                    audio_codec="mp4a.40.2",
                    dynamic_range="SDR",
                ),
                MediaFormatOption(
                    mode=DownloadMode.VIDEO_1080,
                    container=OutputContainer.WEBM,
                    container_policy=ContainerPolicy.GUARANTEED,
                    width=1920,
                    height=1080,
                    fps=30,
                    size_bytes=9_000,
                    size_confidence=SizeConfidence.ESTIMATED,
                    selected_format_ids=("248", "251"),
                    video_codec="vp9",
                    audio_codec="opus",
                    dynamic_range="SDR",
                ),
                MediaFormatOption(
                    mode=DownloadMode.VIDEO_2160,
                    container=OutputContainer.MP4,
                    container_policy=ContainerPolicy.GUARANTEED,
                    requires_transcode=False,
                    width=3840,
                    height=2160,
                    selected_format_ids=("399", "140"),
                    video_codec="av01.0.12M.08",
                    audio_codec="mp4a.40.2",
                ),
            ),
        ),
        allowed_modes=(DownloadMode.VIDEO_2160, DownloadMode.VIDEO_1080),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def _instagram_selection() -> SelectionRecord:
    now = datetime.now(UTC)
    assets = (
        MediaAsset(1, "image", MediaKind.IMAGE, "jpg", "image/jpeg", "post", "instagram"),
        MediaAsset(2, "video", MediaKind.VIDEO, "mp4", "video/mp4", "post", "instagram"),
    )
    return SelectionRecord(
        SelectionToken("opaque-token-123"),
        20,
        10,
        MediaInfo(
            "post",
            "Mixed",
            "instagram",
            MediaKind.PLAYLIST,
            "https://www.instagram.com/p/post/",
            format_options=(
                MediaFormatOption(
                    DownloadMode.ALL_ORIGINAL_MEDIA,
                    selected_format_ids=("image", "video"),
                ),
            ),
            assets=assets,
        ),
        (DownloadMode.ALL_ORIGINAL_MEDIA,),
        now,
        now + timedelta(minutes=10),
    )
