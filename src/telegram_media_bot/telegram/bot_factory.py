from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.telegram.local_api import (
    EndpointLease,
    LocalBotApiManager,
    ManagedLocalApiHandle,
    effective_settings,
)


@dataclass(frozen=True, slots=True)
class TelegramRuntime:
    bot: Bot
    settings: Settings
    endpoint: str
    managed_handle: ManagedLocalApiHandle | None = None
    lease: EndpointLease | None = None

    def close_local_api(self) -> None:
        if self.lease is not None:
            self.lease.close()
        if self.managed_handle is not None:
            self.managed_handle.stop_if_owned()


def create_telegram_runtime(
    settings: Settings,
    *,
    manage_lifecycle: bool = True,
    role: Literal["bot", "worker"] | None = None,
) -> TelegramRuntime:
    local_config = settings.telegram.local_bot_api
    handle: ManagedLocalApiHandle | None = None
    manager: LocalBotApiManager | None = None
    if local_config.enabled:
        manager = LocalBotApiManager(settings)
        endpoint = manager.active_endpoint()
        if endpoint == "local":
            handle = manager.ensure_started() if manage_lifecycle else None
    else:
        endpoint = "local" if settings.telegram.local_api_base_url else "cloud"
    runtime_settings = effective_settings(settings, endpoint)
    lease = (
        manager.register_client(role=role, endpoint=endpoint)
        if manager is not None and role is not None
        else None
    )
    try:
        bot = _create_bot_for_endpoint(runtime_settings, endpoint)
    except Exception:
        if lease is not None:
            lease.close()
        if handle is not None:
            handle.stop_if_owned()
        raise
    return TelegramRuntime(
        bot=bot,
        settings=runtime_settings,
        endpoint=endpoint,
        managed_handle=handle,
        lease=lease,
    )


def create_bot(settings: Settings) -> Bot:
    return create_telegram_runtime(settings, manage_lifecycle=False).bot


def _create_bot_for_endpoint(settings: Settings, endpoint: str) -> Bot:
    token = settings.telegram.token()
    if endpoint == "cloud":
        return Bot(token=token)
    base_url = settings.telegram.local_api_base_url
    if base_url is None:
        raise ValueError("Local Telegram endpoint requires local_api_base_url")
    api = TelegramAPIServer.from_base(
        base_url.rstrip("/"),
        is_local=settings.telegram.local_api_is_local,
    )
    return Bot(token=token, session=AiohttpSession(api=api))
