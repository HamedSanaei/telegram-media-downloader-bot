from __future__ import annotations

from typing import Any, ClassVar

from aiogram import Bot
from arq.connections import RedisSettings

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.workers.jobs import process_download_job


async def startup(ctx: dict[str, Any]) -> None:
    settings = load_settings(require_token=True)
    settings.create_runtime_directories()
    ctx["settings"] = settings
    ctx["bot"] = Bot(token=settings.telegram.bot_token)
    ctx["download_service"] = DownloadService(
        engine=YtDlpEngine(settings),
        enabled_sources=settings.media.enabled_sources,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    bot = ctx.get("bot")
    if isinstance(bot, Bot):
        await bot.session.close()


_settings = load_settings(require_token=True)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [process_download_job]
    on_startup: ClassVar[Any] = startup
    on_shutdown: ClassVar[Any] = shutdown
    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(_settings.redis.url)
    queue_name: ClassVar[str] = _settings.redis.queue_name
    max_jobs: ClassVar[int] = _settings.queue.max_jobs
    job_timeout: ClassVar[int] = _settings.queue.job_timeout_seconds
    max_tries: ClassVar[int] = _settings.queue.max_tries
    keep_result: ClassVar[int] = _settings.queue.keep_result_seconds
    health_check_interval: ClassVar[int] = 30
