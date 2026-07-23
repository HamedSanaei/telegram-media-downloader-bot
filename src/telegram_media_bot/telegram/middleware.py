from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import structlog.contextvars
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class CorrelationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        context: dict[str, object] = {"request_id": secrets.token_urlsafe(9)}
        if isinstance(event, Message):
            context["chat_id"] = event.chat.id
            if event.from_user is not None:
                context["user_id"] = event.from_user.id
        elif isinstance(event, CallbackQuery):
            context["user_id"] = event.from_user.id
            if isinstance(event.message, Message):
                context["chat_id"] = event.message.chat.id
        structlog.contextvars.bind_contextvars(**context)
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.clear_contextvars()
