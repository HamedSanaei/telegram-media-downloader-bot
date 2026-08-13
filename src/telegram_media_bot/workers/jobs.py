from __future__ import annotations

import asyncio
import secrets
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import structlog
import structlog.contextvars
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.user_repository import UserRepository
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.application.services.instagram_delivery import (
    requires_instagram_image_confirmation,
)
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.application.services.progress import (
    DeliveryProgressThrottler,
    ProgressThrottler,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    CollectionTooLargeError,
    DeliveryError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlUnavailableError,
    GalleryDlUnsupportedUrlError,
    ImageValidationError,
    JobCancelledError,
    MediaBotError,
    MediaTooLargeError,
    MediaUnavailableError,
    NativeFormatUnavailableError,
    PlaylistNotAllowedError,
    RateLimitedError,
    TranscodeRejectedError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryItemReceipt,
    DeliveryItemRecord,
    DeliveryItemStatus,
    DeliveryProgressEvent,
    DeliveryStage,
    DownloadMode,
    ErrorCategory,
    ImageDeliveryMode,
    JobId,
    JobKind,
    JobStatus,
    NativeVideoCodec,
    OutputContainer,
    ProgressEvent,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.storage.workspace import (
    cleanup_job_workspace,
    sweep_workspaces,
)
from telegram_media_bot.telegram.delivery import render_caption
from telegram_media_bot.telegram.texts import (
    AUTH_REQUIRED_TEXT,
    CANCELLED_TEXT,
    COLLECTION_LIMIT_TEXT,
    COLLECTION_TOO_LARGE_TEXT,
    DELIVERY_UNCERTAIN_TEXT,
    FAILED_TEXT,
    GALLERY_COOKIES_EXPIRED_TEXT,
    GALLERY_EXTRACTION_TEXT,
    GALLERY_OUTPUT_CHANGED_TEXT,
    GALLERY_UNAVAILABLE_TEXT,
    INVALID_IMAGE_TEXT,
    MEDIA_TOO_LARGE_TEXT,
    MEDIA_UNAVAILABLE_TEXT,
    NATIVE_FORMAT_UNAVAILABLE_TEXT,
    PROVIDER_RATE_LIMIT_TEXT,
    TRANSCODE_REJECTED_TEXT,
    UNSUPPORTED_GALLERY_URL_TEXT,
)
from telegram_media_bot.telegram.ui import (
    container_keyboard,
    instagram_image_delivery_keyboard,
    media_bundle_keyboard,
    render_delivery_progress,
    render_instagram_image_delivery_prompt,
    render_media_info,
    render_progress,
)

logger = structlog.get_logger(__name__)


async def process_inspection_job(
    ctx: dict[str, Any],
    *,
    chat_id: int,
    user_id: int,
    url: str,
) -> str:
    url = canonicalize_media_url(url).canonical_url
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    service = cast(DownloadService, ctx["download_service"])
    bot = cast(Bot, ctx["bot"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    job_id = JobId(str(ctx.get("job_id") or "unknown"))
    structlog.contextvars.bind_contextvars(request_id=str(job_id), job_id=str(job_id))
    record = repository.get_job(job_id)
    attempt = int(ctx.get("job_try") or 1)
    started = monotonic()
    await logger.ainfo(
        "inspection_started", job_id=job_id, user_id=user_id, chat_id=chat_id, attempt=attempt
    )
    try:
        if record is None:
            raise RuntimeError("Durable inspection record is missing")
        if record.status is JobStatus.SUCCEEDED:
            return str(job_id)
        if record.status is JobStatus.CANCELLED:
            return str(job_id)
        if repository.is_cancel_requested(job_id):
            raise JobCancelledError("Inspection was cancelled")
        repository.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        info = await asyncio.to_thread(service.inspect, url)
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.RUNNING, source=info.source, attempt=attempt
        )
        now = datetime.now(UTC)
        if (
            info.source.casefold() == "instagram"
            and not info.assets
            and settings.media.instagram.auto_download
        ):
            instagram_container, instagram_policy = _instagram_download_contract(settings)
            download, created = await asyncio.to_thread(
                JobService(repository).create_download,
                chat_id=chat_id,
                user_id=user_id,
                url=info.webpage_url,
                mode=DownloadMode.BEST_ORIGINAL,
                container=instagram_container,
                container_policy=instagram_policy,
            )
            status_message_id = await _edit_or_send_inspection_message(
                bot=bot,
                chat_id=chat_id,
                message_id=record.status_message_id,
                text="بهترین نسخهٔ اصلی اینستاگرام برای دریافت آماده شد و در صف قرار گرفت.",
            )
            await asyncio.to_thread(
                repository.set_status_message,
                download.job_id,
                status_message_id,
            )
            queue = cast(JobQueue, ctx["queue"])
            await queue.enqueue_download(
                job_id=download.job_id,
                chat_id=chat_id,
                user_id=user_id,
                url=info.webpage_url,
                mode=DownloadMode.BEST_ORIGINAL,
                container=instagram_container,
                container_policy=instagram_policy,
            )
            await asyncio.to_thread(
                repository.transition,
                job_id,
                JobStatus.SUCCEEDED,
                source=info.source,
            )
            metrics.record_job(outcome="inspection_succeeded", source=info.source)
            await logger.ainfo(
                "instagram_download_enqueued",
                job_id=job_id,
                download_job_id=download.job_id,
                download_created=created,
            )
            return str(job_id)
        if info.assets:
            allowed_modes = tuple(dict.fromkeys(option.mode for option in info.format_options))
            if not allowed_modes:
                raise NativeFormatUnavailableError(
                    "No media-bundle delivery plan is available for this post"
                )
            selection = SelectionRecord(
                token=SelectionToken(secrets.token_urlsafe(15)),
                owner_user_id=user_id,
                chat_id=chat_id,
                media=info,
                allowed_modes=allowed_modes,
                created_at=now,
                expires_at=now + timedelta(seconds=settings.persistence.selection_ttl_seconds),
            )
            await asyncio.to_thread(repository.save_selection, selection)
            status_message_id = await _edit_or_send_inspection_message(
                bot=bot,
                chat_id=chat_id,
                message_id=record.status_message_id,
                text=(
                    render_instagram_image_delivery_prompt(info)
                    if requires_instagram_image_confirmation(info)
                    else render_media_info(info)
                ),
                reply_markup=(
                    instagram_image_delivery_keyboard(selection)
                    if requires_instagram_image_confirmation(info)
                    else media_bundle_keyboard(selection)
                ),
            )
            await asyncio.to_thread(repository.set_status_message, job_id, status_message_id)
            await asyncio.to_thread(
                repository.transition, job_id, JobStatus.SUCCEEDED, source=info.source
            )
            metrics.record_job(outcome="inspection_succeeded", source=info.source)
            await logger.ainfo(
                "media_bundle_inspection_completed",
                job_id=job_id,
                source=info.source,
                asset_count=len(info.assets),
            )
            return str(job_id)
        catalog = build_native_option_catalog(info)
        artwork_modes = {
            DownloadMode.YOUTUBE_THUMBNAIL,
            DownloadMode.SOUNDCLOUD_ARTWORK,
        }
        allowed_modes = tuple(
            dict.fromkeys(
                [option.mode for option in catalog.options]
                + [option.mode for option in info.format_options if option.mode in artwork_modes]
            )
        )
        if not allowed_modes:
            raise NativeFormatUnavailableError(
                "No native codec/container plan is available for the configured modes"
            )
        for container in (OutputContainer.MP4, OutputContainer.WEBM, OutputContainer.MP3):
            visible = catalog.for_container(container)
            if not visible:
                continue
            await logger.ainfo(
                "native_options_built",
                job_id=job_id,
                source=info.source,
                container=container.value,
                raw_candidate_count=catalog.raw_candidate_count,
                planned_option_count=catalog.planned_option_count,
                deduplicated_option_count=len(visible),
                hidden_transcode_option_count=catalog.hidden_transcode_option_count,
                unknown_size_option_count=sum(option.size_bytes is None for option in visible),
                options=[
                    {
                        "option_id": option.option_id,
                        "selected_format_ids": option.selected_format_ids,
                        "actual_height": option.actual_height,
                        "actual_width": option.actual_width,
                        "actual_fps": option.actual_fps,
                        "actual_size_bytes": option.size_bytes,
                        "video_filesize": next(
                            (
                                item.video_size_bytes
                                for item in info.format_options
                                if item.selected_format_ids == option.selected_format_ids
                                and item.container is option.container
                            ),
                            None,
                        ),
                        "audio_filesize": next(
                            (
                                item.audio_size_bytes
                                for item in info.format_options
                                if item.selected_format_ids == option.selected_format_ids
                                and item.container is option.container
                            ),
                            None,
                        ),
                        "final_size_bytes": option.size_bytes,
                        "size_is_approximate": option.size_is_approximate,
                        "video_codec": option.video_codec,
                        "audio_codec": option.audio_codec,
                        "transcode_required": option.transcode_required,
                    }
                    for option in visible
                ],
            )
        selection = SelectionRecord(
            token=SelectionToken(secrets.token_urlsafe(15)),
            owner_user_id=user_id,
            chat_id=chat_id,
            media=info,
            allowed_modes=allowed_modes,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.persistence.selection_ttl_seconds),
        )
        await asyncio.to_thread(repository.save_selection, selection)
        text = render_media_info(info, catalog=catalog)
        status_message_id = await _edit_or_send_inspection_message(
            bot=bot,
            chat_id=chat_id,
            message_id=record.status_message_id,
            text=text,
            reply_markup=container_keyboard(selection, catalog),
        )
        await asyncio.to_thread(repository.set_status_message, job_id, status_message_id)
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.SUCCEEDED, source=info.source
        )
        metrics.record_job(outcome="inspection_succeeded", source=info.source)
        await logger.ainfo("inspection_completed", job_id=job_id, source=info.source)
        return str(job_id)
    except JobCancelledError:
        newly_cancelled = await asyncio.to_thread(
            repository.finalize_cancelled, job_id, source="user"
        )
        if newly_cancelled:
            metrics.record_job(outcome="cancelled", error=ErrorCategory.CANCELLED.value)
        await logger.ainfo(
            "inspection_cancelled",
            job_id=job_id,
            cancel_requested=True,
            cancel_source="user",
            final_status=JobStatus.CANCELLED.value,
            state_changed=newly_cancelled,
        )
        return str(job_id)
    except MediaBotError as exc:
        await _handle_controlled_failure(ctx, job_id, chat_id, exc, attempt)
        return str(job_id)
    except asyncio.CancelledError:
        if await asyncio.to_thread(repository.is_cancel_requested, job_id):
            await logger.ainfo(
                "inspection_cancelled",
                job_id=job_id,
                cancel_requested=True,
                cancel_source="user",
                final_status=JobStatus.CANCELLED.value,
                state_changed=False,
            )
            return str(job_id)
        await logger.awarning(
            "inspection_worker_shutdown",
            job_id=job_id,
            cancel_source="shutdown",
            final_status=record.status.value if record is not None else None,
        )
        raise
    except Exception as exc:
        if await asyncio.to_thread(repository.is_cancel_requested, job_id):
            await asyncio.to_thread(repository.finalize_cancelled, job_id, source="user")
            return str(job_id)
        if attempt < settings.queue.max_tries:
            await asyncio.to_thread(
                repository.transition,
                job_id,
                JobStatus.RETRYING,
                error_category=ErrorCategory.INTERNAL,
                error_summary=type(exc).__name__,
                attempt=attempt,
            )
            raise Retry(defer=settings.queue.retry_delay_seconds) from exc
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.FAILED,
            error_category=ErrorCategory.INTERNAL,
            error_summary=type(exc).__name__,
            attempt=attempt,
        )
        metrics.record_job(outcome="failed", error=ErrorCategory.INTERNAL.value)
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        await _notify_admins_of_terminal_failure(
            ctx,
            job_id=job_id,
            kind=JobKind.INSPECTION,
            source=(
                terminal_record.source
                if (terminal_record := repository.get_job(job_id)) is not None
                else None
            ),
            status=JobStatus.FAILED,
            category=ErrorCategory.INTERNAL,
            attempt=attempt,
        )
        await logger.aexception("inspection_unexpected_failure", job_id=job_id)
        return str(job_id)
    finally:
        metrics.observe_duration(monotonic() - started)
        structlog.contextvars.clear_contextvars()


