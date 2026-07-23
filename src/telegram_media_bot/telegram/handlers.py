from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import InvalidUrlError
from telegram_media_bot.telegram.texts import INVALID_URL_TEXT, QUEUED_TEXT, START_TEXT
from telegram_media_bot.telegram.url_extractor import extract_first_url


def build_router(*, settings: Settings, queue: JobQueue) -> Router:
    router = Router(name="main")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("health"))
    async def health(message: Message) -> None:
        await message.answer("OK")

    @router.message()
    async def enqueue_url(message: Message) -> None:
        if message.from_user is None:
            return
        user_id = message.from_user.id
        if user_id in settings.security.blocked_user_ids:
            return
        if settings.security.allowed_user_ids and user_id not in settings.security.allowed_user_ids:
            return

        url = extract_first_url(message.text or message.caption)
        if url is None:
            await message.answer(INVALID_URL_TEXT)
            return
        try:
            validated = DownloadService.validate_url(url)
        except InvalidUrlError:
            await message.answer(INVALID_URL_TEXT)
            return

        job_id = await queue.enqueue_download(
            chat_id=message.chat.id,
            user_id=user_id,
            url=validated,
            mode=settings.media.default_mode,
        )
        await message.answer(QUEUED_TEXT.format(job_id=job_id), parse_mode="HTML")

    return router
