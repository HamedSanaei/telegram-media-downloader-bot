from collections.abc import Awaitable, Callable
from typing import Protocol

from telegram_media_bot.domain.models import (
    DeliveryItemReceipt,
    DeliveryProgressEvent,
    DeliveryReceipt,
    DownloadResult,
)

DeliveryProgressSink = Callable[[DeliveryProgressEvent], None]
DeliveryItemSink = Callable[[DeliveryItemReceipt], Awaitable[None]]


class DeliveryGateway(Protocol):
    async def deliver(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
    ) -> DeliveryReceipt: ...

    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...
