from __future__ import annotations

import asyncio
import secrets
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
import structlog.contextvars
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from arq import Retry

from telegram_media_bot.application.ports.delivery import (
    DeliveryGateway,
)
from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.user_repository import UserRepository
from telegram_media_bot.application.services.cookie_health_service import (
    CookieHealthAlert,
    CookieHealthService,
)
from telegram_media_bot.application.services.diagnostic_sanitizer import (
    sanitize_exception_message,
)
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
from telegram_media_bot.domain.cookie_health import (
    BLOCKING_COOKIE_STATES,
    CookieHealthState,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    BatchDeliveryFailedError,
    CollectionTooLargeError,
    DeliveryError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlUnavailableError,
    GalleryDlUnsupportedUrlError,
    ImageValidationError,
    InstagramCookiesUnavailableError,
    JobCancelledError,
    MediaBotError,
    MediaTooLargeError,
    MediaUnavailableError,
    NativeFormatUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
    TranscodeRejectedError,
)
from telegram_media_bot.domain.failures import (
    FailureContext,
    FailureStage,
    render_failure_notification,
)
from telegram_media_bot.domain.models import (
    COLLECTION_MODES,
    ContainerPolicy,
    DeliveryItemReceipt,
    DeliveryItemRecord,
    DeliveryItemStatus,
    DeliveryProgressEvent,
    DeliveryStage,
    DownloadMode,
    ErrorCategory,
    HighlightTrayRecord,
    ImageDeliveryMode,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaKind,
    NativeVideoCodec,
    OutputContainer,
    ProgressEvent,
    SelectionRecord,
    SelectionToken,
    StoryDeliveryMode,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.storage.workspace import (
    cleanup_job_workspace,
    sweep_workspaces,
)
from telegram_media_bot.telegram.delivery import render_batch_summary, render_caption
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
    INSTAGRAM_COOKIES_BLOCKED_TEXT,
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
    highlight_tray_keyboard,
    instagram_image_delivery_keyboard,
    media_bundle_keyboard,
    render_delivery_progress,
    render_highlight_tray,
    render_instagram_image_delivery_prompt,
    render_media_info,
    render_progress,
    story_choice_keyboard,
)

logger = structlog.get_logger(__name__)


def _project_version() -> str:
    try:
        return version("telegram-media-downloader-bot")
    except PackageNotFoundError:
        return "1.3.8"


APP_VERSION = _project_version()

#: In-memory per-job retry history (best effort; the durable record keeps the attempt count).
_FAILURE_HISTORY: dict[str, list[str]] = {}


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
            intent = canonicalize_media_url(url)
            now = datetime.now(UTC)
            if intent.instagram_kind == "story":
                # Part C: after a successful exact-story inspection the user chooses between the
                # exact story item and every active story of that account. Never bulk-download
                # automatically.
                story_selection = SelectionRecord(
                    token=SelectionToken(secrets.token_urlsafe(15)),
                    owner_user_id=user_id,
                    chat_id=chat_id,
                    media=info,
                    allowed_modes=tuple(
                        dict.fromkeys(option.mode for option in info.format_options)
                    ),
                    created_at=now,
                    expires_at=now + timedelta(seconds=settings.persistence.selection_ttl_seconds),
                )
                await asyncio.to_thread(repository.save_selection, story_selection)
                status_message_id = await _edit_or_send_inspection_message(
                    bot=bot,
                    chat_id=chat_id,
                    message_id=record.status_message_id,
                    text=render_media_info(info),
                    reply_markup=story_choice_keyboard(story_selection),
                )
                await asyncio.to_thread(repository.set_status_message, job_id, status_message_id)
                await asyncio.to_thread(
                    repository.transition, job_id, JobStatus.SUCCEEDED, source=info.source
                )
                metrics.record_job(outcome="inspection_succeeded", source=info.source)
                await logger.ainfo(
                    "story_choice_published",
                    job_id=job_id,
                    source=info.source,
                    media_count=len(info.assets),
                )
                return str(job_id)
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
            highlights_username = (
                _instagram_username_from_url(info.webpage_url)
                if record.url_classification == "profile"
                else None
            )
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
                    instagram_image_delivery_keyboard(
                        selection, highlights_username=highlights_username
                    )
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
        await asyncio.to_thread(
            repository.record_recoverable_failure,
            job_id,
            ErrorCategory.INTERNAL,
            APP_VERSION,
        )
        metrics.record_job(outcome="failed", error=ErrorCategory.INTERNAL.value)
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        context = _build_failure_context(
            ctx,
            job_id=job_id,
            kind=JobKind.INSPECTION,
            exc=exc,
            attempt=attempt,
            stage=FailureStage.INSPECTION,
            started=started,
        )
        await _notify_admins_of_terminal_failure(ctx, context=context, status=JobStatus.FAILED)
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


