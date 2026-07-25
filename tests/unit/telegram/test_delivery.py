import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendDocument, SendVideo
from aiogram.types import Audio, Chat, Document, FSInputFile, InputFile, Message, MessageId, Video

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DeliveryError, DeliveryTooLargeError
from telegram_media_bot.domain.models import (
    DeliveryProgressEvent,
    DeliveryProvider,
    DeliveryStage,
    DownloadArtifact,
    DownloadResult,
    JobId,
    MediaKind,
)
from telegram_media_bot.infrastructure.archive.multipart_zip import MultipartArchive
from telegram_media_bot.telegram.delivery import (
    RoutedDeliveryGateway,
    TelegramDeliveryGateway,
    TrackedFSInputFile,
    _finalization_heartbeat,
    render_caption,
)


class FakeBot:
    fail_video = False
    fail_video_network = False

    def __init__(self) -> None:
        self.last_upload: dict[str, object] = {}
        self.uploads: list[dict[str, object]] = []

    async def send_audio(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        self.uploads.append(kwargs)
        await self._consume(kwargs.get("audio"))
        return _message("audio")

    async def send_video(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        self.uploads.append(kwargs)
        if self.fail_video:
            raise TelegramBadRequest(
                method=SendVideo(chat_id=1, video="existing-file-id"), message="unsupported"
            )
        if self.fail_video_network:
            raise TelegramNetworkError(
                method=SendVideo(chat_id=1, video="existing-file-id"),
                message="connection lost",
            )
        await self._consume(kwargs.get("video"))
        return _message("video")

    async def send_document(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        self.uploads.append(kwargs)
        await self._consume(kwargs.get("document"))
        return _message("document")

    async def send_message(self, **_kwargs: object) -> Message:
        return _message("none")

    async def edit_message_text(self, **_kwargs: object) -> Message:
        return _message("none")

    async def copy_message(self, **_kwargs: object) -> MessageId:
        return MessageId(message_id=77)

    async def _consume(self, upload: object) -> None:
        if isinstance(upload, InputFile):
            async for _chunk in upload.read(cast(Bot, cast(Any, self))):
                pass


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(MediaKind.AUDIO, "audio"), (MediaKind.VIDEO, "video"), (MediaKind.IMAGE, "document")],
)
async def test_delivery_selects_normalized_media_method(
    settings: Settings, tmp_path: Path, kind: MediaKind, expected: str
) -> None:
    configured = _auto_delivery(settings)
    bot = FakeBot()
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), configured)
    result = _result(tmp_path, kind)
    receipt = await gateway.deliver(
        chat_id=1, result=result, caption=render_caption(configured, result)
    )
    assert receipt.method.value == expected
    assert receipt.file_id == "file-id"
    assert bot.last_upload["request_timeout"] == configured.telegram.upload_timeout_seconds


async def test_video_failure_falls_back_to_document(settings: Settings, tmp_path: Path) -> None:
    bot = FakeBot()
    bot.fail_video = True
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), _auto_delivery(settings))
    receipt = await gateway.deliver(
        chat_id=1, result=_result(tmp_path, MediaKind.VIDEO), caption="caption"
    )
    assert receipt.method.value == "document"


async def test_webm_is_always_sent_as_document(settings: Settings, tmp_path: Path) -> None:
    configured = _auto_delivery(settings)
    path = tmp_path / "video.webm"
    path.write_bytes(b"webm")
    result = DownloadResult(
        job_id=JobId("webm"),
        media_id="webm",
        title="WebM",
        source="youtube",
        kind=MediaKind.VIDEO,
        file_path=path,
        file_size_bytes=4,
        mime_type="video/webm",
    )
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, FakeBot())), configured)

    receipt = await gateway.deliver(chat_id=1, result=result, caption="caption")

    assert receipt.method.value == "document"


async def test_multiple_video_artifacts_are_delivered_separately(
    settings: Settings,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    result = DownloadResult(
        job_id=JobId("instagram"),
        media_id="collection",
        title="Collection",
        source="instagram",
        kind=MediaKind.PLAYLIST,
        file_path=first,
        file_size_bytes=6,
        artifacts=(
            DownloadArtifact(first, 3, MediaKind.VIDEO, "video/mp4", "One"),
            DownloadArtifact(second, 3, MediaKind.VIDEO, "video/mp4", "Two"),
        ),
    )
    bot = FakeBot()
    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings)

    receipt = await gateway.deliver(chat_id=1, result=result, caption="caption")

    assert len(receipt.items) == 2
    assert [item.ordinal for item in receipt.items] == [1, 2]
    assert len(bot.uploads) == 2