async def _edit_or_send_inspection_message(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return message_id
        except TelegramAPIError as exc:
            await logger.awarning(
                "inspection_status_edit_failed",
                chat_id=chat_id,
                error_type=type(exc).__name__,
                fallback="send_message",
            )
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )
    return message.message_id


async def process_download_job(
    ctx: dict[str, Any],
    *,
    chat_id: int,
    user_id: int,
    url: str,
    mode: str,
    container: str | None = None,
    container_policy: str = ContainerPolicy.NATIVE_ONLY.value,
    native_video_codec: str | None = None,
    selected_format_ids: list[str] | tuple[str, ...] | None = None,
    image_delivery_mode: str | None = None,
) -> str:
    url = canonicalize_media_url(url).canonical_url
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    service = cast(DownloadService, ctx["download_service"])
    delivery = cast(DeliveryGateway, ctx["delivery"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    job_id = JobId(str(ctx.get("job_id") or "unknown"))
    structlog.contextvars.bind_contextvars(request_id=str(job_id), job_id=str(job_id))
    output_directory = settings.storage.downloads_path() / str(job_id)
    temp_directory = settings.storage.temp_path() / str(job_id)
    attempt = int(ctx.get("job_try") or 1)
    started = monotonic()
    record = repository.get_job(job_id)
    local_cancel = threading.Event()
    progress_queue: asyncio.Queue[ProgressEvent | DeliveryProgressEvent | None] = asyncio.Queue(
        maxsize=1
    )
    reporter: asyncio.Task[None] | None = None
    cleanup_allowed = True
    cleanup_complete = False
    cleanup_reason = "job_finalizer"
    loop = asyncio.get_running_loop()
    last_delivery_progress: DeliveryProgressEvent | None = None

    def progress_sink(event: ProgressEvent) -> None:
        loop.call_soon_threadsafe(_offer_progress, progress_queue, event)

    def delivery_progress_sink(event: DeliveryProgressEvent) -> None:
        nonlocal last_delivery_progress
        last_delivery_progress = event
        loop.call_soon_threadsafe(_offer_progress, progress_queue, event)

    async def persist_delivery_item(item: DeliveryItemReceipt) -> None:
        try:
            await asyncio.to_thread(
                repository.upsert_delivery_item,
                DeliveryItemRecord(
                    job_id=job_id,
                    ordinal=item.ordinal,
                    provider=item.provider,
                    status=DeliveryItemStatus.DELIVERED,
                    method=item.method,
                    recipient_message_id=item.message_id,
                    file_id=item.file_id,
                    file_unique_id=item.file_unique_id,
                ),
            )
        except Exception as exc:
            raise DeliveryError("Delivery receipt persistence failed") from exc

    cancellation = _CancellationProbe(repository, job_id, local_cancel)
    await logger.ainfo(
        "download_started", job_id=job_id, user_id=user_id, chat_id=chat_id, attempt=attempt
    )
    try:
        if record is None:
            raise RuntimeError("Durable download record is missing")
        if record.status is JobStatus.SUCCEEDED and record.delivery_file_id:
            await logger.ainfo("download_idempotent_skip", job_id=job_id)
            return str(job_id)
        if record.status is JobStatus.CANCELLED:
            await logger.ainfo(
                "download_cancelled_skip",
                job_id=job_id,
                cancel_requested=True,
                final_status=JobStatus.CANCELLED.value,
            )
            return str(job_id)
        if record.status is JobStatus.DELIVERY_UNCERTAIN:
            return str(job_id)
        if cancellation():
            raise JobCancelledError("Download was cancelled")
        selected_mode = DownloadMode(mode)
        selected_container = OutputContainer(container) if container else None
        selected_policy = ContainerPolicy(container_policy)
        selected_native_video_codec = (
            NativeVideoCodec(native_video_codec) if native_video_codec else None
        )
        selected_ids = tuple(selected_format_ids or record.selected_format_ids)
        selected_image_delivery_mode = (
            ImageDeliveryMode(image_delivery_mode)
            if image_delivery_mode
            else record.image_delivery_mode
        )
        repository.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        reporter = asyncio.create_task(
            _report_progress(
                progress_queue, record.chat_id, record.status_message_id, delivery, settings
            )
        )
        common_download_arguments: dict[str, Any] = {
            "job_id": job_id,
            "url": url,
            "mode": selected_mode,
            "output_directory": output_directory,
            "temp_directory": temp_directory,
            "progress": progress_sink if record.status_message_id is not None else None,
            "is_cancelled": cancellation,
        }
        if selected_image_delivery_mode is not None:
            common_download_arguments["image_delivery_mode"] = selected_image_delivery_mode
        if selected_ids:
            common_download_arguments["selected_format_ids"] = selected_ids
        if selected_container is not None:
            common_download_arguments.update(
                container=selected_container,
                container_policy=selected_policy,
                native_video_codec=selected_native_video_codec,
            )
        download_task = asyncio.create_task(
            asyncio.to_thread(service.download, **common_download_arguments)
        )
        try:
            result = await asyncio.shield(download_task)
        except asyncio.CancelledError:
            local_cancel.set()
            try:
                with suppress(JobCancelledError, MediaBotError):
                    await asyncio.wait_for(
                        download_task,
                        timeout=settings.yt_dlp.socket_timeout_seconds + 10,
                    )
            except TimeoutError:
                cleanup_allowed = False
                download_task.add_done_callback(_consume_task_exception)
            if await asyncio.to_thread(repository.is_cancel_requested, job_id):
                raise JobCancelledError("Download was cancelled by the user") from None
            raise
        if cancellation():
            raise JobCancelledError("Download was cancelled before delivery")
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.DELIVERING, source=result.source
        )
        receipt = await delivery.deliver(
            chat_id=chat_id,
            result=result,
            caption=render_caption(
                settings,
                result,
                str(ctx.get("bot_username") or "telegram_media_bot"),
            ),
            progress=delivery_progress_sink,
            item_delivered=persist_delivery_item,
            is_cancelled=cancellation,
        )
        if cancellation():
            raise JobCancelledError("Download was cancelled during delivery")
        if settings.storage.delete_after_upload and settings.media.workspace.cleanup_on_success:
            cleanup = await asyncio.to_thread(
                cleanup_job_workspace,
                settings,
                job_id,
                terminal_status=JobStatus.SUCCEEDED.value,
                cleanup_reason="delivery_confirmed",
            )
            metrics.record_workspace_cleanup(
                files_deleted=cleanup.files_deleted,
                directories_deleted=cleanup.directories_deleted,
                bytes_reclaimed=cleanup.bytes_reclaimed,
                failed_paths=cleanup.failed_paths_count,
                duration_seconds=cleanup.duration_seconds,
            )
            cleanup_complete = cleanup.failed_paths_count == 0
        try:
            await asyncio.to_thread(
                repository.complete_download,
                job_id,
                user_id=user_id,
                day=datetime.now(UTC).date(),
                source=result.source,
                delivery_file_id=receipt.file_id,
                delivery_file_unique_id=receipt.file_unique_id,
                attempt=attempt,
                delivered_bytes=result.total_file_size_bytes,
            )
        except JobCancelledError:
            raise
        except Exception as exc:
            raise DeliveryError("Atomic delivery completion persistence failed") from exc
        metrics.add_bytes(result.total_file_size_bytes)
        metrics.record_job(outcome="succeeded", source=result.source)
        if record.status_message_id is not None:
            await _safe_edit(
                delivery, chat_id, record.status_message_id, "دانلود و ارسال با موفقیت انجام شد."
            )
        await logger.ainfo(
            "download_completed",
            job_id=job_id,
            source=result.source,
            file_size_bytes=result.file_size_bytes,
            delivery_method=receipt.method.value,
            delivery_items=len(receipt.items),
        )
        return str(job_id)
    except JobCancelledError:
        cleanup_reason = "cancellation"
        newly_cancelled = await asyncio.to_thread(
            repository.finalize_cancelled, job_id, source="user"
        )
        if newly_cancelled:
            metrics.record_job(outcome="cancelled", error=ErrorCategory.CANCELLED.value)
            await _notify(
                ctx,
                chat_id,
                record.status_message_id if record else None,
                CANCELLED_TEXT,
            )
        await logger.ainfo(
            "download_cancelled",
            job_id=job_id,
            previous_status=record.status.value if record else None,
            cancel_requested=True,
            cancel_source="user",
            final_status=JobStatus.CANCELLED.value,
            state_changed=newly_cancelled,
        )
        return str(job_id)
    except DeliveryError as exc:
        cleanup_reason = "delivery_failure"
        quarantine_persisted = True
        try:
            await asyncio.to_thread(
                repository.transition,
                job_id,
                JobStatus.DELIVERY_UNCERTAIN,
                error_category=ErrorCategory.DELIVERY_UNCERTAIN,
                error_summary=type(exc).__name__,
                attempt=attempt,
            )
        except Exception:
            quarantine_persisted = False
        metrics.record_job(
            outcome="delivery_uncertain", error=ErrorCategory.DELIVERY_UNCERTAIN.value
        )
        await _notify(
            ctx,
            chat_id,
            record.status_message_id if record else None,
            DELIVERY_UNCERTAIN_TEXT,
        )
        await _notify_admins_of_terminal_failure(
            ctx,
            job_id=job_id,
            kind=JobKind.DOWNLOAD,
            source=(
                terminal_record.source
                if (terminal_record := repository.get_job(job_id)) is not None
                else None
            ),
            status=JobStatus.DELIVERY_UNCERTAIN,
            category=ErrorCategory.DELIVERY_UNCERTAIN,
            attempt=attempt,
        )
        progress_fields: dict[str, object] = {}
        if last_delivery_progress is not None:
            progress_fields = {
                "stage": last_delivery_progress.stage.value,
                "item_ordinal": last_delivery_progress.item_ordinal,
                "item_count": last_delivery_progress.item_count,
                "transferred_bytes": last_delivery_progress.transferred_bytes,
                "total_bytes": last_delivery_progress.total_bytes,
                "percent": (
                    last_delivery_progress.percent
                    if last_delivery_progress.stage is DeliveryStage.UPLOADING
                    else None
                ),
                "elapsed_seconds": round(last_delivery_progress.elapsed_seconds, 1),
            }
        await logger.awarning(
            "download_delivery_uncertain",
            job_id=job_id,
            error_type=type(exc).__name__,
            quarantine_persisted=quarantine_persisted,
            **progress_fields,
        )
        return str(job_id)
    except MediaBotError as exc:
        cleanup_reason = "timeout" if "timed out" in str(exc).casefold() else "controlled_failure"
        await _handle_controlled_failure(ctx, job_id, chat_id, exc, attempt)
        return str(job_id)
    except asyncio.CancelledError:
        cancel_requested = await asyncio.to_thread(repository.is_cancel_requested, job_id)
        if cancel_requested:
            cleanup_reason = "cancellation"
            await asyncio.to_thread(repository.finalize_cancelled, job_id, source="user")
            await logger.ainfo(
                "download_cancelled",
                job_id=job_id,
                cancel_requested=True,
                cancel_source="user",
                final_status=JobStatus.CANCELLED.value,
            )
            return str(job_id)
        await logger.awarning(
            "download_worker_shutdown",
            job_id=job_id,
            cancel_requested=False,
            cancel_source="shutdown",
            final_status=record.status.value if record else None,
        )
        raise
    except Exception as exc:
        cleanup_reason = "unexpected_failure"
        if await asyncio.to_thread(repository.is_cancel_requested, job_id):
            await asyncio.to_thread(repository.finalize_cancelled, job_id, source="user")
            return str(job_id)
        if attempt < settings.queue.max_tries:
            await asyncio.to_thread(
                repository.transition,
                job_id,
                JobStatus.RETRYING,
                error_category=ErrorCategory.INTERNAL,
                error_summary=type(exc).__name__,
                attempt=attempt,
            )
            raise Retry(defer=settings.queue.retry_delay_seconds) from exc
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.FAILED,
            error_category=ErrorCategory.INTERNAL,
            error_summary=type(exc).__name__,
            attempt=attempt,
        )
        metrics.record_job(outcome="failed", error=ErrorCategory.INTERNAL.value)
        await _record_failed_usage(repository, job_id, user_id)
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        await _notify_admins_of_terminal_failure(
            ctx,
            job_id=job_id,
            kind=JobKind.DOWNLOAD,
            source=(
                terminal_record.source
                if (terminal_record := repository.get_job(job_id)) is not None
                else None
            ),
            status=JobStatus.FAILED,
            category=ErrorCategory.INTERNAL,
            attempt=attempt,
        )
        await logger.aexception("download_unexpected_failure", job_id=job_id)
        return str(job_id)
    finally:
        local_cancel.set()
        if reporter is not None:
            await progress_queue.put(None)
            await reporter
        if cleanup_allowed and not cleanup_complete:
            current_record = await asyncio.to_thread(repository.get_job, job_id)
            current_status = (
                current_record.status if current_record is not None else JobStatus.FAILED
            )
            if settings.storage.delete_after_upload and _cleanup_enabled(
                settings, current_status, cleanup_reason
            ):
                cleanup = await asyncio.to_thread(
                    cleanup_job_workspace,
                    settings,
                    job_id,
                    terminal_status=current_status.value,
                    cleanup_reason=cleanup_reason,
                )
                metrics.record_workspace_cleanup(
                    files_deleted=cleanup.files_deleted,
                    directories_deleted=cleanup.directories_deleted,
                    bytes_reclaimed=cleanup.bytes_reclaimed,
                    failed_paths=cleanup.failed_paths_count,
                    duration_seconds=cleanup.duration_seconds,
                )
        metrics.observe_duration(monotonic() - started)
        structlog.contextvars.clear_contextvars()