async def process_highlight_tray_job(
    ctx: dict[str, Any],
    *,
    chat_id: int,
    user_id: int,
    url: str,
    username: str,
) -> str:
    """Fetch one Instagram highlight tray and publish the browsable highlight list (Part D)."""
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    engine = ctx.get("gallery_engine") or ctx.get("engine")
    bot = cast(Bot, ctx["bot"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    job_id = JobId(str(ctx.get("job_id") or "unknown"))
    structlog.contextvars.bind_contextvars(request_id=str(job_id), job_id=str(job_id))
    record = repository.get_job(job_id)
    attempt = int(ctx.get("job_try") or 1)
    started = monotonic()
    await logger.ainfo(
        "highlight_tray_started",
        job_id=job_id,
        user_id=user_id,
        chat_id=chat_id,
        username=username,
        attempt=attempt,
    )
    try:
        if record is None:
            raise RuntimeError("Durable highlight-tray record is missing")
        if record.status is JobStatus.SUCCEEDED:
            return str(job_id)
        if record.status is JobStatus.CANCELLED:
            return str(job_id)
        if repository.is_cancel_requested(job_id):
            raise JobCancelledError("Highlight tray fetch was cancelled")
        repository.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        fetch = getattr(engine, "fetch_highlight_tray", None)
        if not callable(fetch):
            raise RuntimeError("Gallery highlight tray fetch is unavailable")
        highlights = await asyncio.to_thread(
            fetch,
            username,
            max_highlights=settings.media.instagram.max_highlight_items,
        )
        if not highlights:
            raise MediaUnavailableError("Instagram account has no highlights")
        now = datetime.now(UTC)
        tray = HighlightTrayRecord(
            token=SelectionToken(secrets.token_urlsafe(15)),
            owner_user_id=user_id,
            chat_id=chat_id,
            username=username,
            highlights=highlights,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.persistence.selection_ttl_seconds),
        )
        await asyncio.to_thread(repository.save_highlight_tray, tray)
        status_message_id = await _edit_or_send_inspection_message(
            bot=bot,
            chat_id=chat_id,
            message_id=record.status_message_id,
            text=render_highlight_tray(tray, page=1),
            reply_markup=highlight_tray_keyboard(tray, page=1),
        )
        await asyncio.to_thread(repository.set_status_message, job_id, status_message_id)
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.SUCCEEDED, source="instagram"
        )
        metrics.record_job(outcome="inspection_succeeded", source="instagram")
        await logger.ainfo(
            "highlight_tray_completed",
            job_id=job_id,
            username=username,
            highlight_count=len(highlights),
        )
        return str(job_id)
    except JobCancelledError:
        await asyncio.to_thread(repository.finalize_cancelled, job_id, source="user")
        await logger.ainfo("highlight_tray_cancelled", job_id=job_id)
        return str(job_id)
    except MediaBotError as exc:
        await _handle_controlled_failure(ctx, job_id, chat_id, exc, attempt)
        return str(job_id)
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
        await asyncio.to_thread(
            repository.record_recoverable_failure,
            job_id,
            ErrorCategory.INTERNAL,
            APP_VERSION,
        )
        metrics.record_job(outcome="failed", error=ErrorCategory.INTERNAL.value)
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        context = _build_failure_context(
            ctx,
            job_id=job_id,
            kind=JobKind.HIGHLIGHT_TRAY,
            exc=exc,
            attempt=attempt,
            stage=FailureStage.EXTRACTION,
            started=started,
        )
        await _notify_admins_of_terminal_failure(ctx, context=context, status=JobStatus.FAILED)
        await logger.aexception("highlight_tray_unexpected_failure", job_id=job_id)
        return str(job_id)
    finally:
        metrics.observe_duration(monotonic() - started)
        structlog.contextvars.clear_contextvars()


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
    story_delivery_mode: str | None = None,
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
        selected_story_delivery_mode = (
            StoryDeliveryMode(story_delivery_mode)
            if story_delivery_mode
            else record.story_delivery_mode
        )
        _gate_collection_cookies(ctx, selected_mode)
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
        if (
            selected_mode is DownloadMode.INSTAGRAM_ALL_STORIES
            and selected_story_delivery_mode is not None
        ):
            result = replace(result, story_delivery_mode=selected_story_delivery_mode)
        if selected_mode in COLLECTION_MODES:
            summary_title = (
                "📚 دانلود استوری‌ها تمام شد"  # noqa: RUF001
                if selected_mode is DownloadMode.INSTAGRAM_ALL_STORIES
                else "⭐ دانلود هایلایت تمام شد"
            )
            batch = await delivery.deliver_batch(
                chat_id=chat_id,
                result=result,
                caption=render_caption(
                    settings,
                    result,
                    str(ctx.get("bot_username") or "telegram_media_bot"),
                ),
                source_url=record.url,
                progress=delivery_progress_sink,
                item_delivered=persist_delivery_item,
                is_cancelled=cancellation,
                summary_title=summary_title,
            )
            if batch.succeeded == 0:
                raise BatchDeliveryFailedError("Every collection item failed to deliver")
            if cancellation():
                raise JobCancelledError("Download was cancelled during delivery")
            try:
                await asyncio.to_thread(
                    repository.complete_download,
                    job_id,
                    user_id=user_id,
                    day=datetime.now(UTC).date(),
                    source=result.source,
                    delivery_file_id=batch.receipts[0].file_id if batch.receipts else None,
                    delivery_file_unique_id=(
                        batch.receipts[0].file_unique_id if batch.receipts else None
                    ),
                    attempt=attempt,
                    delivered_bytes=batch.delivered_bytes,
                )
            except JobCancelledError:
                raise
            except Exception as exc:
                raise DeliveryError("Atomic delivery completion persistence failed") from exc
            metrics.add_bytes(batch.delivered_bytes)
            metrics.record_job(outcome="succeeded", source=result.source)
            if record.status_message_id is not None:
                await _safe_edit(
                    delivery,
                    chat_id,
                    record.status_message_id,
                    render_batch_summary(summary_title, batch.total, batch.succeeded, batch.failed),
                )
            await logger.ainfo(
                "batch_download_completed",
                job_id=job_id,
                source=result.source,
                total=batch.total,
                succeeded=batch.succeeded,
                failed=batch.failed,
                delivered_bytes=batch.delivered_bytes,
            )
            return str(job_id)
        receipt = await delivery.deliver(
            chat_id=chat_id,
            result=result,
            caption=render_caption(
                settings,
                result,
                str(ctx.get("bot_username") or "telegram_media_bot"),
            ),
            source_url=record.url,
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
        context = _build_failure_context(
            ctx,
            job_id=job_id,
            kind=JobKind.DOWNLOAD,
            exc=exc,
            attempt=attempt,
            stage=FailureStage.DELIVERY,
            started=started,
            category=ErrorCategory.DELIVERY_UNCERTAIN,
        )
        await _notify_admins_of_terminal_failure(
            ctx, context=context, status=JobStatus.DELIVERY_UNCERTAIN
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
        await asyncio.to_thread(
            repository.record_recoverable_failure,
            job_id,
            ErrorCategory.INTERNAL,
            APP_VERSION,
        )
        metrics.record_job(outcome="failed", error=ErrorCategory.INTERNAL.value)
        await _record_failed_usage(repository, job_id, user_id)
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        context = _build_failure_context(
            ctx,
            job_id=job_id,
            kind=JobKind.DOWNLOAD,
            exc=exc,
            attempt=attempt,
            stage=FailureStage.DOWNLOAD,
            started=started,
        )
        await _notify_admins_of_terminal_failure(ctx, context=context, status=JobStatus.FAILED)
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
    metrics = cast(MetricsRegistry, ctx["metrics"])
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
    metrics.record_workspace_cleanup(
        files_deleted=cleanup.files_deleted,
        directories_deleted=cleanup.directories_deleted,
        bytes_reclaimed=cleanup.bytes_reclaimed,
        failed_paths=cleanup.failed_paths_count,
        duration_seconds=cleanup.duration_seconds,
    )
    total_removed = purged + cleanup.directories_deleted
    log_context: dict[str, object] = {
        "purged_records": purged,
        "removed_directories": cleanup.directories_deleted,
        "bytes_reclaimed": cleanup.bytes_reclaimed,
        "failed_paths_count": cleanup.failed_paths_count,
    }

    # Inbound-update retention: bounded purge of terminal history + stuck visibility.
    inbound_store = ctx.get("inbound_updates")
    if inbound_store is not None:
        inbox = settings.operations.inbound_updates
        purged_inbound = await asyncio.to_thread(
            inbound_store.purge_retention,
            now,
            completed_retention_days=inbox.completed_retention_days,
            terminal_failure_retention_days=inbox.terminal_failure_retention_days,
            batch_size=inbox.cleanup_batch_size,
        )
        stuck = await asyncio.to_thread(
            inbound_store.stuck_count,
            now - timedelta(minutes=inbox.stuck_after_minutes),
        )
        metrics.record_inbound_purged(purged_inbound)
        metrics.set_inbound_stuck(stuck)
        total_removed += purged_inbound
        log_context["purged_inbound_updates"] = purged_inbound
        log_context["inbound_updates_stuck"] = stuck
        if stuck:
            await logger.awarning(
                "inbound_updates_stuck",
                stuck_updates=stuck,
                older_than_minutes=inbox.stuck_after_minutes,
            )

    # Side-effect ledger: quarantine stale reservations before purging terminal history.
    effect_store = ctx.get("effect_ledger")
    if effect_store is not None:
        stale_effects = await asyncio.to_thread(
            effect_store.reconcile_stale_pending,
            now,
            stale_after_minutes=settings.operations.inbound_updates.effect_pending_stale_minutes,
            batch_size=settings.operations.inbound_updates.cleanup_batch_size,
        )
        metrics.record_effects_marked_uncertain(stale_effects)
        effect_states = await asyncio.to_thread(effect_store.state_counts)
        metrics.set_effects_stale_pending(effect_states.get("pending", 0))
        purged_effects = await asyncio.to_thread(
            effect_store.purge_retention,
            now,
            retention_days=settings.operations.inbound_updates.effect_retention_days,
            batch_size=settings.operations.inbound_updates.cleanup_batch_size,
        )
        total_removed += purged_effects
        log_context["purged_effects"] = purged_effects

    # Bounded recovery: keep draining fresh-cookie backlogs gradually (no busy loop).
    recovery_service = ctx.get("recovery_service")
    if recovery_service is not None:
        recovery_summary = await recovery_service.recover_maintenance_batch()
        if recovery_summary.deferred:
            metrics.record_recovery_deferred()
        log_context["recovery_requeued"] = recovery_summary.requeued
        log_context["recovery_discovered"] = recovery_summary.discovered
        log_context["recovery_deferred"] = recovery_summary.deferred

    await logger.ainfo("maintenance_completed", **log_context)
    return total_removed


async def _handle_controlled_failure(
    ctx: dict[str, Any], job_id: JobId, chat_id: int, exc: MediaBotError, attempt: int
) -> None:
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    record = repository.get_job(job_id)
    category = error_category(exc)
    # An adapter that already processed the request must leave its provider attribution on the
    # durable record, so a terminal failure never reports an unknown source.
    exc_source = getattr(exc, "source", None) or (record.source if record is not None else None)
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
            source=exc_source,
            error_category=category,
            error_summary=type(exc).__name__,
            attempt=attempt,
        )
        raise Retry(defer=settings.queue.retry_delay_seconds) from exc
    await asyncio.to_thread(
        repository.transition,
        job_id,
        JobStatus.FAILED,
        source=exc_source,
        error_category=category,
        error_summary=type(exc).__name__,
        attempt=attempt,
    )
    await asyncio.to_thread(repository.record_recoverable_failure, job_id, category, APP_VERSION)
    metrics.record_job(outcome="failed", error=category.value)
    if record is not None and record.kind is JobKind.DOWNLOAD:
        await _record_failed_usage(repository, job_id, record.user_id)
    await _notify(
        ctx,
        chat_id,
        record.status_message_id if record else None,
        _controlled_failure_text(exc),
    )
    context = _build_failure_context(
        ctx,
        job_id=job_id,
        kind=record.kind if record is not None else JobKind.DOWNLOAD,
        exc=exc,
        attempt=attempt,
        stage=_failure_stage_for_exception(exc),
        started=None,
    )
    await _notify_admins_of_terminal_failure(ctx, context=context, status=JobStatus.FAILED)
    await _record_runtime_auth_failure(ctx, exc)
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


def _failure_stage_for_exception(exc: MediaBotError) -> FailureStage:
    if isinstance(
        exc,
        (
            AuthenticationRequiredError,
            GalleryDlCookiesExpiredError,
            InstagramCookiesUnavailableError,
        ),
    ):
        return FailureStage.AUTHENTICATION
    if isinstance(exc, NativeFormatUnavailableError):
        return FailureStage.FORMAT_PLANNING
    if isinstance(exc, (GalleryDlExtractionError, GalleryDlUnsupportedUrlError)):
        return FailureStage.EXTRACTION
    if isinstance(exc, (DeliveryError, BatchDeliveryFailedError)):
        return FailureStage.DELIVERY
    if isinstance(exc, (PostProcessingError, TranscodeRejectedError)):
        return FailureStage.POSTPROCESS
    if isinstance(exc, (MediaTooLargeError, CollectionTooLargeError)):
        return FailureStage.DOWNLOAD
    if isinstance(exc, GalleryDlOutputChangedError):
        return FailureStage.EXTRACTION
    # The failing adapter may attach the precise pipeline stage for everything else (for
    # example inspection/extraction/download attribution from the yt-dlp engine); without a
    # specialized classification above, that hint beats reporting an anonymous "unknown".
    attached = getattr(exc, "failure_stage", None)
    if isinstance(attached, FailureStage):
        return attached
    return FailureStage.UNKNOWN


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
    if isinstance(exc, InstagramCookiesUnavailableError):
        return INSTAGRAM_COOKIES_BLOCKED_TEXT
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
    context: FailureContext,
    status: JobStatus,
) -> None:
    settings = cast(Settings, ctx["settings"])
    admin_ids = tuple(dict.fromkeys(settings.telegram.admin_ids))
    if not admin_ids:
        return
    bot = cast(Bot, ctx["bot"])
    reply_markup = _cookie_health_reply_markup() if _context_is_cookie_failure(context) else None
    text = render_failure_notification(context)
    results = await asyncio.gather(
        *(
            bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup)
            for admin_id in admin_ids
        ),
        return_exceptions=True,
    )
    failures = tuple(result for result in results if isinstance(result, BaseException))
    log_fields = {
        "job_id": context.job_id,
        "job_kind": context.job_kind.value if context.job_kind else None,
        "status": status.value,
        "error_category": context.error_category.value if context.error_category else None,
        "attempt": context.attempt,
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


def _build_failure_context(
    ctx: dict[str, Any],
    *,
    job_id: JobId,
    kind: JobKind,
    exc: BaseException,
    attempt: int,
    stage: FailureStage,
    started: float | None,
    category: ErrorCategory | None = None,
) -> FailureContext:
    settings = cast(Settings, ctx["settings"])
    repository = ctx.get("repository")
    get_job = getattr(repository, "get_job", None)
    record = get_job(job_id) if callable(get_job) else None
    parsed_url = urlsplit(record.url) if record is not None else None
    platform = (parsed_url.hostname or "").casefold() if parsed_url is not None else None
    url_classification = record.url_classification if record is not None else None
    error_key = _failure_key(exc)
    previous = _previous_failures(job_id, error_key)
    _record_failure_attempt(job_id, error_key)
    safe_reason = sanitize_exception_message(str(exc)) if str(exc) else None
    if isinstance(exc, MediaBotError) and not safe_reason:
        safe_reason = error_category(exc).value
    resolved_category = category or (
        error_category(exc) if isinstance(exc, MediaBotError) else ErrorCategory.INTERNAL
    )
    fallback_chain = getattr(exc, "fallback_chain", None)
    return FailureContext(
        job_id=job_id,
        request_id=str(job_id),
        job_kind=kind,
        failure_stage=stage,
        platform=platform,
        url_classification=url_classification,
        adapter=getattr(exc, "adapter", None),
        extractor=getattr(exc, "extractor", None),
        source=getattr(exc, "source", None) or (record.source if record is not None else None),
        fallback_chain=tuple(fallback_chain) if fallback_chain else None,
        fallback_reason=getattr(exc, "fallback_reason", None),
        error_category=resolved_category,
        exception_type=type(exc).__name__,
        safe_error_reason=safe_reason,
        http_status=getattr(exc, "http_status", None),
        retryable=getattr(exc, "retryable", None),
        attempt=attempt,
        max_attempts=settings.queue.max_tries,
        elapsed_seconds=(round(monotonic() - started, 2) if started is not None else None),
        media_kind=_media_kind_from_record(record),
        app_version=APP_VERSION,
        previous_failures=previous,
    )


def _failure_key(exc: BaseException) -> str:
    http_status = getattr(exc, "http_status", None)
    if isinstance(http_status, int):
        return f"HTTP {http_status}"
    return type(exc).__name__


def _record_failure_attempt(job_id: JobId, error_key: str) -> None:
    _FAILURE_HISTORY.setdefault(str(job_id), []).append(error_key)


def _previous_failures(job_id: JobId, error_key: str) -> tuple[str, ...]:
    history = _FAILURE_HISTORY.get(str(job_id), [])
    count = sum(1 for item in history[:-1] if item == error_key)
    if count == 0:
        return ()
    return (f"{error_key} x{count}",)


def _media_kind_from_record(record: JobRecord | None) -> MediaKind | None:
    if record is None or record.mode is None:
        return None
    mode = record.mode
    if mode in COLLECTION_MODES:
        return MediaKind.PLAYLIST
    if mode.value.startswith("video"):
        return MediaKind.VIDEO
    if mode.value.startswith("image") or mode.value.startswith("images"):
        return MediaKind.IMAGE
    if mode.value.startswith("audio"):
        return MediaKind.AUDIO
    return None


def _context_is_cookie_failure(context: FailureContext) -> bool:
    return context.error_category is ErrorCategory.AUTHENTICATION and (
        context.source == "instagram" or context.failure_stage is FailureStage.AUTHENTICATION
    )


def _cookie_health_reply_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍪 سلامت کوکی‌ها",  # noqa: RUF001
                    callback_data="adm:ch:open",
                )
            ]
        ]
    )


