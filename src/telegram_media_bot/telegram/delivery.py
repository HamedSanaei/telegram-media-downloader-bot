from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from time import monotonic

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, InputFile, Message

from telegram_media_bot.application.ports.delivery import (
    DeliveryGateway,
    DeliveryItemSink,
    DeliveryProgressSink,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DeliveryError,
    DeliveryTooLargeError,
)
from telegram_media_bot.domain.models import (
    DeliveryItemReceipt,
    DeliveryMethod,
    DeliveryProgressEvent,
    DeliveryProvider,
    DeliveryReceipt,
    DeliveryStage,
    DownloadResult,
    MediaKind,
)
from telegram_media_bot.infrastructure.archive.multipart_zip import MultipartZipBuilder

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
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
    ) -> DeliveryReceipt:
        limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        if result.file_size_bytes > limit:
            raise DeliveryTooLargeError("File exceeds the configured Telegram upload limit")
        filename = sanitize_filename(
            result.title,
            suffix=result.file_path.suffix,
            max_length=self._settings.telegram.filename_max_length,
        )
        preferred = self._preferred_method(result)
        try:
            message = await self._send_tracked(
                preferred,
                chat_id,
                result,
                filename,
                caption,
                progress,
            )
            receipt = _receipt(message, preferred)
            if item_delivered is not None:
                await item_delivered(receipt.primary)
            return receipt
        except TelegramBadRequest as exc:
            if preferred is DeliveryMethod.DOCUMENT:
                await logger.awarning(
                    "telegram_document_delivery_failed",
                    error_type=type(exc).__name__,
                )
                raise DeliveryError("Telegram document delivery failed") from exc
            await logger.awarning(
                "telegram_media_delivery_fallback",
                preferred_method=preferred.value,
                error_type=type(exc).__name__,
            )
            try:
                message = await self._send_tracked(
                    DeliveryMethod.DOCUMENT,
                    chat_id,
                    result,
                    filename,
                    caption,
                    progress,
                )
                receipt = _receipt(message, DeliveryMethod.DOCUMENT)
                if item_delivered is not None:
                    await item_delivered(receipt.primary)
                return receipt
            except TelegramAPIError as fallback_exc:
                await logger.awarning(
                    "telegram_document_fallback_failed",
                    error_type=type(fallback_exc).__name__,
                )
                raise DeliveryError("Telegram delivery failed") from fallback_exc
        except TelegramAPIError as exc:
            await logger.awarning(
                "telegram_delivery_response_uncertain",
                method=preferred.value,
                error_type=type(exc).__name__,
            )
            raise DeliveryError("Telegram delivery response is uncertain") from exc

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

    async def _send_tracked(
        self,
        method: DeliveryMethod,
        chat_id: int,
        result: DownloadResult,
        filename: str,
        caption: str,
        progress: DeliveryProgressSink | None,
    ) -> Message:
        started = monotonic()
        stream_finished = asyncio.Event()
        request_finished = asyncio.Event()

        def emit(stage: DeliveryStage, transferred: int) -> None:
            if progress is None:
                return
            progress(
                DeliveryProgressEvent(
                    job_id=result.job_id,
                    stage=stage,
                    transferred_bytes=transferred,
                    total_bytes=result.file_size_bytes,
                    item_transferred_bytes=transferred,
                    item_size_bytes=result.file_size_bytes,
                    elapsed_seconds=monotonic() - started,
                )
            )

        def on_stream_finished(transferred: int) -> None:
            emit(DeliveryStage.FINALIZING, transferred)
            stream_finished.set()

        upload = TrackedFSInputFile(
            result.file_path,
            filename=filename,
            chunk_size=self._settings.telegram.upload_chunk_size_kb * 1024,
            on_progress=lambda transferred: emit(DeliveryStage.UPLOADING, transferred),
            on_finished=on_stream_finished,
        )
        heartbeat = asyncio.create_task(
            _finalization_heartbeat(
                stream_finished,
                request_finished,
                interval_seconds=self._settings.telegram.upload_heartbeat_interval_seconds,
                emit=lambda: emit(DeliveryStage.FINALIZING, result.file_size_bytes),
            )
        )
        try:
            return await self._send(method, chat_id, upload, caption)
        finally:
            request_finished.set()
            await heartbeat

    async def _send(
        self,
        method: DeliveryMethod,
        chat_id: int,
        upload: InputFile,
        caption: str,
    ) -> Message:
        request_timeout = self._settings.telegram.upload_timeout_seconds
        if method is DeliveryMethod.AUDIO:
            return await self._bot.send_audio(
                chat_id=chat_id,
                audio=upload,
                caption=caption,
                request_timeout=request_timeout,
            )
        if method is DeliveryMethod.VIDEO:
            return await self._bot.send_video(
                chat_id=chat_id,
                video=upload,
                caption=caption,
                supports_streaming=True,
                request_timeout=request_timeout,
            )
        return await self._bot.send_document(
            chat_id=chat_id,
            document=upload,
            caption=caption,
            request_timeout=request_timeout,
        )

    def _preferred_method(self, result: DownloadResult) -> DeliveryMethod:
        if (
            (self._settings.telegram.upload_as_document and is_document_delivery_compatible(result))
            or result.kind is MediaKind.PLAYLIST
            or result.file_path.suffix.casefold() == ".webm"
        ):
            return DeliveryMethod.DOCUMENT
        if result.kind is MediaKind.AUDIO:
            return DeliveryMethod.AUDIO
        if result.kind is MediaKind.VIDEO and result.inline_video_streamable:
            return DeliveryMethod.VIDEO
        return DeliveryMethod.DOCUMENT