async def maintenance_job(ctx: dict[str, Any]) -> int:
    settings = cast(Settings, ctx["settings"])
    last_run = float(ctx.get("maintenance_last_run") or 0.0)
    current = monotonic()
    if current - last_run < settings.persistence.cleanup_interval_seconds:
        return 0
    ctx["maintenance_last_run"] = current
    repository = cast(JobRepository, ctx["repository"])
    now = datetime.now(UTC)
    purged = await asyncio.to_thread(
        repository.purge_expired, now, settings.storage.job_retention_days
    )
    cleanup = await asyncio.to_thread(
        sweep_workspaces,
        settings,
        repository,
        now,
        cleanup_reason="maintenance",
    )
    metrics = cast(MetricsRegistry, ctx["metrics"])
    metrics.record_workspace_cleanup(
        files_deleted=cleanup.files_deleted,
        directories_deleted=cleanup.directories_deleted,
        bytes_reclaimed=cleanup.bytes_reclaimed,
        failed_paths=cleanup.failed_paths_count,
        duration_seconds=cleanup.duration_seconds,
    )
    await logger.ainfo(
        "maintenance_completed",
        purged_records=purged,
        removed_directories=cleanup.directories_deleted,
        bytes_reclaimed=cleanup.bytes_reclaimed,
        failed_paths_count=cleanup.failed_paths_count,
    )
    return purged + cleanup.directories_deleted


