from __future__ import annotations

import asyncio
import secrets
import shutil
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, cast

import structlog
import structlog.contextvars
from aiogram import Bot
from arq import Retry

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.application.services.progress import ProgressThrottler
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DeliveryError,
    JobCancelledError,
    MediaBotError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    ErrorCategory,
    JobId,
    JobStatus,
    MediaKind,
    ProgressEvent,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.telegram.delivery import render_caption
from telegram_media_bot.telegram.texts import CANCELLED_TEXT, FAILED_TEXT
from telegram_media_bot.telegram.ui import render_media_info, render_progress, selection_keyboard

logger = structlog.get_logger(__name__)


async def process_inspection_job(
    ctx: dict[str, Any],
    *,
    chat_id: int,
    user_id: int,
    url: str,
) -> str:
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
        if repository.is_cancel_requested(job_id):
            raise JobCancelledError("Inspection was cancelled")
        repository.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        info = await asyncio.to_thread(service.inspect, url)
        now = datetime.now(UTC)
        selection = SelectionRecord(
            token=SelectionToken(secrets.token_urlsafe(15)),
            owner_user_id=user_id,
            chat_id=chat_id,
            media=info,
            allowed_modes=_configured_modes_for(info.kind, settings.media.enabled_modes),
            created_at=now,
            expires_at=now + timedelta(seconds=settings.persistence.selection_ttl_seconds),
        )
        await asyncio.to_thread(repository.save_selection, selection)
        text = render_media_info(info)
        if record.status_message_id is not None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=record.status_message_id,
                text=text,
                reply_markup=selection_keyboard(selection),
            )
        else:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=selection_keyboard(selection),
            )
            await asyncio.to_thread(repository.set_status_message, job_id, message.message_id)
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.SUCCEEDED, source=info.source
        )
        metrics.record_job(outcome="inspection_succeeded", source=info.source)
        await logger.ainfo("inspection_completed", job_id=job_id, source=info.source)
        return str(job_id)
    except JobCancelledError:
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.CANCELLED,
            error_category=ErrorCategory.CANCELLED,
            error_summary="cancelled_by_user",
        )
        metrics.record_job(outcome="cancelled", error=ErrorCategory.CANCELLED.value)
        return str(job_id)
    except MediaBotError as exc:
        await _handle_controlled_failure(ctx, job_id, chat_id, exc, attempt)
        return str(job_id)
    except Exception as exc:
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
        await logger.aexception("inspection_unexpected_failure", job_id=job_id)
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
) -> str:
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
    progress_queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue(maxsize=1)
    reporter: asyncio.Task[None] | None = None
    cleanup_allowed = True
    loop = asyncio.get_running_loop()

    def progress_sink(event: ProgressEvent) -> None:
        loop.call_soon_threadsafe(_offer_progress, progress_queue, event)

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
        if record.status is JobStatus.DELIVERY_UNCERTAIN:
            return str(job_id)
        if cancellation():
            raise JobCancelledError("Download was cancelled")
        selected_mode = DownloadMode(mode)
        repository.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        reporter = asyncio.create_task(
            _report_progress(
                progress_queue, record.chat_id, record.status_message_id, delivery, settings
            )
        )
        download_task = asyncio.create_task(
            asyncio.to_thread(
                service.download,
                job_id=job_id,
                url=url,
                mode=selected_mode,
                output_directory=output_directory,
                temp_directory=temp_directory,
                progress=progress_sink if record.status_message_id is not None else None,
                is_cancelled=cancellation,
            )
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
            raise
        if cancellation():
            raise JobCancelledError("Download was cancelled before delivery")
        await asyncio.to_thread(
            repository.transition, job_id, JobStatus.DELIVERING, source=result.source
        )
        receipt = await delivery.deliver(
            chat_id=chat_id,
            result=result,
            caption=render_caption(settings, result),
        )
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.SUCCEEDED,
            source=result.source,
            delivery_file_id=receipt.file_id,
            delivery_file_unique_id=receipt.file_unique_id,
            attempt=attempt,
        )
        metrics.add_bytes(result.file_size_bytes)
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
        )
        return str(job_id)
    except JobCancelledError:
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.CANCELLED,
            error_category=ErrorCategory.CANCELLED,
            error_summary="cancelled_by_user",
            attempt=attempt,
        )
        metrics.record_job(outcome="cancelled", error=ErrorCategory.CANCELLED.value)
        await _notify(ctx, chat_id, record.status_message_id if record else None, CANCELLED_TEXT)
        await logger.ainfo("download_cancelled", job_id=job_id)
        return str(job_id)
    except DeliveryError as exc:
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.DELIVERY_UNCERTAIN,
            error_category=ErrorCategory.DELIVERY_UNCERTAIN,
            error_summary=type(exc).__name__,
            attempt=attempt,
        )
        metrics.record_job(
            outcome="delivery_uncertain", error=ErrorCategory.DELIVERY_UNCERTAIN.value
        )
        await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
        await logger.awarning("download_delivery_uncertain", job_id=job_id)
        return str(job_id)
    except MediaBotError as exc:
        await _handle_controlled_failure(ctx, job_id, chat_id, exc, attempt)
        return str(job_id)
    except asyncio.CancelledError:
        await logger.awarning("download_worker_shutdown", job_id=job_id)
        raise
    except Exception as exc:
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
        await logger.aexception("download_unexpected_failure", job_id=job_id)
        return str(job_id)
    finally:
        local_cancel.set()
        if reporter is not None:
            _offer_progress(progress_queue, None)
            await reporter
        if settings.storage.delete_after_upload and cleanup_allowed:
            await asyncio.gather(
                asyncio.to_thread(
                    _safe_remove, output_directory, settings.storage.downloads_path()
                ),
                asyncio.to_thread(_safe_remove, temp_directory, settings.storage.temp_path()),
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
    removed = await asyncio.to_thread(_cleanup_orphans, settings, repository, now)
    await logger.ainfo("maintenance_completed", purged_records=purged, removed_directories=removed)
    return purged + removed


async def _handle_controlled_failure(
    ctx: dict[str, Any], job_id: JobId, chat_id: int, exc: MediaBotError, attempt: int
) -> None:
    settings = cast(Settings, ctx["settings"])
    repository = cast(JobRepository, ctx["repository"])
    metrics = cast(MetricsRegistry, ctx["metrics"])
    record = repository.get_job(job_id)
    category = error_category(exc)
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
    await _notify_failure(ctx, chat_id, record.status_message_id if record else None)
    await logger.awarning(
        "job_controlled_failure", job_id=job_id, error_category=category.value, attempt=attempt
    )


async def _report_progress(
    queue: asyncio.Queue[ProgressEvent | None],
    chat_id: int,
    message_id: int | None,
    delivery: DeliveryGateway,
    settings: Settings,
) -> None:
    if message_id is None:
        return
    throttler = ProgressThrottler(
        min_interval_seconds=settings.telegram.progress_min_interval_seconds,
        min_percent_delta=settings.telegram.progress_min_percent_delta,
    )
    while True:
        event = await queue.get()
        if event is None:
            return
        if throttler.should_emit(event):
            await _safe_edit(
                delivery,
                chat_id,
                message_id,
                render_progress(event.percent, event.downloaded_bytes, event.total_bytes),
            )


async def _notify_failure(ctx: dict[str, Any], chat_id: int, message_id: int | None) -> None:
    await _notify(ctx, chat_id, message_id, FAILED_TEXT)


async def _notify(ctx: dict[str, Any], chat_id: int, message_id: int | None, text: str) -> None:
    delivery = cast(DeliveryGateway, ctx["delivery"])
    if message_id is not None:
        await _safe_edit(delivery, chat_id, message_id, text)
        return
    try:
        await delivery.send_text(chat_id, text)
    except DeliveryError as exc:
        await logger.awarning("telegram_notification_failed", error_type=type(exc).__name__)


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


def _configured_modes_for(
    kind: MediaKind, configured: tuple[DownloadMode, ...]
) -> tuple[DownloadMode, ...]:
    if kind is MediaKind.AUDIO:
        relevant = {DownloadMode.BEST, DownloadMode.AUDIO_BEST, DownloadMode.AUDIO_MP3}
        return tuple(mode for mode in configured if mode in relevant)
    if kind is MediaKind.IMAGE:
        return tuple(mode for mode in configured if mode is DownloadMode.BEST)
    return configured


def _offer_progress(
    queue: asyncio.Queue[ProgressEvent | None], event: ProgressEvent | None
) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


def _safe_remove(path: Path, root: Path) -> None:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise RuntimeError("Refusing to remove a path outside the job root")
    if resolved.exists():
        shutil.rmtree(resolved)


def _cleanup_orphans(settings: Settings, repository: JobRepository, now: datetime) -> int:
    removed = 0
    cutoff = now.timestamp() - settings.storage.orphan_grace_seconds
    for root in (settings.storage.downloads_path(), settings.storage.temp_path()):
        for child in root.iterdir():
            if not child.is_dir() or child.stat().st_mtime > cutoff:
                continue
            record = repository.get_job(JobId(child.name))
            if record is None or record.status.terminal:
                _safe_remove(child, root)
                removed += 1
    return removed
