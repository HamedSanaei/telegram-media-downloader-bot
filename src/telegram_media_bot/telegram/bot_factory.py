from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import structlog
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import LocalBotApiError
from telegram_media_bot.infrastructure.telegram.local_api import (
    EndpointLease,
    LocalBotApiManager,
    ManagedLocalApiHandle,
    effective_settings,
)

logger = structlog.get_logger(__name__)

_READINESS_BACKOFF_CAP_SECONDS = 5.0
_READINESS_INITIAL_BACKOFF_SECONDS = 0.25


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
            application_owns_lifecycle = local_config.lifecycle_owner == "application"
            if manage_lifecycle and application_owns_lifecycle:
                handle = manager.ensure_started()
            elif manage_lifecycle and not manager.endpoint_reachable():
                raise ValueError("Configured Local Telegram API service is unreachable")
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


def readiness_wait_required(settings: Settings) -> bool:
    """Whether startup must wait for an already-running external Local Telegram API endpoint.

    Application-owned managed Local Bot API processes are started by ``ensure_started`` with
    their own bounded startup timeout, so only service-owned managed instances and external
    (remote) endpoints require a separate readiness wait here.
    """
    local_api = settings.telegram.local_bot_api
    if not local_api.enabled:
        return False
    return local_api.mode == "external" or local_api.lifecycle_owner == "service"


async def wait_for_local_api_readiness(settings: Settings) -> None:
    """Bounded, cancellable wait for the configured Local Telegram API endpoint to accept connections.

    The first probe runs immediately so an already-ready Local API never delays startup. Later
    probes use exponential backoff capped at ``_READINESS_BACKOFF_CAP_SECONDS`` and stop at the
    configured ``startup_timeout_seconds`` deadline. The await points are in the event loop, so
    SIGINT/SIGTERM cancels the wait promptly. Emits structured ``local_api_startup_wait``,
    ``local_api_startup_ready``, and ``local_api_startup_timeout`` events.
    """
    manager = LocalBotApiManager(settings)
    deadline = time.monotonic() + settings.telegram.local_bot_api.startup_timeout_seconds
    started = time.monotonic()
    attempt = 0
    delay = _READINESS_INITIAL_BACKOFF_SECONDS
    while True:
        reachable = await asyncio.to_thread(manager.endpoint_reachable)
        if reachable:
            await logger.ainfo(
                "local_api_startup_ready",
                attempt=attempt,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await logger.awarning(
                "local_api_startup_timeout",
                attempt=attempt,
                timeout_seconds=settings.telegram.local_bot_api.startup_timeout_seconds,
            )
            raise LocalBotApiError(
                "Configured Local Telegram API service did not become reachable within "
                f"{settings.telegram.local_bot_api.startup_timeout_seconds} seconds"
            )
        await logger.ainfo(
            "local_api_startup_wait",
            attempt=attempt,
            backoff_seconds=round(delay, 3),
            remaining_seconds=round(remaining, 3),
        )
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, _READINESS_BACKOFF_CAP_SECONDS)
        attempt += 1


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