async def _handle_controlled_failure(
    ctx: dict[str, Any], job_id: JobId, chat_id: int, exc: MediaBotError, attempt: int
) -> None:
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    record = repository.get_job(job_id)
    category = error_category(exc)
    if await asyncio.to_thread(repository.is_cancel_requested, job_id):
        await asyncio.to_thread(repository.finalize_cancelled, job_id, source="user")
        await logger.ainfo(
            "job_cancelled",
            job_id=job_id,
            cancel_requested=True,
            cancel_source="user",
            final_status=JobStatus.CANCELLED.value,
        )
        return
    if exc.retryable and attempt < settings.queue.max_tries:
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.RETRYING,
            error_category=category,
            error_summary=type(exc).__name__,
            attempt=attempt,
        )
        raise Retry(defer=settings.queue.retry_delay_seconds) from exc
    await asyncio.to_thread(
        repository.transition,
        job_id,
        JobStatus.FAILED,
        error_category=category,
        error_summary=type(exc).__name__,
        attempt=attempt,
    )
    metrics.record_job(outcome="failed", error=category.value)
    if record is not None and record.kind is JobKind.DOWNLOAD:
        await _record_failed_usage(repository, job_id, record.user_id)
    await _notify(
        ctx,
        chat_id,
        record.status_message_id if record else None,
        _controlled_failure_text(exc),
    )
    await _notify_admins_of_terminal_failure(
        ctx,
        job_id=job_id,
        kind=record.kind if record is not None else JobKind.DOWNLOAD,
        source=record.source if record is not None else None,
        status=JobStatus.FAILED,
        category=category,
        attempt=attempt,
    )
    await logger.awarning(
        "job_controlled_failure",
        job_id=job_id,
        error_category=category.value,
        error_type=type(exc).__name__,
        error_reason=_controlled_failure_reason(exc),
        source=record.source if record is not None else None,
        attempt=attempt,
    )


