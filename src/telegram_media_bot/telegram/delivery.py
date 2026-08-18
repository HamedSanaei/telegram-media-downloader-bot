from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from telegram_media_bot.application.ports.delivery import (
    BatchDeliveryOutcome,
    DeliveryCancellationCheck,
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
    DownloadArtifact,
    DownloadResult,
    ImageDeliveryMode,
    MediaKind,
)
from telegram_media_bot.infrastructure.archive.multipart_zip import MultipartZipBuilder

logger = structlog.get_logger(__name__)
_UNSAFE_FILENAME = re.compile(r"[^\w.()\- ]+", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
TELEGRAM_MEDIA_GROUP_MAX_ITEMS = 10


@dataclass(frozen=True, slots=True)
class InstagramDeliveryBatch:
    start_ordinal: int
    artifacts: tuple[DownloadArtifact, ...]


def chunk_media_items(
    items: tuple[DownloadArtifact, ...],
    *,
    max_items: int = TELEGRAM_MEDIA_GROUP_MAX_ITEMS,
) -> tuple[tuple[DownloadArtifact, ...], ...]:
    if max_items < 1 or max_items > TELEGRAM_MEDIA_GROUP_MAX_ITEMS:
        raise ValueError("Invalid Telegram media-group chunk size")
    return tuple(items[start : start + max_items] for start in range(0, len(items), max_items))


def build_instagram_delivery_batches(
    artifacts: tuple[DownloadArtifact, ...],
    mode: ImageDeliveryMode,
) -> tuple[InstagramDeliveryBatch, ...]:
    if not artifacts:
        raise DeliveryError("Instagram delivery plan is empty")
    if any(item.kind not in {MediaKind.IMAGE, MediaKind.VIDEO} for item in artifacts):
        raise DeliveryError("Instagram delivery plan contains unsupported media")
    groups: list[tuple[DownloadArtifact, ...]] = []
    if mode is ImageDeliveryMode.PHOTO:
        groups.extend(chunk_media_items(artifacts))
    else:
        run: list[DownloadArtifact] = []
        run_is_image: bool | None = None
        for artifact in artifacts:
            is_image = artifact.kind is MediaKind.IMAGE
            if run and is_image != run_is_image:
                groups.extend(chunk_media_items(tuple(run)))
                run = []
            run.append(artifact)
            run_is_image = is_image
        groups.extend(chunk_media_items(tuple(run)))
    batches: list[InstagramDeliveryBatch] = []
    ordinal = 1
    for group in groups:
        batches.append(InstagramDeliveryBatch(ordinal, group))
        ordinal += len(group)
    return tuple(batches)


class _AlbumRejectedError(DeliveryError):
    """Telegram rejected the album shape before accepting delivery."""


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
        is_cancelled: DeliveryCancellationCheck | None = None,
    ) -> DeliveryReceipt:
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError
        limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        if result.file_size_bytes > limit:
            raise DeliveryTooLargeError("File exceeds the configured Telegram upload limit")
        filename_source = (
            result.file_path.stem
            if result.image_delivery_mode is ImageDeliveryMode.DOCUMENT
            and result.kind is MediaKind.IMAGE
            else result.title
        )
        filename = sanitize_filename(
            filename_source,
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

    async def deliver_batch(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
        is_cancelled: DeliveryCancellationCheck | None = None,
        summary_title: str = "📚 دانلود مجموعه تمام شد",
    ) -> BatchDeliveryOutcome:
        """Deliver every item with per-item isolation and a final summary."""
        return await _deliver_batch(
            deliver_item=self.deliver,
            summary_sender=lambda text: self.send_text(chat_id, text),
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
            is_cancelled=is_cancelled,
            summary_title=summary_title,
        )

    async def deliver_album(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        item_delivered: DeliveryItemSink | None = None,
    ) -> DeliveryReceipt:
        """Send one Telegram-compatible collection atomically as a media group."""
        media: list[Any] = []
        methods: list[DeliveryMethod] = []
        for index, artifact in enumerate(result.artifacts):
            filename_source = (
                artifact.file_path.stem
                if result.image_delivery_mode is ImageDeliveryMode.DOCUMENT
                and artifact.kind is MediaKind.IMAGE
                else artifact.title or result.title
            )
            filename = sanitize_filename(
                filename_source,
                suffix=artifact.file_path.suffix,
                max_length=self._settings.telegram.filename_max_length,
            )
            upload = FSInputFile(artifact.file_path, filename=filename)
            item_caption = caption if index == 0 and caption else None
            if (
                result.image_delivery_mode is ImageDeliveryMode.DOCUMENT
                and artifact.kind is MediaKind.IMAGE
            ):
                media.append(InputMediaDocument(media=upload, caption=item_caption))
                methods.append(DeliveryMethod.DOCUMENT)
            elif artifact.kind is MediaKind.IMAGE and artifact.mime_type in {
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                media.append(InputMediaPhoto(media=upload, caption=item_caption))
                methods.append(DeliveryMethod.PHOTO)
            elif artifact.kind is MediaKind.VIDEO and artifact.inline_video_streamable:
                media.append(
                    InputMediaVideo(media=upload, caption=item_caption, supports_streaming=True)
                )
                methods.append(DeliveryMethod.VIDEO)
            else:
                raise DeliveryError("Collection is not Telegram album compatible")
        try:
            messages = await self._bot.send_media_group(
                chat_id=chat_id,
                media=media,
                request_timeout=self._settings.telegram.upload_timeout_seconds,
            )
        except TelegramBadRequest as exc:
            raise _AlbumRejectedError("Telegram rejected album delivery") from exc
        except TelegramAPIError as exc:
            raise DeliveryError("Telegram album delivery failed") from exc
        if len(messages) != len(methods):
            raise DeliveryError("Telegram album response item count changed")
        items: list[DeliveryItemReceipt] = []
        for ordinal, (message, method) in enumerate(zip(messages, methods, strict=True), start=1):
            item = replace(_receipt(message, method).primary, ordinal=ordinal)
            items.append(item)
            if item_delivered is not None:
                await item_delivered(item)
        return DeliveryReceipt(items=tuple(items))

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
        if method is DeliveryMethod.PHOTO:
            return await self._bot.send_photo(
                chat_id=chat_id,
                photo=upload,
                caption=caption,
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
            result.image_delivery_mode is ImageDeliveryMode.DOCUMENT
            and result.kind is MediaKind.IMAGE
        ):
            return DeliveryMethod.DOCUMENT
        if (
            (
                self._settings.telegram.upload_as_document
                and result.kind is MediaKind.VIDEO
                and is_document_delivery_compatible(result)
            )
            or result.kind is MediaKind.PLAYLIST
            or result.file_path.suffix.casefold() == ".webm"
        ):
            return DeliveryMethod.DOCUMENT
        if result.kind is MediaKind.AUDIO:
            return DeliveryMethod.AUDIO
        if result.kind is MediaKind.IMAGE and result.mime_type in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }:
            return DeliveryMethod.PHOTO
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
        is_cancelled: DeliveryCancellationCheck | None = None,
    ) -> DeliveryReceipt:
        if result.image_delivery_mode is not None and result.source.casefold() == "instagram":
            return await self._deliver_instagram_media(
                chat_id=chat_id,
                result=result,
                caption=caption,
                progress=progress,
                item_delivered=item_delivered,
                is_cancelled=is_cancelled,
            )
        if result.artifacts:
            return await self._deliver_artifacts(
                chat_id=chat_id,
                result=result,
                caption=caption,
                progress=progress,
                item_delivered=item_delivered,
                is_cancelled=is_cancelled,
            )
        return await self._deliver_one(
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
            is_cancelled=is_cancelled,
        )

    async def _deliver_one(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
        is_cancelled: DeliveryCancellationCheck | None,
    ) -> DeliveryReceipt:
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError
        direct_limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        if result.file_size_bytes <= direct_limit:
            return await self._direct.deliver(
                chat_id=chat_id,
                result=result,
                caption=caption,
                progress=progress,
                item_delivered=item_delivered,
                is_cancelled=is_cancelled,
            )
        if (
            result.image_delivery_mode is ImageDeliveryMode.DOCUMENT
            and result.kind is MediaKind.IMAGE
        ):
            raise DeliveryTooLargeError("Original image exceeds the direct Telegram upload limit")
        if not self._settings.multipart.enabled:
            raise DeliveryTooLargeError("Multipart delivery is disabled")
        return await self._deliver_multipart(
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
            is_cancelled=is_cancelled,
        )

    async def _deliver_artifacts(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
        is_cancelled: DeliveryCancellationCheck | None,
    ) -> DeliveryReceipt:
        direct_limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        album_limit = self._settings.gallery_dl.album_max_items
        if (
            len(result.artifacts) >= 2
            and all(artifact.file_size_bytes <= direct_limit for artifact in result.artifacts)
            and all(
                (
                    artifact.kind is MediaKind.IMAGE
                    and artifact.mime_type
                    in {
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                    }
                )
                or (artifact.kind is MediaKind.VIDEO and artifact.inline_video_streamable)
                for artifact in result.artifacts
            )
        ):
            album_receipts: list[DeliveryItemReceipt] = []
            start = 0
            while start < len(result.artifacts):
                if is_cancelled is not None and is_cancelled():
                    raise asyncio.CancelledError
                end = min(start + album_limit, len(result.artifacts))
                if len(result.artifacts) - end == 1:
                    end -= 1
                chunk = result.artifacts[start:end]
                chunk_result = replace(
                    result,
                    artifacts=chunk,
                    file_path=chunk[0].file_path,
                    file_size_bytes=sum(item.file_size_bytes for item in chunk),
                )

                async def persist_album_item(
                    item: DeliveryItemReceipt,
                    *,
                    offset: int = start,
                ) -> None:
                    if item_delivered is not None:
                        await item_delivered(replace(item, ordinal=offset + item.ordinal))

                try:
                    chunk_receipt = await self._direct.deliver_album(
                        chat_id=chat_id,
                        result=chunk_result,
                        caption=caption if start == 0 else "",
                        item_delivered=persist_album_item,
                    )
                except _AlbumRejectedError:
                    if start:
                        raise
                    await logger.awarning(
                        "telegram_album_delivery_fallback",
                        job_id=result.job_id,
                        asset_count=len(result.artifacts),
                    )
                    break
                album_receipts.extend(
                    replace(item, ordinal=start + item.ordinal) for item in chunk_receipt.items
                )
                for artifact in chunk:
                    await _delete_confirmed_delivery_file(
                        artifact.file_path,
                        job_id=result.job_id,
                        cleanup_reason="album_delivered",
                    )
                start = end
            if start == len(result.artifacts):
                return DeliveryReceipt(items=tuple(album_receipts))
        receipts: list[DeliveryItemReceipt] = []
        completed_bytes = 0
        total_bytes = result.total_file_size_bytes
        for artifact_index, artifact in enumerate(result.artifacts, start=1):
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError
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
                caption=f"{caption}\nرسانه {artifact_index} از {len(result.artifacts)}",  # noqa: RUF001
                progress=map_progress,
                item_delivered=persist_item,
                is_cancelled=is_cancelled,
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

    async def _deliver_instagram_media(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
        is_cancelled: DeliveryCancellationCheck | None,
    ) -> DeliveryReceipt:
        mode = result.image_delivery_mode
        if mode is None:
            raise DeliveryError("Instagram image delivery mode is missing")
        artifacts = result.delivery_artifacts
        batches = build_instagram_delivery_batches(artifacts, mode)
        direct_limit = self._settings.telegram.max_upload_size_mb * 1024 * 1024
        receipts: list[DeliveryItemReceipt] = []
        completed_bytes = 0
        for batch in batches:
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError
            can_group = (
                len(batch.artifacts) >= 2
                and all(item.file_size_bytes <= direct_limit for item in batch.artifacts)
                and _instagram_album_compatible(batch.artifacts, mode)
            )
            if can_group:
                chunk_result = replace(
                    result,
                    file_path=batch.artifacts[0].file_path,
                    file_size_bytes=sum(item.file_size_bytes for item in batch.artifacts),
                    artifacts=batch.artifacts,
                )

                async def persist_album_item(
                    item: DeliveryItemReceipt,
                    *,
                    start_ordinal: int = batch.start_ordinal,
                ) -> None:
                    if item_delivered is not None:
                        await item_delivered(
                            replace(
                                item,
                                ordinal=start_ordinal + item.ordinal - 1,
                            )
                        )

                try:
                    receipt = await self._direct.deliver_album(
                        chat_id=chat_id,
                        result=chunk_result,
                        caption=caption if batch.start_ordinal == 1 else "",
                        item_delivered=persist_album_item,
                    )
                except _AlbumRejectedError:
                    receipt = await self._deliver_instagram_items_individually(
                        chat_id=chat_id,
                        result=result,
                        batch=batch,
                        caption=caption,
                        completed_bytes=completed_bytes,
                        progress=progress,
                        item_delivered=item_delivered,
                        is_cancelled=is_cancelled,
                    )
                else:
                    receipt = DeliveryReceipt(
                        items=tuple(
                            replace(
                                item,
                                ordinal=batch.start_ordinal + item.ordinal - 1,
                            )
                            for item in receipt.items
                        )
                    )
                    for artifact in batch.artifacts:
                        await _delete_confirmed_delivery_file(
                            artifact.file_path,
                            job_id=result.job_id,
                            cleanup_reason="instagram_album_delivered",
                        )
            else:
                receipt = await self._deliver_instagram_items_individually(
                    chat_id=chat_id,
                    result=result,
                    batch=batch,
                    caption=caption,
                    completed_bytes=completed_bytes,
                    progress=progress,
                    item_delivered=item_delivered,
                    is_cancelled=is_cancelled,
                )
            receipts.extend(receipt.items)
            completed_bytes += sum(item.file_size_bytes for item in batch.artifacts)
        if len(receipts) != len(artifacts):
            raise DeliveryError("Instagram delivery receipt count changed")
        return DeliveryReceipt(items=tuple(receipts))

    async def _deliver_instagram_items_individually(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        batch: InstagramDeliveryBatch,
        caption: str,
        completed_bytes: int,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
        is_cancelled: DeliveryCancellationCheck | None,
    ) -> DeliveryReceipt:
        receipts: list[DeliveryItemReceipt] = []
        batch_completed = 0
        total_bytes = result.total_file_size_bytes
        for offset, artifact in enumerate(batch.artifacts):
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError
            ordinal = batch.start_ordinal + offset
            child = DownloadResult(
                job_id=result.job_id,
                media_id=result.media_id,
                title=artifact.title or artifact.file_path.stem,
                source=result.source,
                kind=artifact.kind,
                file_path=artifact.file_path,
                file_size_bytes=artifact.file_size_bytes,
                duration_seconds=result.duration_seconds,
                mime_type=artifact.mime_type,
                inline_video_streamable=artifact.inline_video_streamable,
                image_delivery_mode=result.image_delivery_mode,
            )

            def map_progress(
                event: DeliveryProgressEvent,
                *,
                already_delivered: int = completed_bytes + batch_completed,
                item_ordinal: int = ordinal,
            ) -> None:
                if progress is not None:
                    progress(
                        replace(
                            event,
                            transferred_bytes=min(
                                total_bytes,
                                already_delivered + event.item_transferred_bytes,
                            ),
                            total_bytes=total_bytes,
                            item_ordinal=item_ordinal,
                            item_count=len(result.delivery_artifacts),
                        )
                    )

            async def persist_item(
                item: DeliveryItemReceipt,
                *,
                item_ordinal: int = ordinal,
            ) -> None:
                if item_delivered is not None:
                    await item_delivered(replace(item, ordinal=item_ordinal))

            receipt = await self._deliver_one(
                chat_id=chat_id,
                result=child,
                caption=caption if ordinal == 1 else "",
                progress=map_progress,
                item_delivered=persist_item,
                is_cancelled=is_cancelled,
            )
            receipts.extend(replace(item, ordinal=ordinal) for item in receipt.items)
            await _delete_confirmed_delivery_file(
                artifact.file_path,
                job_id=result.job_id,
                cleanup_reason="instagram_item_delivered",
            )
            batch_completed += artifact.file_size_bytes
        return DeliveryReceipt(items=tuple(receipts))

    async def send_text(self, chat_id: int, text: str) -> int:
        return await self._direct.send_text(chat_id, text)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        await self._direct.edit_text(chat_id, message_id, text)

    async def deliver_batch(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
        is_cancelled: DeliveryCancellationCheck | None = None,
        summary_title: str = "📚 دانلود مجموعه تمام شد",
    ) -> BatchDeliveryOutcome:
        """Deliver a collection item by item; one failure never discards the successful items."""
        return await _deliver_batch(
            deliver_item=self._deliver_one,
            summary_sender=lambda text: self.send_text(chat_id, text),
            chat_id=chat_id,
            result=result,
            caption=caption,
            progress=progress,
            item_delivered=item_delivered,
            is_cancelled=is_cancelled,
            summary_title=summary_title,
        )

    async def _deliver_multipart(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None,
        item_delivered: DeliveryItemSink | None,
        is_cancelled: DeliveryCancellationCheck | None,
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
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError
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
                is_cancelled=is_cancelled,
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


def _instagram_album_compatible(
    artifacts: tuple[DownloadArtifact, ...],
    mode: ImageDeliveryMode,
) -> bool:
    if mode is ImageDeliveryMode.DOCUMENT:
        # Bot API document albums cannot be mixed with photo/video album items.
        return all(item.kind is MediaKind.IMAGE for item in artifacts) or all(
            item.kind is MediaKind.VIDEO and item.inline_video_streamable for item in artifacts
        )
    return all(
        (
            item.kind is MediaKind.IMAGE
            and item.mime_type in {"image/jpeg", "image/png", "image/webp"}
        )
        or (item.kind is MediaKind.VIDEO and item.inline_video_streamable)
        for item in artifacts
    )


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


def render_batch_summary(title: str, total: int, succeeded: int, failed: int) -> str:
    return f"{title}\n\nکل: {total}\nموفق: {succeeded}\nناموفق: {failed}"  # noqa: RUF001


async def _deliver_batch(
    *,
    deliver_item: Callable[..., Awaitable[DeliveryReceipt]],
    summary_sender: Callable[[str], Awaitable[object]],
    chat_id: int,
    result: DownloadResult,
    caption: str,
    progress: DeliveryProgressSink | None,
    item_delivered: DeliveryItemSink | None,
    is_cancelled: DeliveryCancellationCheck | None,
    summary_title: str,
) -> BatchDeliveryOutcome:
    artifacts = result.delivery_artifacts
    receipts: list[DeliveryItemReceipt] = []
    succeeded = 0
    failed = 0
    completed_bytes = 0
    total_bytes = result.total_file_size_bytes
    for index, artifact in enumerate(artifacts, start=1):
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError
        child = DownloadResult(
            job_id=result.job_id,
            media_id=result.media_id,
            title=artifact.title or f"{result.title} {index}",
            source=result.source,
            kind=artifact.kind,
            file_path=artifact.file_path,
            file_size_bytes=artifact.file_size_bytes,
            duration_seconds=result.duration_seconds,
            mime_type=artifact.mime_type,
            inline_video_streamable=artifact.inline_video_streamable,
        )
        # Stable per-item ordinal identity: the artifact position, not the delivery order,
        # so partial failures never renumber successful items.
        ordinal_offset = index - 1

        def map_progress(
            event: DeliveryProgressEvent,
            *,
            completed: int = completed_bytes,
            item_number: int = index,
        ) -> None:
            if progress is None:
                return
            progress(
                replace(
                    event,
                    transferred_bytes=min(total_bytes, completed + event.item_transferred_bytes),
                    total_bytes=total_bytes,
                    item_ordinal=item_number,
                    item_count=len(artifacts),
                )
            )

        async def persist_item(
            item: DeliveryItemReceipt,
            *,
            offset: int = ordinal_offset,
        ) -> None:
            if item_delivered is not None:
                await item_delivered(replace(item, ordinal=offset + item.ordinal))

        try:
            child_receipt = await deliver_item(
                chat_id=chat_id,
                result=child,
                caption=f"{caption}\nرسانه {index} از {len(artifacts)}",  # noqa: RUF001
                progress=map_progress,
                item_delivered=persist_item,
                is_cancelled=is_cancelled,
            )
        except asyncio.CancelledError:
            raise
        except DeliveryError as exc:
            failed += 1
            await logger.awarning(
                "batch_item_delivery_failed",
                job_id=result.job_id,
                item_ordinal=index,
                error_type=type(exc).__name__,
            )
            continue
        succeeded += 1
        receipts.extend(
            replace(item, ordinal=ordinal_offset + item.ordinal) for item in child_receipt.items
        )
        await _delete_confirmed_delivery_file(
            artifact.file_path,
            job_id=result.job_id,
            cleanup_reason="batch_item_delivered",
        )
        completed_bytes += artifact.file_size_bytes
    summary = render_batch_summary(summary_title, len(artifacts), succeeded, failed)
    try:
        await summary_sender(summary)
    except DeliveryError as exc:
        await logger.awarning(
            "batch_summary_failed",
            job_id=result.job_id,
            error_type=type(exc).__name__,
        )
    return BatchDeliveryOutcome(
        total=len(artifacts),
        succeeded=succeeded,
        failed=failed,
        receipts=tuple(receipts),
        delivered_bytes=completed_bytes,
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
    media: Any = message.audio or message.video or message.document
    if media is None and message.photo:
        media = message.photo[-1]
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