def test_caption_contains_runtime_bot_username(settings: Settings, tmp_path: Path) -> None:
    caption = render_caption(settings, _result(tmp_path, MediaKind.VIDEO), "ExampleBot")
    assert "@ExampleBot" in caption


async def test_ambiguous_network_failure_never_falls_back_or_retries(
    settings: Settings, tmp_path: Path
) -> None:
    bot = FakeBot()
    bot.fail_video_network = True
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), _auto_delivery(settings))

    with pytest.raises(DeliveryError):
        await gateway.deliver(
            chat_id=1,
            result=_result(tmp_path, MediaKind.VIDEO),
            caption="caption",
        )

    assert "video" in bot.last_upload
    assert "document" not in bot.last_upload


async def test_delivery_rejects_oversize_before_api_call(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["max_upload_size_mb"] = 1
    configured = Settings.model_validate(raw)
    result = _result(tmp_path, MediaKind.VIDEO)
    result = DownloadResult(
        job_id=result.job_id,
        media_id=result.media_id,
        title=result.title,
        source=result.source,
        kind=result.kind,
        file_path=result.file_path,
        file_size_bytes=2 * 1024 * 1024,
    )
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, FakeBot())), configured)
    with pytest.raises(DeliveryTooLargeError):
        await gateway.deliver(chat_id=1, result=result, caption="caption")


async def test_local_api_accepts_declared_file_over_200_mb_without_recompression(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    configured = Settings.model_validate(raw)
    result = _result(tmp_path, MediaKind.VIDEO)
    large_result = DownloadResult(
        job_id=result.job_id,
        media_id=result.media_id,
        title=result.title,
        source=result.source,
        kind=result.kind,
        file_path=result.file_path,
        file_size_bytes=201 * 1024 * 1024,
    )
    bot = FakeBot()
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), configured)

    await gateway.deliver(chat_id=1, result=large_result, caption="caption")

    assert isinstance(bot.last_upload["document"], FSInputFile)


async def test_tracked_input_file_reports_bytes_without_exposing_path(tmp_path: Path) -> None:
    source = tmp_path / "private-name.bin"
    source.write_bytes(b"abcdefghij")
    transferred: list[int] = []
    finished: list[int] = []
    upload = TrackedFSInputFile(
        source,
        filename="safe.bin",
        chunk_size=4,
        on_progress=transferred.append,
        on_finished=finished.append,
    )

    chunks = [chunk async for chunk in upload.read(cast(Bot, cast(Any, FakeBot())))]

    assert b"".join(chunks) == b"abcdefghij"
    assert transferred == [4, 8, 10]
    assert finished == [10]


async def test_finalization_heartbeat_reports_elapsed_wait_without_fake_bytes() -> None:
    stream_finished = asyncio.Event()
    request_finished = asyncio.Event()
    heartbeats: list[None] = []
    task = asyncio.create_task(
        _finalization_heartbeat(
            stream_finished,
            request_finished,
            interval_seconds=0.01,
            emit=lambda: heartbeats.append(None),
        )
    )

    stream_finished.set()
    await asyncio.sleep(0.035)
    request_finished.set()
    await task

    assert len(heartbeats) >= 2


async def test_text_delivery_helpers(settings: Settings) -> None:
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, FakeBot())), settings)
    assert await gateway.send_text(1, "text") == 1
    await gateway.edit_text(1, 1, "updated")


async def test_routed_delivery_sends_exact_direct_limit_without_partitioning(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["media"]["max_file_size_mb"] = 4096
    raw["media"]["max_source_size_mb"] = 4096
    configured = Settings.model_validate(raw)
    result = _result(tmp_path, MediaKind.VIDEO)
    declared_large = DownloadResult(
        job_id=result.job_id,
        media_id=result.media_id,
        title=result.title,
        source=result.source,
        kind=result.kind,
        file_path=result.file_path,
        file_size_bytes=1900 * 1024 * 1024,
    )
    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, FakeBot())), configured)

    receipt = await gateway.deliver(chat_id=1, result=declared_large, caption="caption")

    assert receipt.primary.provider is DeliveryProvider.BOT_API