def _controlled_failure_reason(exc: MediaBotError) -> str:
    if isinstance(exc, NativeFormatUnavailableError):
        return "native_codec_container_unavailable"
    return error_category(exc).value


async def _report_progress(
    queue: asyncio.Queue[ProgressEvent | DeliveryProgressEvent | None],
    chat_id: int,
    message_id: int | None,
    delivery: DeliveryGateway,
    settings: Settings,
) -> None:
    download_throttler = ProgressThrottler(
        min_interval_seconds=settings.telegram.progress_min_interval_seconds,
        min_percent_delta=settings.telegram.progress_min_percent_delta,
    )
    delivery_throttler = DeliveryProgressThrottler(
        min_interval_seconds=settings.telegram.progress_min_interval_seconds,
        min_percent_delta=settings.telegram.progress_min_percent_delta,
    )
    last_text: str | None = None
    while True:
        event = await queue.get()
        if event is None:
            return
        if isinstance(event, DeliveryProgressEvent):
            if not delivery_throttler.should_emit(event):
                continue
            text = render_delivery_progress(event)
            await logger.ainfo(
                "delivery_progress",
                job_id=event.job_id,
                stage=event.stage.value,
                item_ordinal=event.item_ordinal,
                item_count=event.item_count,
                transferred_bytes=event.transferred_bytes,
                total_bytes=event.total_bytes,
                percent=event.percent if event.stage is DeliveryStage.UPLOADING else None,
                elapsed_seconds=round(event.elapsed_seconds, 1),
            )
        elif download_throttler.should_emit(event):
            text = render_progress(
                event.percent,
                event.downloaded_bytes,
                event.total_bytes,
                status=event.status,
            )
        else:
            continue
        if message_id is not None and text != last_text:
            await _safe_edit(delivery, chat_id, message_id, text)
            last_text = text


