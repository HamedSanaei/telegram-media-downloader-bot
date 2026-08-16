from __future__ import annotations

import asyncio

import structlog
from aiogram import Dispatcher

from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import PngUsageChartRenderer
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.persistence.sqlite_usage_analytics import (
    SqliteUsageAnalyticsRepository,
)
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.infrastructure.security.redis_rate_limiter import RedisRateLimiter
from telegram_media_bot.infrastructure.security.telegram_membership import (
    TelegramMembershipChecker,
)
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.telegram.bot_factory import (
    create_telegram_runtime,
    readiness_wait_required,
    wait_for_local_api_readiness,
)
from telegram_media_bot.telegram.handlers import build_router

logger = structlog.get_logger(__name__)


async def run_bot(settings: Settings) -> None:
    settings.create_runtime_directories()
    if readiness_wait_required(settings):
        manager = LocalBotApiManager(settings)
        if manager.active_endpoint() == "local":
            await wait_for_local_api_readiness(settings)
    runtime = await asyncio.to_thread(create_telegram_runtime, settings, role="bot")
    settings = runtime.settings
    bot = runtime.bot
    queue: ArqJobQueue | None = None
    rate_limiter: RedisRateLimiter | None = None
    membership_checker: TelegramMembershipChecker | None = None
    try:
        await bot.get_me()
        queue = await ArqJobQueue.create(settings)
        repository = SqliteJobRepository(settings.database_path())
        repository.initialize()
        rate_limiter = RedisRateLimiter.create(settings.redis.url)
        if settings.telegram.required_channels.enabled:
            membership_checker = TelegramMembershipChecker.create(
                bot,
                settings.redis.url,
                settings.telegram.required_channels,
            )
        access_policy = AccessPolicyService(
            settings=settings,
            repository=repository,
            rate_limiter=rate_limiter,
            membership_checker=membership_checker,
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(
            build_router(
                settings=settings,
                queue=queue,
                repository=repository,
                access_policy=access_policy,
                jobs=JobService(repository),
                users=repository,
                usage_analytics=UsageAnalyticsService(
                    SqliteUsageAnalyticsRepository(settings.database_path()),
                    admin_ids=settings.telegram.admin_ids,
                ),
                usage_chart_renderer=PngUsageChartRenderer(),
                cookie_manager=(
                    NetscapeCookieManager(cookie_file)
                    if (cookie_file := settings.effective_cookie_file()) is not None
                    else None
                ),
            )
        )
        await logger.ainfo("bot_started")
        await dispatcher.start_polling(
            bot,
            polling_timeout=settings.telegram.polling_timeout_seconds,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        if queue is not None:
            await queue.close()
        if rate_limiter is not None:
            await rate_limiter.close()
        if membership_checker is not None:
            await membership_checker.close()
        await bot.session.close()
        await asyncio.to_thread(runtime.close_local_api)