def is_document_delivery_compatible(result: DownloadResult) -> bool:
    """Documents impose no media codec/profile restriction on a regular local file."""
    return result.file_path.is_file()


class RoutedDeliveryGateway(DeliveryGateway):
    """Route final files through Local Bot API or bounded ZIP volumes."""

    def __init__(self, bot: Bot, settings: Settings) -> None:
        self._settings = settings
        self._direct = TelegramDeliveryGateway(bot, settings)
        self._multipart = MultipartZipBuilder(settings.multipart)

    async def deliver(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
    ) -> DeliveryReceipt:
        if result.artifacts:
            return await self._deliver_artifacts(
                chat_id=chat_id,
                result=result,
                caption=caption,
                progress=progress,
                item_delivered=item_delivered,
            )
        return await self._deliver_one(
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
        )

    async def _deliver_one(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
    ) -> DeliveryReceipt:
        direct_limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        if result.file_size_bytes <= direct_limit:
            return await self._direct.deliver(
                chat_id=chat_id,
                result=result,
                caption=caption,
                progress=progress,
                item_delivered=item_delivered,
            )
        if not self._settings.multipart.enabled:
            raise DeliveryTooLargeError("Multipart delivery is disabled")
        return await self._deliver_multipart(
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
        )

    async def _deliver_artifacts(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
    ) -> DeliveryReceipt:
        receipts: list[DeliveryItemReceipt] = []
        completed_bytes = 0
        total_bytes = result.total_file_size_bytes
        for artifact_index, artifact in enumerate(result.artifacts, start=1):
            child = DownloadResult(
                job_id=result.job_id,
                media_id=result.media_id,
                title=artifact.title or f"{result.title} {artifact_index}",
                source=result.source,
                kind=artifact.kind,
                file_path=artifact.file_path,
                file_size_bytes=artifact.file_size_bytes,
                duration_seconds=result.duration_seconds,
                mime_type=artifact.mime_type,
                inline_video_streamable=artifact.inline_video_streamable,
            )
            ordinal_offset = len(receipts)

            def map_progress(
                event: DeliveryProgressEvent,
                *,
                completed: int = completed_bytes,
                item_number: int = artifact_index,
            ) -> None:
                if progress is None:
                    return
                progress(
                    replace(
                        event,
                        transferred_bytes=min(
                            total_bytes,
                            completed + event.transferred_bytes,
                        ),
                        total_bytes=total_bytes,
                        item_ordinal=item_number,
                        item_count=len(result.artifacts),
                    )
                )

            async def persist_item(
                item: DeliveryItemReceipt,
                *,
                offset: int = ordinal_offset,
            ) -> None:
                mapped = replace(item, ordinal=offset + item.ordinal)
                if item_delivered is not None:
                    await item_delivered(mapped)

            child_receipt = await self._deliver_one(
                chat_id=chat_id,
                result=child,
                caption=f"{caption}\nویدئو {artifact_index} از {len(result.artifacts)}",
                progress=map_progress,
                item_delivered=persist_item,
            )
            receipts.extend(
                replace(item, ordinal=ordinal_offset + item.ordinal) for item in child_receipt.items
            )
            await _delete_confirmed_delivery_file(
                artifact.file_path,
                job_id=result.job_id,
                cleanup_reason="artifact_delivered",
            )
            completed_bytes += artifact.file_size_bytes
        return DeliveryReceipt(items=tuple(receipts))

    async def send_text(self, chat_id: int, text: str) -> int:
        return await self._direct.send_text(chat_id, text)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        await self._direct.edit_text(chat_id, message_id, text)

    async def _deliver_multipart(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
    ) -> DeliveryReceipt:
        packaging_started = monotonic()
        if progress is not None:
            progress(
                DeliveryProgressEvent(
                    job_id=result.job_id,
                    stage=DeliveryStage.PACKAGING,
                    total_bytes=result.file_size_bytes,
                )
            )
        multipart = (
            self._multipart.isolated()
            if isinstance(self._multipart, MultipartZipBuilder)
            else self._multipart
        )
        archive_task = asyncio.create_task(asyncio.to_thread(multipart.build, result.file_path))
        try:
            archive = await asyncio.shield(archive_task)
        except asyncio.CancelledError:
            await asyncio.to_thread(multipart.cancel_active)
            with suppress(Exception):
                await archive_task
            raise
        paths = (*archive.volumes, archive.manifest)
        total_upload_bytes = sum(path.stat().st_size for path in paths)
        completed_bytes = 0
        receipts: list[DeliveryItemReceipt] = []
        for ordinal, path in enumerate(paths, start=1):
            part = DownloadResult(
                job_id=result.job_id,
                media_id=result.media_id,
                title=path.stem,
                source=result.source,
                kind=MediaKind.UNKNOWN,
                file_path=path,
                file_size_bytes=path.stat().st_size,
                mime_type="application/zip" if path != archive.manifest else "application/json",
            )
            part_caption = (
                f"{caption}\nبخش {ordinal} از {len(archive.volumes)}"
                if path != archive.manifest
                else f"{caption}\nفایل manifest شامل اندازه و SHA-256 همه بخش‌ها"  # noqa: RUF001
            )

            def map_progress(
                event: DeliveryProgressEvent,
                *,
                completed: int = completed_bytes,
                item_ordinal: int = ordinal,
            ) -> None:
                if progress is None:
                    return
                progress(
                    replace(
                        event,
                        transferred_bytes=completed + event.item_transferred_bytes,
                        total_bytes=total_upload_bytes,
                        item_ordinal=item_ordinal,
                        item_count=len(paths),
                        elapsed_seconds=monotonic() - packaging_started,
                    )
                )

            receipt = await self._direct.deliver(
                chat_id=chat_id,
                result=part,
                caption=part_caption,
                progress=map_progress,
            )
            item = replace(
                receipt.primary,
                provider=DeliveryProvider.MULTIPART,
                ordinal=ordinal,
            )
            receipts.append(item)
            if item_delivered is not None:
                await item_delivered(item)
            await _delete_confirmed_delivery_file(
                path,
                job_id=result.job_id,
                cleanup_reason="multipart_part_delivered",
            )
            completed_bytes += part.file_size_bytes
        await self._direct.send_text(
            chat_id,
            "همه فایل‌های ‎.zip.001‎، ‎.zip.002‎ و سایر بخش‌ها را در یک پوشه قرار دهید "  # noqa: RUF001
            "و فایل ‎.zip.001‎ را با 7-Zip باز و Extract کنید. سپس SHA-256 را با manifest "
            "بررسی کنید.",
        )
        return DeliveryReceipt(items=tuple(receipts))


