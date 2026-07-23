from typing import Protocol

from telegram_media_bot.domain.models import DeliveryReceipt, DownloadResult


class DeliveryGateway(Protocol):
    async def deliver(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
    ) -> DeliveryReceipt: ...

    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...