async def _notify_failure(ctx: dict[str, Any], chat_id: int, message_id: int | None) -> None:
    await _notify(ctx, chat_id, message_id, FAILED_TEXT)


async def _record_failed_usage(
    repository: JobRepository,
    job_id: JobId,
    user_id: int,
) -> None:
    users = cast(UserRepository, repository)
    await asyncio.to_thread(
        users.record_download_outcome,
        job_id=job_id,
        user_id=user_id,
        day=datetime.now(UTC).date(),
        succeeded=False,
    )


def _controlled_failure_text(exc: MediaBotError) -> str:
    if isinstance(exc, GalleryDlCookiesExpiredError):
        return GALLERY_COOKIES_EXPIRED_TEXT
    if isinstance(exc, GalleryDlExtractionError):
        return GALLERY_EXTRACTION_TEXT
    if isinstance(exc, RateLimitedError):
        return PROVIDER_RATE_LIMIT_TEXT
    if isinstance(exc, CollectionTooLargeError):
        return COLLECTION_TOO_LARGE_TEXT
    if isinstance(exc, ImageValidationError):
        return INVALID_IMAGE_TEXT
    if isinstance(exc, GalleryDlOutputChangedError):
        return GALLERY_OUTPUT_CHANGED_TEXT
    if isinstance(exc, GalleryDlUnavailableError):
        return GALLERY_UNAVAILABLE_TEXT
    if isinstance(exc, GalleryDlUnsupportedUrlError):
        return UNSUPPORTED_GALLERY_URL_TEXT
    if isinstance(exc, AuthenticationRequiredError):
        return AUTH_REQUIRED_TEXT
    if isinstance(exc, MediaTooLargeError):
        return MEDIA_TOO_LARGE_TEXT
    if isinstance(exc, PlaylistNotAllowedError):
        return COLLECTION_LIMIT_TEXT
    if isinstance(exc, NativeFormatUnavailableError):
        return NATIVE_FORMAT_UNAVAILABLE_TEXT
    if isinstance(exc, MediaUnavailableError):
        return MEDIA_UNAVAILABLE_TEXT
    if isinstance(exc, TranscodeRejectedError):
        return TRANSCODE_REJECTED_TEXT
    return FAILED_TEXT


