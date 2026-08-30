import asyncio
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendDocument, SendMediaGroup, SendMessage, SendVideo
from aiogram.types import (
    Audio,
    Chat,
    Document,
    FSInputFile,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageId,
    PhotoSize,
    Video,
)

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DeliveryError,
    DeliveryTooLargeError,
    DeliveryUncertainError,
)
from telegram_media_bot.domain.models import (
    DeliveryMethod,
    DeliveryProgressEvent,
    DeliveryProvider,
    DeliveryStage,
    DownloadArtifact,
    DownloadResult,
    ImageDeliveryMode,
    JobId,
    MediaKind,
)
from telegram_media_bot.infrastructure.archive.multipart_zip import MultipartArchive
from telegram_media_bot.telegram.delivery import (
    SOURCE_URL_LABEL,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MEDIA_GROUP_MAX_ITEMS,
    RoutedDeliveryGateway,
    TelegramDeliveryGateway,
    TrackedFSInputFile,
    _finalization_heartbeat,
    append_source_url,
    build_instagram_delivery_batches,
    chunk_media_items,
    render_caption,
)


class FakeBot:
    fail_video = False
    fail_video_network = False
    fail_album = False
    fail_album_network = False

    def __init__(self) -> None:
        self.last_upload: dict[str, object] = {}
        self.uploads: list[dict[str, object]] = []
        self.texts: list[dict[str, object]] = []

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

    async def send_photo(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        self.uploads.append(kwargs)
        await self._consume(kwargs.get("photo"))
        return _message("photo")

    async def send_media_group(self, **kwargs: object) -> list[Message]:
        self.last_upload = kwargs
        self.uploads.append(kwargs)
        media = cast(list[object], kwargs["media"])
        if self.fail_album:
            raise TelegramBadRequest(
                method=SendMediaGroup(chat_id=1, media=cast(Any, media)),
                message="unsupported",
            )
        if self.fail_album_network:
            raise TelegramNetworkError(
                method=SendMediaGroup(chat_id=1, media=cast(Any, media)),
                message="connection lost",
            )
        return [
            _message("photo" if isinstance(item, InputMediaPhoto) else "video", index + 1)
            for index, item in enumerate(media)
        ]

    async def send_message(self, **kwargs: object) -> Message:
        self.texts.append(kwargs)
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
    source_url = "https://www.instagram.com/reel/ABC123/"
    receipt = await gateway.deliver(
        chat_id=1,
        result=_result(tmp_path, MediaKind.VIDEO),
        caption="caption",
        source_url=source_url,
    )
    assert receipt.method.value == "document"
    assert bot.last_upload["caption"] == f"caption\n\n{SOURCE_URL_LABEL} {source_url}"
    assert len(bot.uploads) == 2


async def test_non_streamable_native_video_uses_document_without_encode(
    settings: Settings,
    tmp_path: Path,
) -> None:
    bot = FakeBot()
    result = replace(_result(tmp_path, MediaKind.VIDEO), inline_video_streamable=False)

    receipt = await TelegramDeliveryGateway(
        cast(Bot, cast(Any, bot)),
        _auto_delivery(settings),
    ).deliver(chat_id=1, result=result, caption="caption")

    assert receipt.method is DeliveryMethod.DOCUMENT
    assert "document" in bot.last_upload


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


async def test_inline_images_are_sent_as_photo(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")
    result = replace(
        _result(tmp_path, MediaKind.IMAGE),
        file_path=path,
        mime_type="image/jpeg",
    )
    bot = FakeBot()

    receipt = await TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1, result=result, caption="caption"
    )

    assert receipt.method is DeliveryMethod.PHOTO
    assert "photo" in bot.last_upload


async def test_album_chunks_preserve_order_and_avoid_singleton_group(
    settings: Settings, tmp_path: Path
) -> None:
    artifacts: list[DownloadArtifact] = []
    for index in range(11):
        path = tmp_path / f"{index + 1:04}.jpg"
        path.write_bytes(b"image")
        artifacts.append(
            DownloadArtifact(path, 5, MediaKind.IMAGE, "image/jpeg", f"Image {index + 1}")
        )
    result = DownloadResult(
        job_id=JobId("album"),
        media_id="album",
        title="Album",
        source="instagram",
        kind=MediaKind.PLAYLIST,
        file_path=artifacts[0].file_path,
        file_size_bytes=55,
        artifacts=tuple(artifacts),
    )
    bot = FakeBot()
    source_url = "https://www.instagram.com/p/Album123/"

    receipt = await RoutedDeliveryGateway(
        cast(Bot, cast(Any, bot)), _auto_delivery(settings)
    ).deliver(chat_id=1, result=result, caption="caption", source_url=source_url)

    assert [len(cast(list[object], call["media"])) for call in bot.uploads] == [9, 2]
    assert [item.ordinal for item in receipt.items] == list(range(1, 12))
    first_group = cast(list[Any], bot.uploads[0]["media"])
    source_text = f"{SOURCE_URL_LABEL} {source_url}"
    assert first_group[0].caption == f"caption\n\n{source_text}"
    assert all(item.caption == source_text for item in first_group[1:])
    second_group = cast(list[Any], bot.uploads[1]["media"])
    assert all(item.caption == source_text for item in second_group)


async def test_rejected_album_falls_back_but_ambiguous_album_does_not_retry(
    settings: Settings, tmp_path: Path
) -> None:
    artifacts: list[DownloadArtifact] = []
    for index in range(2):
        path = tmp_path / f"fallback-{index}.jpg"
        path.write_bytes(b"image")
        artifacts.append(DownloadArtifact(path, 5, MediaKind.IMAGE, "image/jpeg"))
    result = DownloadResult(
        JobId("album-fallback"),
        "album",
        "Album",
        "instagram",
        MediaKind.PLAYLIST,
        artifacts[0].file_path,
        10,
        artifacts=tuple(artifacts),
    )
    rejected = FakeBot()
    rejected.fail_album = True
    receipt = await RoutedDeliveryGateway(cast(Bot, cast(Any, rejected)), settings).deliver(
        chat_id=1, result=result, caption="caption"
    )
    assert len(receipt.items) == 2
    assert len(rejected.uploads) == 3

    for artifact in artifacts:
        artifact.file_path.write_bytes(b"image")
    ambiguous = FakeBot()
    ambiguous.fail_album_network = True
    with pytest.raises(DeliveryError):
        await RoutedDeliveryGateway(cast(Bot, cast(Any, ambiguous)), settings).deliver(
            chat_id=1, result=result, caption="caption"
        )
    assert len(ambiguous.uploads) == 1


def test_caption_contains_runtime_bot_username(settings: Settings, tmp_path: Path) -> None:
    caption = render_caption(settings, _result(tmp_path, MediaKind.VIDEO), "ExampleBot")
    assert "@ExampleBot" in caption


def test_source_url_is_appended_after_unchanged_caption_with_one_blank_line(
    settings: Settings,
    tmp_path: Path,
) -> None:
    result = _result(tmp_path, MediaKind.VIDEO)
    old_caption = render_caption(settings, result, "ExampleBot")
    source_url = "https://www.instagram.com/reel/ABC123/"

    placement = append_source_url(
        old_caption,
        source_url,
        title="Title",
    )

    assert placement.media_caption == f"{old_caption}\n\n{SOURCE_URL_LABEL} {source_url}"
    assert placement.media_caption.endswith(f"{SOURCE_URL_LABEL} {source_url}")
    assert placement.fallback_text is None
    assert "@ExampleBot" in placement.media_caption


def test_source_url_boundary_reduces_only_title_and_never_truncates_url(
    settings: Settings,
    tmp_path: Path,
) -> None:
    title = "T" * 768
    result = replace(_result(tmp_path, MediaKind.VIDEO), title=title)
    old_caption = render_caption(settings, result, "ExampleBot")
    source_url = f"https://example.com/{'u' * 300}"

    placement = append_source_url(old_caption, source_url, title=title)

    assert len(placement.media_caption) <= TELEGRAM_CAPTION_LIMIT
    assert placement.media_caption.endswith(f"{SOURCE_URL_LABEL} {source_url}")
    assert source_url in placement.media_caption
    assert "منبع: youtube" in placement.media_caption
    assert "@ExampleBot" in placement.media_caption
    assert placement.fallback_text is None


def test_source_url_exact_1024_boundary_and_one_character_over() -> None:
    source_url = "https://example.com/original"
    source_text = f"{SOURCE_URL_LABEL} {source_url}"
    fixed = "\nsource: youtube\n@ExampleBot"
    title_length = TELEGRAM_CAPTION_LIMIT - len(fixed) - 2 - len(source_text)
    exact_title = "T" * title_length

    exact = append_source_url(exact_title + fixed, source_url, title=exact_title)
    over_title = f"{exact_title}T"
    reduced = append_source_url(over_title + fixed, source_url, title=over_title)

    assert len(exact.media_caption) == TELEGRAM_CAPTION_LIMIT
    assert exact.media_caption.startswith(exact_title)
    assert len(reduced.media_caption) == TELEGRAM_CAPTION_LIMIT
    assert reduced.media_caption.startswith(exact_title)
    assert reduced.media_caption.endswith(source_text)
    assert exact.fallback_text is reduced.fallback_text is None


async def test_source_url_uses_replied_text_fallback_without_duplicate_media(
    settings: Settings,
    tmp_path: Path,
) -> None:
    bot = FakeBot()
    result = _result(tmp_path, MediaKind.VIDEO)
    existing_caption = "ثابت" * 200
    source_url = f"https://example.com/{'u' * 200}"

    receipt = await TelegramDeliveryGateway(
        cast(Bot, cast(Any, bot)), _auto_delivery(settings)
    ).deliver(
        chat_id=1,
        result=result,
        caption=existing_caption,
        source_url=source_url,
    )

    assert len(bot.uploads) == 1
    assert bot.last_upload["caption"] == existing_caption
    assert bot.texts[0]["text"] == f"{SOURCE_URL_LABEL} {source_url}"
    reply = cast(Any, bot.texts[0]["reply_parameters"])
    assert reply.message_id == receipt.message_id


async def test_source_reply_failure_persists_media_before_delivery_becomes_uncertain(
    settings: Settings,
    tmp_path: Path,
) -> None:
    class FailSourceReplyBot(FakeBot):
        async def send_message(self, **kwargs: object) -> Message:
            self.texts.append(kwargs)
            raise TelegramNetworkError(
                method=SendMessage(chat_id=1, text="source"),
                message="connection lost",
            )

    bot = FailSourceReplyBot()
    persisted: list[int] = []

    async def item_delivered(item: object) -> None:
        persisted.append(cast(Any, item).ordinal)

    with pytest.raises(DeliveryUncertainError):
        await TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), _auto_delivery(settings)).deliver(
            chat_id=1,
            result=_result(tmp_path, MediaKind.VIDEO),
            caption="ثابت" * 200,
            source_url=f"https://example.com/{'u' * 200}",
            item_delivered=item_delivered,
        )

    assert len(bot.uploads) == 1
    assert persisted == [1]


