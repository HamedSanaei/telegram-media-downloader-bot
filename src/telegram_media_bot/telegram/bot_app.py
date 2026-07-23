from __future__ import annotations

import structlog
from aiogram import Bot, Dispatcher

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue
from telegram_media_bot.telegram.handlers import build_router

logger = structlog.get_logger(__name__)


async def run_bot(settings: Settings) -> None:
    settings.create_runtime_directories()
    queue = await ArqJobQueue.create(settings)
    bot = Bot(token=settings.telegram.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings=settings, queue=queue))
    try:
        await logger.ainfo("bot_started")
        await dispatcher.start_polling(
            bot,
            polling_timeout=settings.telegram.polling_timeout_seconds,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await queue.close()
        await bot.session.close()