async def test_routed_delivery_sends_multipart_immediately_above_direct_limit(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["media"]["max_file_size_mb"] = 4096
    raw["media"]["max_source_size_mb"] = 4096
    configured = Settings.model_validate(raw)
    result = _result(tmp_path, MediaKind.VIDEO)
    declared_large = DownloadResult(
        job_id=result.job_id,
        media_id=result.media_id,
        title=result.title,
        source=result.source,
        kind=result.kind,
        file_path=result.file_path,
        file_size_bytes=1900 * 1024 * 1024 + 1,
    )
    volume_one = tmp_path / "result.mp4.zip.001"
    volume_two = tmp_path / "result.mp4.zip.002"
    manifest = tmp_path / "result.mp4.manifest.json"
    for path in (volume_one, volume_two, manifest):
        path.write_bytes(b"part")

    class FakeBuilder:
        def build(self, _source: Path) -> MultipartArchive:
            return MultipartArchive((volume_one, volume_two), manifest)

    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, FakeBot())), configured)
    gateway._multipart = cast(Any, FakeBuilder())

    persisted_ordinals: list[int] = []
    progress: list[DeliveryProgressEvent] = []

    async def item_delivered(item: object) -> None:
        persisted_ordinals.append(cast(Any, item).ordinal)

    receipt = await gateway.deliver(
        chat_id=1,
        result=declared_large,
        caption="caption",
        progress=progress.append,
        item_delivered=item_delivered,
    )

    assert len(receipt.items) == 3
    assert all(item.provider is DeliveryProvider.MULTIPART for item in receipt.items)
    assert persisted_ordinals == [1, 2, 3]
    assert progress[0].stage is DeliveryStage.PACKAGING
    assert progress[-1].item_ordinal == 3
    assert progress[-1].item_count == 3


async def test_multipart_persists_first_receipt_before_later_network_failure(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["max_upload_size_mb"] = 1
    raw["media"]["max_file_size_mb"] = 10
    raw["media"]["max_source_size_mb"] = 10
    raw["multipart"]["part_size_mb"] = 1
    configured = Settings.model_validate(raw)
    source = tmp_path / "result.mp4"
    source.write_bytes(b"source")
    result = DownloadResult(
        job_id=JobId("job"),
        media_id="media",
        title="Title",
        source="youtube",
        kind=MediaKind.VIDEO,
        file_path=source,
        file_size_bytes=2 * 1024 * 1024,
    )
    volume_one = tmp_path / "result.mp4.zip.001"
    volume_two = tmp_path / "result.mp4.zip.002"
    manifest = tmp_path / "result.mp4.manifest.json"
    for path in (volume_one, volume_two, manifest):
        path.write_bytes(b"part")

    class FakeBuilder:
        def build(self, _source: Path) -> MultipartArchive:
            return MultipartArchive((volume_one, volume_two), manifest)

    class FailSecondBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.document_calls = 0

        async def send_document(self, **kwargs: object) -> Message:
            self.document_calls += 1
            if self.document_calls == 2:
                raise TelegramNetworkError(
                    method=SendDocument(chat_id=1, document="existing-file-id"),
                    message="connection lost",
                )
            return await super().send_document(**kwargs)

    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, FailSecondBot())), configured)
    gateway._multipart = cast(Any, FakeBuilder())
    persisted: list[int] = []

    async def item_delivered(item: object) -> None:
        persisted.append(cast(Any, item).ordinal)

    with pytest.raises(DeliveryError):
        await gateway.deliver(
            chat_id=1,
            result=result,
            caption="caption",
            item_delivered=item_delivered,
        )

    assert persisted == [1]


def _auto_delivery(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["upload_as_document"] = False
    return Settings.model_validate(raw)


def _result(tmp_path: Path, kind: MediaKind) -> DownloadResult:
    path = tmp_path / "result.mp4"
    path.write_bytes(b"media")
    return DownloadResult(
        job_id=JobId("job"),
        media_id="media",
        title="Title",
        source="youtube",
        kind=kind,
        file_path=path,
        file_size_bytes=5,
    )


def _message(kind: str) -> Message:
    if kind == "audio":
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            audio=Audio(file_id="file-id", file_unique_id="unique-id", duration=1),
        )
    if kind == "video":
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            video=Video(
                file_id="file-id",
                file_unique_id="unique-id",
                width=1,
                height=1,
                duration=1,
            ),
        )
    if kind == "document":
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            document=Document(file_id="file-id", file_unique_id="unique-id"),
        )
    return Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"))