@pytest.mark.parametrize(
    ("kind", "suffix", "mime_type", "expected_method"),
    [
        (MediaKind.AUDIO, ".mp3", "audio/mpeg", DeliveryMethod.AUDIO),
        (MediaKind.VIDEO, ".mp4", "video/mp4", DeliveryMethod.VIDEO),
        (MediaKind.IMAGE, ".jpg", "image/jpeg", DeliveryMethod.PHOTO),
        (MediaKind.UNKNOWN, ".bin", "application/octet-stream", DeliveryMethod.DOCUMENT),
    ],
)
async def test_every_single_media_method_keeps_source_url(
    settings: Settings,
    tmp_path: Path,
    kind: MediaKind,
    suffix: str,
    mime_type: str,
    expected_method: DeliveryMethod,
) -> None:
    path = tmp_path / f"media{suffix}"
    path.write_bytes(b"media")
    result = replace(
        _result(tmp_path, kind),
        file_path=path,
        mime_type=mime_type,
        inline_video_streamable=kind is MediaKind.VIDEO,
    )
    bot = FakeBot()
    source_url = "https://example.com/original"

    receipt = await TelegramDeliveryGateway(
        cast(Bot, cast(Any, bot)), _auto_delivery(settings)
    ).deliver(chat_id=1, result=result, caption="old", source_url=source_url)

    assert receipt.method is expected_method
    assert bot.last_upload["caption"] == f"old\n\n{SOURCE_URL_LABEL} {source_url}"


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

    bot = FakeBot()
    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), configured)
    gateway._multipart = cast(Any, FakeBuilder())

    persisted_ordinals: list[int] = []
    progress: list[DeliveryProgressEvent] = []

    async def item_delivered(item: object) -> None:
        persisted_ordinals.append(cast(Any, item).ordinal)

    receipt = await gateway.deliver(
        chat_id=1,
        result=declared_large,
        caption="caption",
        source_url="https://example.com/original",
        progress=progress.append,
        item_delivered=item_delivered,
    )

    assert len(receipt.items) == 3
    assert all(item.provider is DeliveryProvider.MULTIPART for item in receipt.items)
    assert persisted_ordinals == [1, 2, 3]
    assert progress[0].stage is DeliveryStage.PACKAGING
    assert progress[-1].item_ordinal == 3
    assert progress[-1].item_count == 3
    assert all(
        str(upload["caption"]).endswith(f"{SOURCE_URL_LABEL} https://example.com/original")
        for upload in bot.uploads
    )
    assert "بخش 1 از 2\n\n" in str(bot.uploads[0]["caption"])
    assert "manifest شامل اندازه و SHA-256 همه بخش‌ها\n\n" in str(  # noqa: RUF001
        bot.uploads[-1]["caption"]
    )
    assert not volume_one.exists()
    assert not volume_two.exists()
    assert not manifest.exists()


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
    assert not volume_one.exists()
    assert volume_two.exists()
    assert manifest.exists()