async def _notify(ctx: dict[str, Any], chat_id: int, message_id: int | None, text: str) -> None:
    delivery = cast(DeliveryGateway, ctx["delivery"])
    if message_id is not None:
        await _safe_edit(delivery, chat_id, message_id, text)
        return
    try:
        await delivery.send_text(chat_id, text)
    except DeliveryError as exc:
        await logger.awarning("telegram_notification_failed", error_type=type(exc).__name__)


async def _notify_admins_of_terminal_failure(
    ctx: dict[str, Any],
    *,
    job_id: JobId,
    kind: JobKind,
    source: str | None,
    status: JobStatus,
    category: ErrorCategory,
    attempt: int,
) -> None:
    settings = cast(Settings, ctx["settings"])
    admin_ids = tuple(dict.fromkeys(settings.telegram.admin_ids))
    if not admin_ids:
        return
    bot = cast(Bot, ctx["bot"])
    text = (
        "🚨 خطای نهایی پردازش\n"
        f"شناسه کار: {job_id}\n"
        f"نوع کار: {kind.value}\n"
        f"منبع: {_safe_admin_source(source)}\n"
        f"وضعیت: {status.value}\n"
        f"دسته خطا: {category.value}\n"
        f"تلاش: {attempt}"
    )
    results = await asyncio.gather(
        *(bot.send_message(chat_id=admin_id, text=text) for admin_id in admin_ids),
        return_exceptions=True,
    )
    failures = tuple(result for result in results if isinstance(result, BaseException))
    log_fields = {
        "job_id": job_id,
        "job_kind": kind.value,
        "status": status.value,
        "error_category": category.value,
        "attempt": attempt,
        "recipient_count": len(admin_ids),
        "sent_count": len(admin_ids) - len(failures),
        "failed_count": len(failures),
    }
    if failures:
        await logger.awarning(
            "admin_failure_notification_incomplete",
            **log_fields,
            error_types=sorted({type(failure).__name__ for failure in failures}),
        )
        return
    await logger.ainfo("admin_failure_notification_completed", **log_fields)


