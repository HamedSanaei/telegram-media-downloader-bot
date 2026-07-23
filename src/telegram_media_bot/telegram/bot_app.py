from __future__ import annotations

import structlog
from aiogram import Dispatcher

from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.infrastructure.security.redis_rate_limiter import RedisRateLimiter
from telegram_media_bot.telegram.bot_factory import create_bot
from telegram_media_bot.telegram.handlers import build_router

logger = structlog.get_logger(__name__)


async def run_bot(settings: Settings) -> None:
    settings.create_runtime_directories()
    queue = await ArqJobQueue.create(settings)
    repository = SqliteJobRepository(settings.database_path())
    repository.initialize()
    rate_limiter = RedisRateLimiter.create(settings.redis.url)
    access_policy = AccessPolicyService(
        settings=settings,
        repository=repository,
        rate_limiter=rate_limiter,
    )
    bot = create_bot(settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(
            settings=settings,
            queue=queue,
            repository=repository,
            access_policy=access_policy,
            jobs=JobService(repository),
        )
    )
    try:
        await logger.ainfo("bot_started")
        await dispatcher.start_polling(
            bot,
            polling_timeout=settings.telegram.polling_timeout_seconds,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await queue.close()
        await rate_limiter.close()
        await bot.session.close()