async def _delete_confirmed_delivery_file(
    path: Path,
    *,
    job_id: object,
    cleanup_reason: str,
) -> None:
    started = monotonic()
    try:
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        await logger.awarning(
            "job_workspace_cleanup_failed",
            job_id=job_id,
            terminal_status="delivery_confirmed",
            cleanup_reason=cleanup_reason,
            files_deleted=0,
            directories_deleted=0,
            bytes_reclaimed=0,
            duration_seconds=round(monotonic() - started, 6),
            failed_paths_count=1,
        )
        return
    await logger.ainfo(
        "job_workspace_file_deleted",
        job_id=job_id,
        terminal_status="delivery_confirmed",
        cleanup_reason=cleanup_reason,
        files_deleted=1,
        directories_deleted=0,
        bytes_reclaimed=size,
        duration_seconds=round(monotonic() - started, 6),
        failed_paths_count=0,
        entry_kind="delivered_file",
    )


def render_caption(
    settings: Settings,
    result: DownloadResult,
    bot_username: str = "telegram_media_bot",
) -> str:
    title = sanitize_caption_value(result.title, 768)
    source = sanitize_caption_value(result.source, 128)
    username = sanitize_caption_value(bot_username.lstrip("@"), 64)
    rendered = settings.telegram.caption_template.format(
        title=title,
        source=source,
        bot_username=username,
    )
    attribution = f"@{username}"
    if attribution.casefold() not in rendered.casefold():
        rendered = f"{rendered}\n{attribution}"
    return rendered[:1024]


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


class TrackedFSInputFile(FSInputFile):
    def __init__(
        self,
        path: str | Path,
        *,
        filename: str,
        chunk_size: int,
        on_progress: Callable[[int], None],
        on_finished: Callable[[int], None],
    ) -> None:
        super().__init__(path, filename=filename, chunk_size=chunk_size)
        self._on_progress = on_progress
        self._on_finished = on_finished

    async def read(self, bot: Bot) -> AsyncGenerator[bytes]:
        transferred = 0
        async for chunk in super().read(bot):
            yield chunk
            transferred += len(chunk)
            self._on_progress(transferred)
        self._on_finished(transferred)


async def _finalization_heartbeat(
    stream_finished: asyncio.Event,
    request_finished: asyncio.Event,
    *,
    interval_seconds: float,
    emit: Callable[[], None],
) -> None:
    stream_wait = asyncio.create_task(stream_finished.wait())
    request_wait = asyncio.create_task(request_finished.wait())
    done, pending = await asyncio.wait(
        {stream_wait, request_wait},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if request_wait in done or request_finished.is_set():
        return
    while not request_finished.is_set():
        try:
            await asyncio.wait_for(request_finished.wait(), timeout=interval_seconds)
        except TimeoutError:
            emit()
