from __future__ import annotations

from typing import Any

import pytest

import telegram_media_bot.workers.jobs as jobs_module
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.workers.jobs import cookie_health_poll_minutes, cookie_health_watcher


class FakeHealthService:
    def __init__(self) -> None:
        self.static_calls = 0

    def refresh_static(self) -> tuple[dict[object, object], tuple[object, ...]]:
        self.static_calls += 1
        return {}, ()


def test_cookie_health_cron_uses_sparse_minutes_for_45_minute_interval() -> None:
    assert cookie_health_poll_minutes(45) == {0, 15, 30, 45}


@pytest.mark.asyncio
async def test_cookie_health_scan_does_not_run_every_30_seconds(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = settings.model_dump()
    raw["cookie_health"]["enabled"] = True
    raw["cookie_health"]["expiry_watch_interval_minutes"] = 45
    raw["cookie_health"]["active_probe_interval_minutes"] = 0
    configured = Settings.model_validate(raw)
    service = FakeHealthService()
    monkeypatch.setattr(jobs_module, "CookieHealthService", FakeHealthService)
    now = 0.0
    monkeypatch.setattr(jobs_module, "monotonic", lambda: now)
    context: dict[str, Any] = {
        "settings": configured,
        "cookie_health_service": service,
    }

    for seconds in range(0, 2701, 30):
        now = float(seconds)
        await cookie_health_watcher(context)

    assert service.static_calls == 2
