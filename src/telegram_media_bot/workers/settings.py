from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from aiogram import Bot
from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.typing import WorkerSettingsBase

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.domain.models import (
    ComponentHealth,
    HealthReport,
    JobKind,
    JobStatus,
    RecoveryDecision,
)
from telegram_media_bot.infrastructure.observability.health_server import HealthServer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.telegram.bot_factory import TelegramRuntime, create_telegram_runtime
from telegram_media_bot.telegram.delivery import RoutedDeliveryGateway
from telegram_media_bot.workers.jobs import (
    maintenance_job,
    process_download_job,
    process_inspection_job,
)

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = load_settings(require_token=True)
    settings.create_runtime_directories()
    runtime = await asyncio.to_thread(create_telegram_runtime, settings, role="worker")
    settings = runtime.settings
    bot = runtime.bot
    try:
        repository = SqliteJobRepository(settings.database_path())
        await asyncio.to_thread(repository.initialize)
        engine = YtDlpEngine(settings)
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
            instagram_max_videos=settings.media.instagram.max_videos,
        )
        metrics = MetricsRegistry()
        identity = await bot.get_me()
        ctx.update(
            settings=settings,
            repository=repository,
            bot=bot,
            engine=engine,
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
                    _cleanup_cancelled_directories, settings, str(record.job_id)
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
    engine = cast(YtDlpEngine, ctx["engine"])
    redis_ok, database_ok = await asyncio.gather(
        queue.healthy(), asyncio.to_thread(repository.healthy)
    )
    storage_ok = await asyncio.to_thread(_storage_writable, settings)
    telegram_ok = bool(ctx.get("bot_identity_available", False))
    checks = [
        ComponentHealth("redis", redis_ok),
        ComponentHealth("database", database_ok),
        ComponentHealth("storage", storage_ok),
        ComponentHealth("telegram", telegram_ok),
        ComponentHealth("ffmpeg", shutil.which("ffmpeg") is not None),
        engine.health(),
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


def _storage_writable(settings: Settings) -> bool:
    probe = settings.storage.state_path() / ".readiness-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _cleanup_cancelled_directories(settings: Settings, job_id: str) -> None:
    for root in (settings.storage.downloads_path(), settings.storage.temp_path()):
        target = (root / job_id).resolve()
        if target != root.resolve() and target.is_relative_to(root.resolve()) and target.exists():
            shutil.rmtree(target)


_settings = load_settings(require_token=True)


class WorkerSettings(WorkerSettingsBase):
    functions: tuple[Any, ...] = (process_inspection_job, process_download_job)
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