async def test_multipart_cancellation_stops_active_archive_process(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["max_upload_size_mb"] = 1
    raw["media"]["max_file_size_mb"] = 10
    raw["media"]["max_source_size_mb"] = 10
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
    started = threading.Event()
    cancelled = threading.Event()

    class BlockingBuilder:
        def build(self, _source: Path) -> MultipartArchive:
            started.set()
            if not cancelled.wait(2):
                raise RuntimeError("archive process was not cancelled")
            raise RuntimeError("archive process terminated")

        def cancel_active(self) -> None:
            cancelled.set()

    gateway = RoutedDeliveryGateway(cast(Bot, cast(Any, FakeBot())), configured)
    gateway._multipart = cast(Any, BlockingBuilder())
    task = asyncio.create_task(gateway.deliver(chat_id=1, result=result, caption="caption"))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_instagram_batch_planner_rejects_empty_and_invalid_limits(tmp_path: Path) -> None:
    with pytest.raises(DeliveryError, match="empty"):
        build_instagram_delivery_batches((), ImageDeliveryMode.PHOTO)
    assert chunk_media_items(()) == ()
    with pytest.raises(ValueError):
        chunk_media_items((), max_items=0)
    with pytest.raises(ValueError):
        chunk_media_items((), max_items=TELEGRAM_MEDIA_GROUP_MAX_ITEMS + 1)


@pytest.mark.parametrize(
    ("count", "expected_request_sizes"),
    [
        (1, [1]),
        (9, [9]),
        (10, [10]),
        (11, [10, 1]),
        (20, [10, 10]),
        (21, [10, 10, 1]),
        (37, [10, 10, 10, 7]),
    ],
)
async def test_instagram_photo_batches_preserve_every_source_item(
    settings: Settings,
    tmp_path: Path,
    count: int,
    expected_request_sizes: list[int],
) -> None:
    result = _instagram_result(tmp_path, [MediaKind.IMAGE] * count, ImageDeliveryMode.PHOTO)
    bot = FakeBot()

    receipt = await RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1,
        result=result,
        caption="caption",
    )

    assert [
        len(cast(list[object], upload["media"])) if "media" in upload else 1
        for upload in bot.uploads
    ] == expected_request_sizes
    assert [item.ordinal for item in receipt.items] == list(range(1, count + 1))
    assert len({item.ordinal for item in receipt.items}) == count
    remaining = await asyncio.to_thread(
        lambda: [
            artifact.file_path
            for artifact in result.delivery_artifacts
            if artifact.file_path.exists()
        ]
    )
    assert remaining == []


