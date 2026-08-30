from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher

from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.application.services.effect_ledger import EffectLedgerService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import PngUsageChartRenderer
from telegram_media_bot.infrastructure.cookies.health import (
    MissingCookieChecker,
    NetscapeStaticCookieChecker,
)
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager
from telegram_media_bot.infrastructure.persistence.sqlite_cookie_health import (
    SqliteCookieHealthRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_effects import SqliteEffectLedger
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
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
from telegram_media_bot.telegram.durable_polling import durable_poll, replay_pending_updates
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
        inbox_store = SqliteInboundUpdateRepository(settings.database_path())
        await asyncio.to_thread(inbox_store.initialize)
        effect_store = SqliteEffectLedger(settings.database_path())
        await asyncio.to_thread(effect_store.initialize)
        effects = EffectLedgerService(effect_store)
        stale_effects = await asyncio.to_thread(
            effect_store.reconcile_stale_pending,
            datetime.now(UTC),
            stale_after_minutes=settings.operations.inbound_updates.effect_pending_stale_minutes,
            batch_size=settings.operations.inbound_updates.cleanup_batch_size,
        )
        if stale_effects:
            await logger.ainfo(
                "telegram_effects_stale_reconciled",
                effects_marked_uncertain=stale_effects,
                stale_after_minutes=settings.operations.inbound_updates.effect_pending_stale_minutes,
            )
        cookie_health_store = SqliteCookieHealthRepository(settings.database_path())
        await asyncio.to_thread(cookie_health_store.initialize)
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
                cookie_health_service=cookie_health_service,
                effects=effects,
            )
        )
        inbox = DurableUpdateInbox(inbox_store)
        await logger.ainfo(
            "inbound_update_inbox_ready",
            pending_updates=await asyncio.to_thread(inbox.pending_count),
        )
        # Reconcile durable updates left incomplete by a previous crash/downtime before polling.
        recovered = await replay_pending_updates(bot, dispatcher, inbox)
        await logger.ainfo("bot_started", recovered_updates=recovered)
        await durable_poll(
            bot,
            dispatcher,
            inbox,
            polling_timeout=settings.telegram.polling_timeout_seconds,
            stopped=lambda: False,
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
