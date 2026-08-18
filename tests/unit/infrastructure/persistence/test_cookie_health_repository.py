from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.domain.cookie_health import (
    ActiveProbeResult,
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.infrastructure.persistence.sqlite_cookie_health import (
    SqliteCookieHealthRepository,
)

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _health(provider: CookieService) -> ProviderCookieHealth:
    return ProviderCookieHealth(
        provider=provider,
        status=CookieHealthState.AUTH_FAILED,
        static=StaticCookieCheck(
            provider=provider,
            status=CookieHealthState.EXPIRED,
            file_ok=True,
            record_count=3,
            earliest_expiry=_NOW,
            latest_expiry=_NOW,
            safe_reason="cookies expired",
        ),
        active=ActiveProbeResult(
            provider=provider,
            status=CookieHealthState.AUTH_FAILED,
            safe_reason="login rejected",
        ),
        last_checked_at=_NOW,
        last_successful_auth_check_at=_NOW,
        last_notified_state=CookieHealthState.AUTH_FAILED,
        last_reminder_at=_NOW,
    )


def test_cookie_health_repository_roundtrip_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "state" / "jobs.sqlite3"
    store = SqliteCookieHealthRepository(path)
    store.initialize()

    store.save(_health(CookieService.INSTAGRAM))
    store.save(_health(CookieService.YOUTUBE))

    # A brand-new repository over the same database behaves like a process restart.
    restarted = SqliteCookieHealthRepository(path)
    restarted.initialize()
    loaded = restarted.load_all()
    assert set(loaded) == {CookieService.INSTAGRAM, CookieService.YOUTUBE}
    health = loaded[CookieService.INSTAGRAM]
    assert health.status is CookieHealthState.AUTH_FAILED
    assert health.static.record_count == 3
    assert health.active is not None
    assert health.active.safe_reason == "login rejected"
    assert health.last_notified_state is CookieHealthState.AUTH_FAILED
    assert health.last_reminder_at == _NOW

    single = restarted.load(CookieService.PINTEREST)
    assert single is None
