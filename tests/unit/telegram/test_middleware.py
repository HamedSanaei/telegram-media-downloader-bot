from datetime import UTC, datetime
from typing import Any

import structlog.contextvars
from aiogram.types import Chat, Message, TelegramObject, User

from telegram_media_bot.telegram.middleware import CorrelationMiddleware


async def test_correlation_middleware_binds_and_clears_context() -> None:
    captured: dict[str, Any] = {}

    async def handler(_event: TelegramObject, _data: dict[str, Any]) -> str:
        captured.update(structlog.contextvars.get_contextvars())
        return "ok"

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=10, type="private"),
        from_user=User(id=20, is_bot=False, first_name="User"),
    )
    result = await CorrelationMiddleware()(handler, message, {})
    assert result == "ok"
    assert captured["chat_id"] == 10
    assert captured["user_id"] == 20
    assert captured["request_id"]
    assert structlog.contextvars.get_contextvars() == {}
