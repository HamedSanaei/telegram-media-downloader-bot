from __future__ import annotations

from typing import Protocol

from telegram_media_bot.domain.cookies import CookieUpdateSummary


class CookieManager(Protocol):
    def merge(self, uploaded: bytes) -> CookieUpdateSummary: ...

    def export_combined(self) -> bytes: ...