@pytest.mark.parametrize(
    ("kind", "source_url"),
    [
        (MediaKind.IMAGE, "https://www.instagram.com/p/Image123/"),
        (MediaKind.VIDEO, "https://www.instagram.com/reel/Reel123/"),
    ],
)
async def test_instagram_single_post_and_reel_keep_canonical_source_url(
    settings: Settings,
    tmp_path: Path,
    kind: MediaKind,
    source_url: str,
) -> None:
    result = _instagram_result(tmp_path, [kind], ImageDeliveryMode.PHOTO)
    bot = FakeBot()

    receipt = await RoutedDeliveryGateway(
        cast(Bot, cast(Any, bot)), _auto_delivery(settings)
    ).deliver(
        chat_id=1,
        result=result,
        caption="old caption",
        source_url=source_url,
    )

    assert len(receipt.items) == 1
    assert bot.last_upload["caption"] == (f"old caption\n\n{SOURCE_URL_LABEL} {source_url}")


async def test_instagram_album_first_item_keeps_old_caption_and_all_items_are_traceable(
    settings: Settings,
    tmp_path: Path,
) -> None:
    result = _instagram_result(
        tmp_path,
        [MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.IMAGE],
        ImageDeliveryMode.PHOTO,
    )
    bot = FakeBot()
    source_url = "https://www.instagram.com/p/Mixed123/"

    await RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1,
        result=result,
        caption="old caption",
        source_url=source_url,
    )

    media = cast(list[Any], bot.uploads[0]["media"])
    source_text = f"{SOURCE_URL_LABEL} {source_url}"
    assert media[0].caption == f"old caption\n\n{source_text}"
    assert [item.caption for item in media[1:]] == [source_text, source_text]


