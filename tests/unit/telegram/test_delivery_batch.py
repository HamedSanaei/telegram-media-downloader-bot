from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendPhoto
from aiogram.types import (
    Chat,
    Document,
    InputFile,
    Message,
    PhotoSize,
    Video,
)

from telegram_media_bot.application.ports.delivery import (
    DeliveryCancellationCheck,
    DeliveryItemSink,
    DeliveryProgressSink,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DeliveryError
from telegram_media_bot.domain.models import (
    DeliveryMethod,
    DeliveryReceipt,
    DownloadArtifact,
    DownloadResult,
    JobId,
    MediaKind,
)
from telegram_media_bot.telegram.delivery import (
    SOURCE_URL_LABEL,
    RoutedDeliveryGateway,
    render_batch_summary,
)


def _message(message_id: int, method: str) -> Message:
    chat = Chat(id=1, type="private")
    if method == "photo":
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=chat,
            photo=[
                PhotoSize(
                    file_id=f"f{message_id}",
                    file_unique_id=f"u{message_id}",
                    width=1,
                    height=1,
                    file_size=1,
                )
            ],
        )
    if method == "video":
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=chat,
            video=Video(
                file_id=f"f{message_id}",
                file_unique_id=f"u{message_id}",
                width=1,
                height=1,
                duration=1,
            ),
        )
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        document=Document(file_id=f"f{message_id}", file_unique_id=f"u{message_id}"),
    )


class FakeBatchBot:
    def __init__(self, fail_on: set[int]) -> None:
        self.fail_on = fail_on
        self.upload_count = 0
        self.uploads: list[dict[str, object]] = []
        self.texts: list[str] = []

    async def _consume(self, upload: object) -> None:
        if isinstance(upload, InputFile):
            async for _chunk in upload.read(cast(Bot, cast(Any, self))):
                pass

    async def send_photo(self, **kwargs: object) -> Message:
        return await self._send("photo", kwargs)

    async def send_video(self, **kwargs: object) -> Message:
        return await self._send("video", kwargs)

    async def send_document(self, **kwargs: object) -> Message:
        return await self._send("document", kwargs)

    async def send_audio(self, **kwargs: object) -> Message:
        return await self._send("document", kwargs)

    async def send_media_group(self, **_kwargs: object) -> list[Message]:
        raise DeliveryError("album not used in batch tests")

    async def send_message(self, **kwargs: object) -> Message:
        self.texts.append(str(kwargs["text"]))
        return _message(0, "document")

    async def edit_message_text(self, **_kwargs: object) -> Message:
        return _message(0, "document")

    async def _send(self, method: str, kwargs: dict[str, object]) -> Message:
        self.upload_count += 1
        self.uploads.append(kwargs)
        if self.upload_count in self.fail_on:
            raise TelegramAPIError(
                method=SendPhoto(chat_id=1, photo="existing-file-id"),
                message="network lost",
            )
        await self._consume(kwargs.get(method) or kwargs.get("document"))
        return _message(self.upload_count, method)


def _artifact(path: Path, index: int, kind: MediaKind, name: str) -> DownloadArtifact:
    return DownloadArtifact(
        file_path=path,
        file_size_bytes=path.stat().st_size,
        kind=kind,
        mime_type="image/jpeg" if kind is MediaKind.IMAGE else "video/mp4",
        title=name,
        inline_video_streamable=kind is MediaKind.VIDEO,
        source_index=index,
    )


def _result(tmp_path: Path, names: list[tuple[str, MediaKind]]) -> DownloadResult:
    artifacts = []
    for index, (name, kind) in enumerate(names, start=1):
        path = tmp_path / name
        path.write_bytes(b"media" * index)
        artifacts.append(_artifact(path, index, kind, name))
    first = artifacts[0]
    return DownloadResult(
        job_id=JobId("batch"),
        media_id="batch",
        title="Stories",
        source="instagram",
        kind=MediaKind.PLAYLIST,
        file_path=first.file_path,
        file_size_bytes=sum(item.file_size_bytes for item in artifacts),
        mime_type=first.mime_type,
        artifacts=tuple(artifacts),
        inline_video_streamable=first.inline_video_streamable,
    )


def _gateway(settings: Settings, bot: FakeBatchBot) -> RoutedDeliveryGateway:
    raw = settings.model_dump()
    raw["telegram"]["upload_as_document"] = False
    configured = Settings.model_validate(raw)
    return RoutedDeliveryGateway(cast(Bot, cast(Any, bot)), configured)


async def test_batch_delivers_all_items_in_order(settings: Settings, tmp_path: Path) -> None:
    bot = FakeBatchBot(fail_on=set())
    result = _result(
        tmp_path,
        [("1.jpg", MediaKind.IMAGE), ("2.mp4", MediaKind.VIDEO), ("3.jpg", MediaKind.IMAGE)],
    )
    outcome = await _gateway(settings, bot).deliver_batch(
        chat_id=1,
        result=result,
        caption="cap",
        source_url="https://www.instagram.com/stories/exampleuser/",
        summary_title="\U0001f4da \u062f\u0627\u0646\u0644\u0648\u062f \u0627\u0633\u062a\u0648\u0631\u06cc\u200c\u0647\u0627 \u062a\u0645\u0627\u0645 \u0634\u062f",
    )
    assert outcome.total == 3
    assert outcome.succeeded == 3
    assert outcome.failed == 0
    assert [item.ordinal for item in outcome.receipts] == [1, 2, 3]
    assert [item.method for item in outcome.receipts] == [
        DeliveryMethod.PHOTO,
        DeliveryMethod.VIDEO,
        DeliveryMethod.PHOTO,
    ]
    assert bot.upload_count == 3
    for index, upload in enumerate(bot.uploads, start=1):
        caption = str(upload["caption"])
        assert f"رسانه {index} از 3" in caption
        assert caption.endswith(
            f"{SOURCE_URL_LABEL} https://www.instagram.com/stories/exampleuser/"
        )
    assert any(
        "\u06a9\u0644: 3" in text and "\u0645\u0648\u0641\u0642: 3" in text for text in bot.texts
    )


