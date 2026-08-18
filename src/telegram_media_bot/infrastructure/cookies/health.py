from __future__ import annotations

from datetime import datetime

from telegram_media_bot.application.ports.cookie_health import StaticCookieChecker
from telegram_media_bot.domain.cookie_health import CookieHealthState, StaticCookieCheck
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager


class NetscapeStaticCookieChecker(StaticCookieChecker):
    """Static, network-free Cookie Health checks over the canonical combined cookie file."""

    def __init__(self, manager: NetscapeCookieManager) -> None:
        self._manager = manager

    def check(
        self,
        provider: CookieService,
        *,
        now: datetime,
        expiring_soon_hours: float,
    ) -> StaticCookieCheck:
        return self._manager.static_health(
            provider,
            now=now,
            expiring_soon_hours=expiring_soon_hours,
        )


class MissingCookieChecker(StaticCookieChecker):
    """Static checker used when no canonical cookie file is configured."""

    def check(
        self,
        provider: CookieService,
        *,
        now: datetime,
        expiring_soon_hours: float,
    ) -> StaticCookieCheck:
        del now, expiring_soon_hours
        return StaticCookieCheck(
            provider=provider,
            status=CookieHealthState.MISSING,
            file_ok=False,
            safe_reason="no canonical cookie file is configured",
        )