@pytest.mark.parametrize(
    ("suffix", "mime_type"),
    [(".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")],
)
async def test_instagram_document_delivery_preserves_exact_bytes_and_format(
    settings: Settings,
    tmp_path: Path,
    suffix: str,
    mime_type: str,
) -> None:
    payload = b"exact-gallery-dl-original\x00bytes"
    path = tmp_path / f"0001-image{suffix}"
    path.write_bytes(payload)
    result = DownloadResult(
        job_id=JobId("exact-image"),
        media_id="post",
        title="Image",
        source="instagram",
        kind=MediaKind.IMAGE,
        file_path=path,
        file_size_bytes=len(payload),
        mime_type=mime_type,
        image_delivery_mode=ImageDeliveryMode.DOCUMENT,
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    bot = FakeBot()

    receipt = await TelegramDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1,
        result=result,
        caption="caption",
        source_url="https://www.instagram.com/p/Original123/",
    )

    upload = cast(FSInputFile, bot.last_upload["document"])
    upload_path = Path(upload.path)
    assert receipt.method is DeliveryMethod.DOCUMENT
    assert upload_path.suffix == suffix
    delivered_bytes = await asyncio.to_thread(upload_path.read_bytes)
    assert hashlib.sha256(delivered_bytes).hexdigest() == before
    assert upload.filename is not None and upload.filename.endswith(suffix)
    assert bot.last_upload["caption"] == (
        f"caption\n\n{SOURCE_URL_LABEL} https://www.instagram.com/p/Original123/"
    )


async def test_instagram_document_albums_use_documents_and_exact_ten_boundaries(
    settings: Settings,
    tmp_path: Path,
) -> None:
    result = _instagram_result(
        tmp_path,
        [MediaKind.IMAGE] * 21,
        ImageDeliveryMode.DOCUMENT,
    )
    bot = FakeBot()

    await RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1,
        result=result,
        caption="caption",
    )

    assert [
        len(cast(list[object], upload["media"])) if "media" in upload else 1
        for upload in bot.uploads
    ] == [10, 10, 1]
    for upload in bot.uploads[:2]:
        assert all(
            isinstance(item, InputMediaDocument) for item in cast(list[object], upload["media"])
        )
    assert "document" in bot.uploads[-1]


async def test_instagram_mixed_photo_mode_keeps_image_image_video_image_order(
    settings: Settings,
    tmp_path: Path,
) -> None:
    kinds = [MediaKind.IMAGE, MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.IMAGE]
    result = _instagram_result(tmp_path, kinds, ImageDeliveryMode.PHOTO)
    bot = FakeBot()

    receipt = await RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
        chat_id=1,
        result=result,
        caption="caption",
    )

    media = cast(list[object], bot.uploads[0]["media"])
    assert [type(item) for item in media] == [
        InputMediaPhoto,
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaPhoto,
    ]
    assert [item.ordinal for item in receipt.items] == [1, 2, 3, 4]


