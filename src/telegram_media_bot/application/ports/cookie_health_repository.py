from __future__ import annotations

from typing import Protocol

from telegram_media_bot.domain.cookie_health import ProviderCookieHealth
from telegram_media_bot.domain.cookies import CookieService


class CookieHealthRepository(Protocol):
    def initialize(self) -> None: ...

    def load_all(self) -> dict[CookieService, ProviderCookieHealth]: ...

    def load(self, provider: CookieService) -> ProviderCookieHealth | None: ...

    def save(self, health: ProviderCookieHealth) -> None: ...
