from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendVideo
from aiogram.types import Audio, Chat, Document, Message, Video

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DeliveryTooLargeError
from telegram_media_bot.domain.models import DownloadResult, JobId, MediaKind
from telegram_media_bot.telegram.delivery import TelegramDeliveryGateway, render_caption


class FakeBot:
    fail_video = False

    def __init__(self) -> None:
        self.last_upload: dict[str, object] = {}

    async def send_audio(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        return _message("audio")

    async def send_video(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        if self.fail_video:
            raise TelegramBadRequest(
                method=SendVideo(chat_id=1, video="existing-file-id"), message="unsupported"
            )
        return _message("video")

    async def send_document(self, **kwargs: object) -> Message:
        self.last_upload = kwargs
        return _message("document")

    async def send_message(self, **_kwargs: object) -> Message:
        return _message("none")

    async def edit_message_text(self, **_kwargs: object) -> Message:
        return _message("none")


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


async def test_text_delivery_helpers(settings: Settings) -> None:
    gateway = TelegramDeliveryGateway(cast(Bot, cast(Any, FakeBot())), settings)
    assert await gateway.send_text(1, "text") == 1
    await gateway.edit_text(1, 1, "updated")


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