def _safe_admin_source(source: str | None) -> str:
    if source is None or not 1 <= len(source) <= 32:
        return "unknown"
    if not all(
        character.isascii() and (character.isalnum() or character in "_-") for character in source
    ):
        return "unknown"
    return source.casefold()


async def _safe_edit(delivery: DeliveryGateway, chat_id: int, message_id: int, text: str) -> None:
    try:
        await delivery.edit_text(chat_id, message_id, text)
    except DeliveryError as exc:
        await logger.awarning("telegram_edit_failed", error_type=type(exc).__name__)


class _CancellationProbe:
    def __init__(
        self,
        repository: JobRepository,
        job_id: JobId,
        local_cancel: threading.Event,
    ) -> None:
        self._repository = repository
        self._job_id = job_id
        self._local_cancel = local_cancel
        self._last_check = 0.0
        self._cached = False

    def __call__(self) -> bool:
        if self._local_cancel.is_set():
            return True
        now = monotonic()
        if now - self._last_check >= 0.5:
            self._cached = self._repository.is_cancel_requested(self._job_id)
            self._last_check = now
        return self._cached


def _instagram_download_contract(
    settings: Settings,
) -> tuple[OutputContainer | None, ContainerPolicy]:
    container = OutputContainer.MP4 if settings.media.instagram.force_mp4 else None
    return container, ContainerPolicy.NATIVE_ONLY


def _offer_progress(
    queue: asyncio.Queue[ProgressEvent | DeliveryProgressEvent | None],
    event: ProgressEvent | DeliveryProgressEvent | None,
) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


def _cleanup_enabled(settings: Settings, status: JobStatus, cleanup_reason: str) -> bool:
    policy = settings.media.workspace
    if not status.terminal:
        return False
    if cleanup_reason == "timeout":
        return policy.cleanup_on_timeout
    if status is JobStatus.SUCCEEDED:
        return policy.cleanup_on_success
    if status is JobStatus.CANCELLED:
        return policy.cleanup_on_cancel
    return policy.cleanup_on_failure