def _instagram_username_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    username = parts[0]
    if not username or not all(
        character.isascii() and (character.isalnum() or character in "._") for character in username
    ):
        return None
    return username


def _gate_collection_cookies(ctx: dict[str, Any], mode: DownloadMode) -> None:
    """Fail early before a doomed authenticated Instagram collection job (Part E)."""
    if mode not in COLLECTION_MODES:
        return
    health_service = ctx.get("cookie_health_service")
    if not isinstance(health_service, CookieHealthService):
        return
    current = health_service.all_health().get(CookieService.INSTAGRAM)
    if current is None or current.status not in BLOCKING_COOKIE_STATES:
        return
    raise InstagramCookiesUnavailableError(
        "Instagram cookies are not valid for this collection job", source="instagram"
    )


async def _record_runtime_auth_failure(ctx: dict[str, Any], exc: BaseException) -> None:
    """A real runtime authentication failure updates Cookie Health and alerts admins (Part B)."""
    if not isinstance(exc, (GalleryDlCookiesExpiredError, AuthenticationRequiredError)):
        return
    source = (
        getattr(exc, "source", None)
        or getattr(exc, "extractor", None)
        or ("instagram" if isinstance(exc, GalleryDlCookiesExpiredError) else "")
    ).casefold()
    provider = {
        "youtube": CookieService.YOUTUBE,
        "youtube:tab": CookieService.YOUTUBE,
        "instagram": CookieService.INSTAGRAM,
        "tiktok": CookieService.TIKTOK,
        "twitter": CookieService.TWITTER,
        "x": CookieService.TWITTER,
        "pinterest": CookieService.PINTEREST,
        "soundcloud": CookieService.SOUNDCLOUD,
    }.get(source)
    if provider is None:
        return
    health_service = ctx.get("cookie_health_service")
    if not isinstance(health_service, CookieHealthService):
        return
    alert = health_service.update_from_auth_failure(
        provider,
        safe_reason=sanitize_exception_message(str(exc)) or "authentication failed",
    )
    if alert is not None:
        await _notify_admins_of_cookie_alert(ctx, alert)