async def test_instagram_mixed_document_mode_uses_ordered_type_runs(
    settings: Settings,
    tmp_path: Path,
) -> None:
    kinds = [MediaKind.IMAGE, MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.IMAGE]
    result = _instagram_result(tmp_path, kinds, ImageDeliveryMode.DOCUMENT)
    bot = FakeBot()

    receipt = await RoutedDeliveryGateway(
        cast(Bot, cast(Any, bot)), _auto_delivery(settings)
    ).deliver(
        chat_id=1,
        result=result,
        caption="caption",
    )

    assert [
        len(cast(list[object], upload["media"])) if "media" in upload else 1
        for upload in bot.uploads
    ] == [2, 1, 1]
    assert all(
        isinstance(item, InputMediaDocument) for item in cast(list[object], bot.uploads[0]["media"])
    )
    assert "video" in bot.uploads[1]
    assert "document" in bot.uploads[2]
    assert [item.ordinal for item in receipt.items] == [1, 2, 3, 4]


async def test_instagram_cancellation_is_checked_between_media_groups(
    settings: Settings,
    tmp_path: Path,
) -> None:
    cancelled = False

    class CancellingBot(FakeBot):
        async def send_media_group(self, **kwargs: object) -> list[Message]:
            nonlocal cancelled
            messages = await super().send_media_group(**kwargs)
            cancelled = True
            return messages

    result = _instagram_result(
        tmp_path,
        [MediaKind.IMAGE] * 21,
        ImageDeliveryMode.PHOTO,
    )
    bot = CancellingBot()

    with pytest.raises(asyncio.CancelledError):
        await RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), settings).deliver(
            chat_id=1,
            result=result,
            caption="caption",
            is_cancelled=lambda: cancelled,
        )

    assert len(bot.uploads) == 1


async def test_instagram_failure_in_later_batch_stops_before_success_receipt(
    settings: Settings,
    tmp_path: Path,
) -> None:
    class FailingSecondBatchBot(FakeBot):
        async def send_media_group(self, **kwargs: object) -> list[Message]:
            if self.uploads:
                raise TelegramNetworkError(
                    method=SendMediaGroup(chat_id=1, media=cast(Any, kwargs["media"])),
                    message="connection lost",
                )
            return await super().send_media_group(**kwargs)

    result = _instagram_result(
        tmp_path,
        [MediaKind.IMAGE] * 20,
        ImageDeliveryMode.PHOTO,
    )

    with pytest.raises(DeliveryError):
        await RoutedDeliveryGateway(
            cast(Bot, cast(Any, FailingSecondBatchBot())), settings
        ).deliver(chat_id=1, result=result, caption="caption")


def _instagram_result(
    tmp_path: Path,
    kinds: list[MediaKind],
    mode: ImageDeliveryMode,
) -> DownloadResult:
    artifacts: list[DownloadArtifact] = []
    for index, kind in enumerate(kinds, start=1):
        suffix = ".jpg" if kind is MediaKind.IMAGE else ".mp4"
        path = tmp_path / f"{index:04}{suffix}"
        path.write_bytes(f"asset-{index}".encode())
        artifacts.append(
            DownloadArtifact(
                file_path=path,
                file_size_bytes=path.stat().st_size,
                kind=kind,
                mime_type="image/jpeg" if kind is MediaKind.IMAGE else "video/mp4",
                title=f"Asset {index}",
                inline_video_streamable=kind is MediaKind.VIDEO,
                source_index=index,
            )
        )
    first = artifacts[0]
    return DownloadResult(
        job_id=JobId("instagram-delivery"),
        media_id="post",
        title="Instagram post",
        source="instagram",
        kind=first.kind if len(artifacts) == 1 else MediaKind.PLAYLIST,
        file_path=first.file_path,
        file_size_bytes=sum(item.file_size_bytes for item in artifacts),
        mime_type=first.mime_type,
        artifacts=tuple(artifacts) if len(artifacts) > 1 else (),
        inline_video_streamable=first.inline_video_streamable,
        image_delivery_mode=mode,
    )


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
        inline_video_streamable=kind is MediaKind.VIDEO,
    )


def _message(kind: str, message_id: int = 1) -> Message:
    if kind == "audio":
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            audio=Audio(file_id="file-id", file_unique_id="unique-id", duration=1),
        )
    if kind == "video":
        return Message(
            message_id=message_id,
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
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            document=Document(file_id="file-id", file_unique_id="unique-id"),
        )
    if kind == "photo":
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            photo=[
                PhotoSize(
                    file_id="file-id",
                    file_unique_id="unique-id",
                    width=1,
                    height=1,
                    file_size=1,
                )
            ],
        )
    return Message(message_id=message_id, date=datetime.now(UTC), chat=Chat(id=1, type="private"))
