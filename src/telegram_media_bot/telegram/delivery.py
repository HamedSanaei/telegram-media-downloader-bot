from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DeliveryError, DeliveryTooLargeError
from telegram_media_bot.domain.models import (
    DeliveryMethod,
    DeliveryReceipt,
    DownloadResult,
    MediaKind,
)

logger = structlog.get_logger(__name__)
_UNSAFE_FILENAME = re.compile(r"[^\w.()\- ]+", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


class TelegramDeliveryGateway(DeliveryGateway):
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings

    async def deliver(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
    ) -> DeliveryReceipt:
        limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        if result.file_size_bytes > limit:
            raise DeliveryTooLargeError("File exceeds the configured Telegram upload limit")
        filename = sanitize_filename(
            result.title,
            suffix=result.file_path.suffix,
            max_length=self._settings.telegram.filename_max_length,
        )
        upload = FSInputFile(result.file_path, filename=filename)
        preferred = self._preferred_method(result)
        try:
            message = await self._send(preferred, chat_id, upload, caption)
            return _receipt(message, preferred)
        except TelegramAPIError as exc:
            if preferred is DeliveryMethod.DOCUMENT:
                raise DeliveryError("Telegram document delivery failed") from exc
            await logger.awarning(
                "telegram_media_delivery_fallback",
                preferred_method=preferred.value,
                error_type=type(exc).__name__,
            )
            try:
                message = await self._send(DeliveryMethod.DOCUMENT, chat_id, upload, caption)
                return _receipt(message, DeliveryMethod.DOCUMENT)
            except TelegramAPIError as fallback_exc:
                raise DeliveryError("Telegram delivery failed") from fallback_exc

    async def send_text(self, chat_id: int, text: str) -> int:
        try:
            message = await self._bot.send_message(chat_id=chat_id, text=text)
        except TelegramAPIError as exc:
            raise DeliveryError("Telegram message delivery failed") from exc
        return message.message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except TelegramAPIError as exc:
            raise DeliveryError("Telegram progress edit failed") from exc

    async def _send(
        self,
        method: DeliveryMethod,
        chat_id: int,
        upload: FSInputFile,
        caption: str,
    ) -> Message:
        if method is DeliveryMethod.AUDIO:
            return await self._bot.send_audio(chat_id=chat_id, audio=upload, caption=caption)
        if method is DeliveryMethod.VIDEO:
            return await self._bot.send_video(
                chat_id=chat_id,
                video=upload,
                caption=caption,
                supports_streaming=True,
            )
        return await self._bot.send_document(chat_id=chat_id, document=upload, caption=caption)

    def _preferred_method(self, result: DownloadResult) -> DeliveryMethod:
        if self._settings.telegram.upload_as_document or result.kind is MediaKind.PLAYLIST:
            return DeliveryMethod.DOCUMENT
        if result.kind is MediaKind.AUDIO:
            return DeliveryMethod.AUDIO
        if result.kind is MediaKind.VIDEO:
            return DeliveryMethod.VIDEO
        return DeliveryMethod.DOCUMENT


def render_caption(settings: Settings, result: DownloadResult) -> str:
    title = sanitize_caption_value(result.title, 768)
    source = sanitize_caption_value(result.source, 128)
    return settings.telegram.caption_template.format(title=title, source=source)[:1024]


def sanitize_caption_value(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    printable = "".join(character if character.isprintable() else " " for character in normalized)
    return _WHITESPACE.sub(" ", printable).strip()[:limit] or "بدون عنوان"


def sanitize_filename(title: str, *, suffix: str, max_length: int) -> str:
    safe_suffix = suffix.casefold() if re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", suffix) else ""
    normalized = unicodedata.normalize("NFKC", Path(title).name)
    cleaned = _UNSAFE_FILENAME.sub("_", normalized).strip(" ._")
    cleaned = _WHITESPACE.sub(" ", cleaned) or "media"
    stem_limit = max(1, max_length - len(safe_suffix))
    return f"{cleaned[:stem_limit].rstrip()}{safe_suffix}"


def _receipt(message: Message, method: DeliveryMethod) -> DeliveryReceipt:
    media = message.audio or message.video or message.document
    if media is None:
        raise DeliveryError("Telegram response did not contain an uploaded file")
    return DeliveryReceipt(
        method=method,
        message_id=message.message_id,
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
    )
