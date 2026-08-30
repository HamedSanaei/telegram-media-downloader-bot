from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from telegram_media_bot.domain.models import (
    DeliveryItemReceipt,
    DeliveryProgressEvent,
    DeliveryReceipt,
    DownloadResult,
)

DeliveryProgressSink = Callable[[DeliveryProgressEvent], None]
DeliveryItemSink = Callable[[DeliveryItemReceipt], Awaitable[None]]
DeliveryCancellationCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class BatchDeliveryOutcome:
    """Result of one collection/batch delivery with per-item failure isolation."""

    total: int
    succeeded: int
    failed: int
    receipts: tuple[DeliveryItemReceipt, ...]
    delivered_bytes: int = 0


class DeliveryGateway(Protocol):
    async def deliver(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        source_url: str | None = None,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
        is_cancelled: DeliveryCancellationCheck | None = None,
    ) -> DeliveryReceipt: ...

    async def deliver_batch(
        self,
        *,
        chat_id: int,
        result: DownloadResult,
        caption: str,
        source_url: str | None = None,
        progress: DeliveryProgressSink | None = None,
        item_delivered: DeliveryItemSink | None = None,
        is_cancelled: DeliveryCancellationCheck | None = None,
        summary_title: str = "📚 دانلود مجموعه تمام شد",
    ) -> BatchDeliveryOutcome: ...

    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...
