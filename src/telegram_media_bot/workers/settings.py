from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from aiogram import Bot
from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job as ArqJob
from arq.jobs import JobStatus as ArqJobStatus
from arq.typing import WorkerSettingsBase

from telegram_media_bot.application.ports.delivery import DeliveryGateway
from telegram_media_bot.application.ports.download_engine import DownloadEngine
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.job_recovery_service import JobRecoveryService
from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.domain.models import (
    ComponentHealth,
    HealthReport,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    RecoveryDecision,
)
from telegram_media_bot.infrastructure.cookies.health import (
    MissingCookieChecker,
    NetscapeStaticCookieChecker,
)
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.media_engine_router import RoutedMediaEngine
from telegram_media_bot.infrastructure.observability.health_server import HealthServer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_cookie_health import (
    SqliteCookieHealthRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_effects import SqliteEffectLedger
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.infrastructure.storage.workspace import (
    cleanup_job_workspace,
    sweep_workspaces,
)
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.telegram.bot_factory import (
    TelegramRuntime,
    create_telegram_runtime,
    readiness_wait_required,
    wait_for_local_api_readiness,
)
from telegram_media_bot.telegram.delivery import RoutedDeliveryGateway
from telegram_media_bot.workers.jobs import (
    maintenance_job,
    process_download_job,
    process_highlight_tray_job,
    process_inspection_job,
)

logger = structlog.get_logger(__name__)

RecoveryNotifier = Callable[[JobRecord], Awaitable[None]]


async def startup(ctx: dict[str, Any]) -> None:
    settings = load_settings(require_token=True)
    settings.create_runtime_directories()
    if readiness_wait_required(settings):
        local_api_manager = LocalBotApiManager(settings)
        if local_api_manager.active_endpoint() == "local":
            await wait_for_local_api_readiness(settings)
    runtime = await asyncio.to_thread(create_telegram_runtime, settings, role="worker")
    settings = runtime.settings
    bot = runtime.bot
    try:
        repository = SqliteJobRepository(settings.database_path())
        await asyncio.to_thread(repository.initialize)
        inbound_store = SqliteInboundUpdateRepository(settings.database_path())
        await asyncio.to_thread(inbound_store.initialize)
        effect_store = SqliteEffectLedger(settings.database_path())
        await asyncio.to_thread(effect_store.initialize)
        cookie_health_store = SqliteCookieHealthRepository(settings.database_path())
        await asyncio.to_thread(cookie_health_store.initialize)
        ytdlp_engine = YtDlpEngine(settings)
        gallery_engine = GalleryDlEngine(settings)
        gallery_health = await asyncio.to_thread(gallery_engine.health)
        cookie_file = settings.effective_cookie_file()
        gallery_cookie_readability = {
            source: (
                (cookie := settings.gallery_dl.cookie_for(source, cookie_file)) is None
                or cookie.is_file()
            )
            for source in sorted(settings.gallery_dl.enabled_platforms)
        }
        engine = RoutedMediaEngine(gallery_engine, ytdlp_engine)
        queue = ArqJobQueue(
            cast(ArqRedis, ctx["redis"]), settings.redis.queue_name, owns_pool=False
        )
        service = DownloadService(
            engine=engine,
            enabled_sources=settings.media.enabled_sources,
            url_validator=PublicUrlValidator(
                reject_private_networks=settings.security.reject_private_network_urls
            ),
            allow_playlists=settings.media.allow_playlists,
            playlist_max_items=settings.media.playlist_max_items,
            max_duration_seconds=settings.media.max_duration_seconds,
            max_file_size_bytes=settings.media.max_file_size_mb * 1024 * 1024,
            max_source_size_bytes=settings.media.max_source_size_mb * 1024 * 1024,
            instagram_max_videos=settings.media.instagram.max_videos,
            instagram_max_stories=settings.media.instagram.max_stories_per_batch,
            instagram_max_highlight_items=settings.media.instagram.max_highlight_items,
        )
        metrics = MetricsRegistry()
        cookie_health_service = CookieHealthService(
            store=cookie_health_store,
            checker=(
                NetscapeStaticCookieChecker(NetscapeCookieManager(cookie_file))
                if (cookie_file := settings.effective_cookie_file()) is not None
                else MissingCookieChecker()
            ),
            expiring_soon_hours=settings.cookie_health.expiring_soon_hours,
            reminder_interval_minutes=settings.cookie_health.reminder_interval_minutes,
            recovery_notifications=settings.cookie_health.recovery_notifications,
        )
        if settings.cookie_health.enabled:
            await asyncio.to_thread(cookie_health_service.refresh_static)
        identity = await bot.get_me()
        ctx.update(
            settings=settings,
            repository=repository,
            inbound_updates=inbound_store,
            effect_ledger=effect_store,
            bot=bot,
            engine=engine,
            gallery_engine=gallery_engine,
            queue=queue,
            download_service=service,
            delivery=RoutedDeliveryGateway(
                bot,
                settings,
            ),
            metrics=metrics,
            telegram_runtime=runtime,
            bot_username=identity.username or "telegram_media_bot",
            bot_identity_available=True,
            cookie_health_service=cookie_health_service,
        )
        cutoff = datetime.now(UTC)
        recovered = await asyncio.to_thread(repository.reconcile_abandoned, cutoff)
        requeued_count = 0
        cancelled_count = 0
        for recovery in recovered:
            record = recovery.job
            if recovery.decision is RecoveryDecision.SKIP_CANCELLED:
                abort = await queue.abort_job(
                    record.job_id,
                    timeout_seconds=0.1,
                    finalize_stale=True,
                )
                await asyncio.to_thread(
                    cleanup_job_workspace,
                    settings,
                    record.job_id,
                    terminal_status=JobStatus.CANCELLED.value,
                    cleanup_reason="startup_cancelled_recovery",
                )
                cancelled_count += 1
                await logger.ainfo(
                    "job_recovery_decision",
                    job_id=record.job_id,
                    previous_status=recovery.previous_status.value,
                    cancel_requested=True,
                    arq_job_status=abort.previous_status.value,
                    recovery_decision=recovery.decision.value,
                    cancel_source="startup_reconciliation",
                    abort_result=abort.final_status.value,
                    redis_keys_removed=abort.redis_keys_removed,
                    final_status=record.status.value,
                )
                continue
            if record.status is not JobStatus.QUEUED:
                await logger.awarning(
                    "delivery_requires_operator_review",
                    job_id=record.job_id,
                    status=record.status.value,
                    previous_status=recovery.previous_status.value,
                    cancel_requested=record.cancel_requested,
                    recovery_decision=recovery.decision.value,
                    final_status=record.status.value,
                )
                continue
            if record.kind is JobKind.INSPECTION:
                await queue.enqueue_inspection(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                )
            elif record.mode is not None:
                await queue.enqueue_download(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                    mode=record.mode,
                    container=record.container,
                    container_policy=record.container_policy,
                    native_video_codec=record.native_video_codec,
                    selected_format_ids=record.selected_format_ids,
                    image_delivery_mode=record.image_delivery_mode,
                )
            requeued_count += 1
            await logger.ainfo(
                "job_recovery_decision",
                job_id=record.job_id,
                previous_status=recovery.previous_status.value,
                cancel_requested=False,
                recovery_decision=recovery.decision.value,
                final_status=record.status.value,
            )
        startup_cleanup = await asyncio.to_thread(
            sweep_workspaces,
            settings,
            repository,
            datetime.now(UTC),
            cleanup_reason="startup",
        )
        metrics.record_workspace_cleanup(
            files_deleted=startup_cleanup.files_deleted,
            directories_deleted=startup_cleanup.directories_deleted,
            bytes_reclaimed=startup_cleanup.bytes_reclaimed,
            failed_paths=startup_cleanup.failed_paths_count,
            duration_seconds=startup_cleanup.duration_seconds,
        )
        delivery_gateway = cast(DeliveryGateway, ctx["delivery"])
        effective_recovery_threshold = settings.recovery.effective_queue_pressure_threshold(
            settings.queue.max_jobs
        )
        stale_effects = await asyncio.to_thread(
            effect_store.reconcile_stale_pending,
            datetime.now(UTC),
            stale_after_minutes=settings.operations.inbound_updates.effect_pending_stale_minutes,
            batch_size=settings.operations.inbound_updates.cleanup_batch_size,
        )
        metrics.record_effects_marked_uncertain(stale_effects)
        metrics.set_effects_stale_pending(effect_store.state_counts().get("pending", 0))
        recovery_service = JobRecoveryService(
            repository,
            queue,
            max_attempts=settings.recovery.max_recovery_attempts,
            max_age_days=settings.recovery.max_recoverable_age_days,
            notify=(
                _resume_notifier(delivery_gateway) if settings.recovery.notify_on_resume else None
            ),
            remediation_batch_size=settings.recovery.remediation_batch_size,
            startup_recovery_batch_size=settings.recovery.startup_recovery_batch_size,
            reconciliation_batch_size=settings.recovery.reconciliation_batch_size,
            queue_pressure_threshold=effective_recovery_threshold,
            max_recovery_per_user=settings.recovery.max_recovery_per_user,
            queue_depth_probe=queue.queue_depth,
        )
        ctx["recovery_service"] = recovery_service
        metrics.set_recoverable_batch_size(settings.recovery.remediation_batch_size)
        metrics.set_recovery_effective_threshold(recovery_service.effective_queue_threshold)
        startup_outstanding, startup_headroom = await recovery_service.queue_observability()
        metrics.set_recovery_outstanding_queue_depth(startup_outstanding)
        metrics.set_recovery_available_headroom(startup_headroom)
        appfix_recovered = 0
        if settings.recovery.app_fix_recovery_enabled:
            appfix_summary = await recovery_service.recover_after_app_fix()
            appfix_recovered = appfix_summary.requeued
            for _ in range(appfix_recovered):
                metrics.record_recovery("app_fix")
        recovery_requeues_reconciled = await recovery_service.reconcile_recovery_requeues(
            lambda job_id: _recovery_arq_missing(
                cast(ArqRedis, ctx["redis"]), settings.redis.queue_name, job_id
            )
        )
        recoverable_pending = recovery_service.pending_recoverable_count()
        metrics.set_recoverable_pending(recoverable_pending)
        server = HealthServer(
            host=settings.observability.health_host,
            port=settings.observability.health_port,
            probe=lambda: _health_report(ctx),
            metrics=metrics,
            queue_depth=queue.queue_depth,
            metrics_enabled=settings.observability.metrics_enabled,
        )
        await server.start()
        ctx["health_server"] = server
        await logger.ainfo(
            "worker_started",
            recovered_jobs=len(recovered),
            requeued_jobs=requeued_count,
            cancelled_jobs=cancelled_count,
            appfix_recovered_jobs=appfix_recovered,
            recovery_requeues_reconciled=recovery_requeues_reconciled,
            recoverable_jobs_pending=recoverable_pending,
            cleanup_directories=startup_cleanup.directories_deleted,
            cleanup_bytes_reclaimed=startup_cleanup.bytes_reclaimed,
            cleanup_failed_paths=startup_cleanup.failed_paths_count,
            gallery_dl_enabled=settings.gallery_dl.enabled,
            gallery_dl_healthy=gallery_health.healthy,
            gallery_dl_version=gallery_health.detail,
            gallery_dl_cookie_readability=gallery_cookie_readability,
        )
    except Exception:
        ctx.pop("bot", None)
        ctx.pop("telegram_runtime", None)
        await bot.session.close()
        await asyncio.to_thread(runtime.close_local_api)
        raise


async def shutdown(ctx: dict[str, Any]) -> None:
    server = ctx.get("health_server")
    if isinstance(server, HealthServer):
        await server.close()
    bot = ctx.get("bot")
    if isinstance(bot, Bot):
        await bot.session.close()
    runtime = ctx.get("telegram_runtime")
    if isinstance(runtime, TelegramRuntime):
        await asyncio.to_thread(runtime.close_local_api)


async def _health_report(ctx: dict[str, Any]) -> HealthReport:
    settings = cast(Settings, ctx["settings"])
    repository = cast(SqliteJobRepository, ctx["repository"])
    queue = cast(ArqJobQueue, ctx["queue"])
    engine = cast(DownloadEngine, ctx["engine"])
    redis_ok, database_ok, engine_health = await asyncio.gather(
        queue.healthy(),
        asyncio.to_thread(repository.healthy),
        asyncio.to_thread(engine.health),
    )
    storage_ok = await asyncio.to_thread(_storage_writable, settings)
    telegram_ok = bool(ctx.get("bot_identity_available", False))
    checks = [
        ComponentHealth("redis", redis_ok),
        ComponentHealth("database", database_ok),
        ComponentHealth("storage", storage_ok),
        ComponentHealth("telegram", telegram_ok),
        ComponentHealth("ffmpeg", shutil.which("ffmpeg") is not None),
        engine_health,
    ]
    runtime = ctx.get("telegram_runtime")
    if settings.telegram.local_bot_api.enabled and isinstance(runtime, TelegramRuntime):
        local_reachable = await asyncio.to_thread(LocalBotApiManager(settings).endpoint_reachable)
        local_active = runtime.endpoint == "local"
        checks.append(
            ComponentHealth(
                "local_bot_api",
                local_reachable if local_active else True,
                "active" if local_active else "inactive",
            )
        )
    return HealthReport(checks=tuple(checks))


def _resume_notifier(delivery: DeliveryGateway) -> RecoveryNotifier:
    from telegram_media_bot.telegram.texts import RESUME_NOTICE_TEXT

    async def notify(record: JobRecord) -> None:
        if record.status_message_id is None:
            return
        with suppress(Exception):
            await delivery.edit_text(record.chat_id, record.status_message_id, RESUME_NOTICE_TEXT)

    return notify


async def _recovery_arq_missing(
    redis: ArqRedis,
    queue_name: str,
    job_id: JobId,
) -> bool:
    """Whether a durable job has no live ARQ job, so its enqueue can be retried safely."""
    try:
        status = await ArqJob(str(job_id), redis, _queue_name=queue_name).status()
    except Exception:
        return False
    return status is ArqJobStatus.not_found


def _storage_writable(settings: Settings) -> bool:
    probe = settings.storage.state_path() / ".readiness-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


_settings = load_settings(require_token=True)


class WorkerSettings(WorkerSettingsBase):
    functions: tuple[Any, ...] = (
        process_inspection_job,
        process_download_job,
        process_highlight_tray_job,
    )
    cron_jobs: tuple[Any, ...] = (
        cron(maintenance_job, minute=None, second={0, 30}, run_at_startup=True),
    )
    on_startup: Any = startup
    on_shutdown: Any = shutdown
    redis_settings: RedisSettings = RedisSettings.from_dsn(_settings.redis.url)
    queue_name: str = _settings.redis.queue_name
    max_jobs: int = _settings.queue.max_jobs
    job_timeout: int = _settings.queue.job_timeout_seconds
    max_tries: int = _settings.queue.max_tries
    keep_result: int = _settings.queue.keep_result_seconds
    allow_abort_jobs: bool = True
    health_check_interval: int = 30