async def test_batch_isolates_item_failures_and_sends_summary(
    settings: Settings, tmp_path: Path
) -> None:
    bot = FakeBatchBot(fail_on={2})
    result = _result(
        tmp_path,
        [("1.jpg", MediaKind.IMAGE), ("2.mp4", MediaKind.VIDEO), ("3.jpg", MediaKind.IMAGE)],
    )
    outcome = await _gateway(settings, bot).deliver_batch(
        chat_id=1, result=result, caption="cap", summary_title="batch"
    )
    assert outcome.total == 3
    assert outcome.succeeded == 2
    assert outcome.failed == 1
    # Successful items survive an individual failure; ordering is preserved.
    assert [item.ordinal for item in outcome.receipts] == [1, 3]
    assert bot.upload_count == 3
    assert any(
        "\u06a9\u0644: 3" in text
        and "\u0645\u0648\u0641\u0642: 2" in text
        and "\u0646\u0627\u0645\u0648\u0641\u0642: 1" in text
        for text in bot.texts
    )


async def test_batch_all_fail_returns_zero_successes(settings: Settings, tmp_path: Path) -> None:
    bot = FakeBatchBot(fail_on={1, 2})
    result = _result(tmp_path, [("1.jpg", MediaKind.IMAGE), ("2.jpg", MediaKind.IMAGE)])
    outcome = await _gateway(settings, bot).deliver_batch(chat_id=1, result=result, caption="cap")
    assert outcome.succeeded == 0
    assert outcome.failed == 2
    assert outcome.receipts == ()


class _CancellingGateway(RoutedDeliveryGateway):
    def __init__(self, settings: Settings, bot: FakeBatchBot) -> None:
        super().__init__(cast(Bot, cast(Any, bot)), settings)
        self.cancelled = False

    async def _deliver_one(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        source_url: str | None = None,
        caption_title: str = "",
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
        is_cancelled: DeliveryCancellationCheck | None = None,
    ) -> DeliveryReceipt:
        receipt = await super()._deliver_one(
            chat_id=chat_id,
            result=result,
            caption=caption,
            source_url=source_url,
            caption_title=caption_title or result.title,
            progress=progress,
            item_delivered=item_delivered,
            is_cancelled=is_cancelled,
        )
        # Cancel after the first item so the loop observes the cancellation mid-batch.
        self.cancelled = True
        return receipt


async def test_batch_respects_cancellation(settings: Settings, tmp_path: Path) -> None:
    bot = FakeBatchBot(fail_on=set())
    result = _result(
        tmp_path,
        [("1.jpg", MediaKind.IMAGE), ("2.mp4", MediaKind.VIDEO), ("3.jpg", MediaKind.IMAGE)],
    )
    raw = settings.model_dump()
    raw["telegram"]["upload_as_document"] = False
    configured = Settings.model_validate(raw)
    gateway = _CancellingGateway(configured, bot)

    with pytest.raises(asyncio.CancelledError):
        await gateway.deliver_batch(
            chat_id=1,
            result=result,
            caption="cap",
            is_cancelled=lambda: gateway.cancelled,
        )
    # Cancellation stopped the batch before every item was delivered.
    assert bot.upload_count == 1


async def test_batch_does_not_apply_single_item_limit_to_aggregate(
    settings: Settings, tmp_path: Path
) -> None:
    # Each item is small (<= direct limit); the aggregate is large but must not trip a
    # single-item "too large" check.
    bot = FakeBatchBot(fail_on=set())
    artifacts = [
        _artifact(
            _write(tmp_path, f"big-{index}.jpg", 12),
            index,
            MediaKind.IMAGE,
            f"item{index}",
        )
        for index in range(1, 4)
    ]
    first = artifacts[0]
    result = DownloadResult(
        job_id=JobId("batch"),
        media_id="batch",
        title="Stories",
        source="instagram",
        kind=MediaKind.PLAYLIST,
        file_path=first.file_path,
        file_size_bytes=sum(item.file_size_bytes for item in artifacts) * 100,
        mime_type=first.mime_type,
        artifacts=tuple(artifacts),
        inline_video_streamable=first.inline_video_streamable,
    )
    outcome = await _gateway(settings, bot).deliver_batch(chat_id=1, result=result, caption="cap")
    assert outcome.succeeded == 3


def _write(tmp_path: Path, name: str, size: int) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


def test_render_batch_summary_format() -> None:
    text = render_batch_summary("title", 8, 7, 1)
    assert "\u06a9\u0644: 8" in text
    assert "\u0645\u0648\u0641\u0642: 7" in text
    assert "\u0646\u0627\u0645\u0648\u0641\u0642: 1" in text
