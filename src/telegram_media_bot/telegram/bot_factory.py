from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from telegram_media_bot.bootstrap.config import Settings


def create_bot(settings: Settings) -> Bot:
    base_url = settings.telegram.local_api_base_url
    if base_url is None:
        return Bot(token=settings.telegram.bot_token)
    api = TelegramAPIServer.from_base(
        base_url.rstrip("/"), is_local=settings.telegram.local_api_is_local
    )
    return Bot(token=settings.telegram.bot_token, session=AiohttpSession(api=api))