async def _notify_admins_of_cookie_alert(
    ctx: dict[str, Any],
    alert: CookieHealthAlert,
) -> None:
    settings = cast(Settings, ctx["settings"])
    admin_ids = tuple(dict.fromkeys(settings.telegram.admin_ids))
    if not admin_ids:
        return
    bot = cast(Bot, ctx["bot"])
    text = _render_cookie_alert_text(alert)
    await asyncio.gather(
        *(
            bot.send_message(
                chat_id=admin_id, text=text, reply_markup=_cookie_health_reply_markup()
            )
            for admin_id in admin_ids
        ),
        return_exceptions=True,
    )
    await logger.ainfo(
        "cookie_health_alert_sent",
        provider=alert.provider.value,
        previous_state=alert.previous_state.value if alert.previous_state else None,
        new_state=alert.new_state.value,
        recovery=alert.recovery,
        reminder=alert.reminder,
        recipient_count=len(admin_ids),
    )


def _render_cookie_alert_text(alert: CookieHealthAlert) -> str:
    state_label = _cookie_state_label(alert.new_state)
    if alert.recovery:
        return f"✅ کوکی‌های {_cookie_provider_label(alert.provider)} سالم شدند ({state_label})."
    if alert.reminder:
        return (
            f"🔄 یادآوری: کوکی‌های {_cookie_provider_label(alert.provider)} هنوز "
            f"{state_label} هستند."
        )
    return f"🍪 وضعیت کوکی‌های {_cookie_provider_label(alert.provider)} تغییر کرد: {state_label}."


def _cookie_state_label(state: CookieHealthState) -> str:
    return {
        CookieHealthState.HEALTHY: "سالم ✅",
        CookieHealthState.EXPIRING_SOON: "در حال انقضا ⚠️",
        CookieHealthState.EXPIRED: "منقضی ❌",
        CookieHealthState.AUTH_FAILED: "ورود نامعتبر ❌",
        CookieHealthState.MISSING: "ناموجود ❌",
        CookieHealthState.MALFORMED: "خراب ❌",
        CookieHealthState.UNVERIFIED: "تأییدنشده ❓",
        CookieHealthState.CHECK_ERROR: "خطای بررسی ⚠️",
    }[state]


def _cookie_provider_label(provider: CookieService) -> str:
    from telegram_media_bot.domain.cookies import COOKIE_SERVICE_LABELS

    return COOKIE_SERVICE_LABELS.get(provider, provider.value)


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
