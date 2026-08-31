from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.domain.cookie_health import (
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.workers import jobs


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[CookieService, ProviderCookieHealth] = {}

    def initialize(self) -> None:
        return None

    def load_all(self) -> dict[CookieService, ProviderCookieHealth]:
        return dict(self.rows)

    def load(self, provider: CookieService) -> ProviderCookieHealth | None:
        return self.rows.get(provider)

    def save(self, health: ProviderCookieHealth) -> None:
        self.rows[health.provider] = health


class CountingStaticChecker:
    def __init__(self) -> None:
        self.calls = 0

    def check(
        self,
        provider: CookieService,
        *,
        now: datetime,
        expiring_soon_hours: float,
    ) -> StaticCookieCheck:
        del now, expiring_soon_hours
        self.calls += 1
        return StaticCookieCheck(
            provider,
            CookieHealthState.UNVERIFIED,
            file_ok=True,
            record_count=1,
        )


def test_worker_startup_and_cron_have_no_cookie_health_probe_path() -> None:
    root = Path(__file__).resolve().parents[3]
    settings_source = (root / "src/telegram_media_bot/workers/settings.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(settings_source)

    assert "GalleryDlCookieProbe" not in settings_source
    assert "cookie_health_watcher" not in settings_source
    assert "run_active_probes" not in settings_source
    assert not hasattr(jobs, "cookie_health_watcher")
    assert not hasattr(jobs, "cookie_health_poll_minutes")
    cron_functions = [
        call.args[0].id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "cron"
        and call.args
        and isinstance(call.args[0], ast.Name)
    ]
    assert cron_functions == ["maintenance_job", "audit_dispatch_job"]


def test_more_than_45_minutes_of_static_refreshes_have_no_network_dependency() -> None:
    checker = CountingStaticChecker()
    current = datetime(2026, 8, 20, tzinfo=UTC)
    service = CookieHealthService(MemoryStore(), checker, now=lambda: current)

    for _seconds in range(0, 45 * 60 + 31, 30):
        current = current + timedelta(seconds=30)
        service.refresh_static((CookieService.INSTAGRAM,))

    assert checker.calls == 92
    parameters = inspect.signature(CookieHealthService).parameters
    assert "probe" not in parameters
    assert "probe_concurrency" not in parameters
