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
from telegram_media_bot.domain.models import ComponentHealth, HealthReport, JobKind, JobStatus
from telegram_media_bot.infrastructure.observability.health_server import HealthServer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.telegram.bot_factory import create_bot
from telegram_media_bot.telegram.delivery import TelegramDeliveryGateway
from telegram_media_bot.workers.jobs import (
    maintenance_job,
    process_download_job,
    process_inspection_job,
)

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = load_settings(require_token=True)
    settings.create_runtime_directories()
    repository = SqliteJobRepository(settings.database_path())
    await asyncio.to_thread(repository.initialize)
    bot = create_bot(settings)
    engine = YtDlpEngine(settings)
    queue = ArqJobQueue(cast(ArqRedis, ctx["redis"]), settings.redis.queue_name, owns_pool=False)
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
    )
    metrics = MetricsRegistry()
    ctx.update(
        settings=settings,
        repository=repository,
        bot=bot,
        engine=engine,
        queue=queue,
        download_service=service,
        delivery=TelegramDeliveryGateway(bot, settings),
        metrics=metrics,
    )
    cutoff = datetime.now(UTC)
    recovered = await asyncio.to_thread(repository.reconcile_abandoned, cutoff)
    for record in recovered:
        if record.status is not JobStatus.QUEUED:
            await logger.awarning(
                "delivery_requires_operator_review",
                job_id=record.job_id,
                status=record.status.value,
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
    await logger.ainfo("worker_started", recovered_jobs=len(recovered))


async def shutdown(ctx: dict[str, Any]) -> None:
    server = ctx.get("health_server")
    if isinstance(server, HealthServer):
        await server.close()
    bot = ctx.get("bot")
    if isinstance(bot, Bot):
        await bot.session.close()


async def _health_report(ctx: dict[str, Any]) -> HealthReport:
    settings = cast(Settings, ctx["settings"])
    repository = cast(SqliteJobRepository, ctx["repository"])
    queue = cast(ArqJobQueue, ctx["queue"])
    engine = cast(YtDlpEngine, ctx["engine"])
    bot = cast(Bot, ctx["bot"])
    redis_ok, database_ok = await asyncio.gather(
        queue.healthy(), asyncio.to_thread(repository.healthy)
    )
    storage_ok = await asyncio.to_thread(_storage_writable, settings)
    telegram_ok = True
    if settings.observability.telegram_readiness_check:
        try:
            await bot.get_me()
        except Exception:
            telegram_ok = False
    checks = (
        ComponentHealth("redis", redis_ok),
        ComponentHealth("database", database_ok),
        ComponentHealth("storage", storage_ok),
        ComponentHealth("telegram", telegram_ok),
        ComponentHealth("ffmpeg", shutil.which("ffmpeg") is not None),
        engine.health(),
    )
    return HealthReport(checks=checks)


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
    health_check_interval: int = 30
