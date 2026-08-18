from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.cookie_health import ActiveProbeResult, StaticCookieCheck
from telegram_media_bot.domain.cookies import CookieService


class StaticCookieChecker(Protocol):
    """Network-free static validation of the canonical cookie file for one provider."""

    def check(
        self,
        provider: CookieService,
        *,
        now: datetime,
        expiring_soon_hours: float,
    ) -> StaticCookieCheck: ...


class ActiveCookieProbe(Protocol):
    """Real but lightweight authenticated probe for one provider."""

    async def probe(self, provider: CookieService) -> ActiveProbeResult: ...
