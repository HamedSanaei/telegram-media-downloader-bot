from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.ports.cookie_health_repository import CookieHealthRepository
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
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


class FakeStore:
    def __init__(self) -> None:
        self._rows: dict[CookieService, ProviderCookieHealth] = {}

    def initialize(self) -> None:
        return None

    def load_all(self) -> dict[CookieService, ProviderCookieHealth]:
        return dict(self._rows)

    def load(self, provider: CookieService) -> ProviderCookieHealth | None:
        return self._rows.get(provider)

    def save(self, health: ProviderCookieHealth) -> None:
        self._rows[health.provider] = health


class StubChecker:
    def __init__(self, statuses: dict[CookieService, CookieHealthState]) -> None:
        self._statuses = statuses

    def check(
        self,
        provider: CookieService,
        *,
        now: datetime,
        expiring_soon_hours: float,
    ) -> StaticCookieCheck:
        del now, expiring_soon_hours
        status = self._statuses.get(provider, CookieHealthState.UNVERIFIED)
        return StaticCookieCheck(
            provider=provider,
            status=status,
            file_ok=status is not CookieHealthState.MISSING,
            record_count=0 if status is CookieHealthState.MISSING else 1,
        )


def _service(
    store: CookieHealthRepository,
    *,
    checker: StubChecker | None = None,
    reminder_minutes: int = 180,
    recovery: bool = True,
    now: Callable[[], datetime] | None = None,
) -> CookieHealthService:
    return CookieHealthService(
        store=store,
        checker=checker or StubChecker({}),
        expiring_soon_hours=24,
        reminder_interval_minutes=reminder_minutes,
        recovery_notifications=recovery,
        now=now or (lambda: _NOW),
    )


def test_static_refresh_persists_and_alerts_on_failure_transition() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
    )
    updated, alerts = service.refresh_static()
    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.EXPIRED
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.provider is CookieService.INSTAGRAM
    assert alert.previous_state is None
    assert alert.new_state is CookieHealthState.EXPIRED
    # Persisted last_notified_state survives for restart deduplication.
    persisted = store.load(CookieService.INSTAGRAM)
    assert persisted is not None
    assert persisted.last_notified_state is CookieHealthState.EXPIRED


def test_healthy_initial_state_does_not_alert() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
    )
    _updated, alerts = service.refresh_static()
    assert alerts == ()


def test_targeted_refresh_replaces_stale_persisted_missing_state() -> None:
    store = FakeStore()
    missing_service = _service(
        store,
        checker=StubChecker({CookieService.PINTEREST: CookieHealthState.MISSING}),
    )
    missing_service.refresh_static((CookieService.PINTEREST,))
    service = _service(
        store,
        checker=StubChecker({CookieService.PINTEREST: CookieHealthState.UNVERIFIED}),
    )

    updated, _alerts = service.refresh_static((CookieService.PINTEREST,))

    assert updated[CookieService.PINTEREST].status is CookieHealthState.UNVERIFIED
    persisted = store.load(CookieService.PINTEREST)
    assert persisted is not None
    assert persisted.status is CookieHealthState.UNVERIFIED
    assert persisted.static.record_count == 1


def test_same_failure_state_is_deduplicated_then_reminded() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
        reminder_minutes=180,
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    # Immediate repeat without a reminder interval passing -> no alert.
    _updated, alerts = service.refresh_static()
    assert alerts == ()
    # Simulate the reminder interval passing.
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
        reminder_minutes=180,
        now=lambda: _NOW + timedelta(hours=4),
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    assert alerts[0].reminder is True


def test_expiring_soon_then_expired_transition_alerts_once_each() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRING_SOON}),
    )
    _updated, alerts = service.refresh_static()
    assert [alert.new_state for alert in alerts] == [CookieHealthState.EXPIRING_SOON]
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
        now=lambda: _NOW + timedelta(hours=30),
    )
    _updated, alerts = service.refresh_static()
    assert [alert.new_state for alert in alerts] == [CookieHealthState.EXPIRED]
    assert alerts[0].previous_state is CookieHealthState.EXPIRING_SOON


def test_recovery_to_healthy_sends_recovery_alert_when_enabled() -> None:
    store = FakeStore()
    service = _service(store)
    assert service.update_from_auth_failure(CookieService.INSTAGRAM, safe_reason="rejected")
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
        recovery=True,
        now=lambda: _NOW + timedelta(hours=1),
    )
    _updated, alerts = service.refresh_static(clear_runtime_auth_failure=True)
    assert len(alerts) == 1
    assert alerts[0].recovery is True


def test_recovery_notification_can_be_disabled() -> None:
    store = FakeStore()
    service = _service(store)
    assert service.update_from_auth_failure(CookieService.INSTAGRAM, safe_reason="rejected")
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
        recovery=False,
        now=lambda: _NOW + timedelta(hours=1),
    )
    _updated, alerts = service.refresh_static(clear_runtime_auth_failure=True)
    assert alerts == ()


def test_runtime_auth_failure_updates_health_and_alerts() -> None:
    store = FakeStore()
    service = _service(store, checker=StubChecker({}))
    alert = service.update_from_auth_failure(
        CookieService.INSTAGRAM, safe_reason="cookies were rejected"
    )
    assert alert is not None
    assert alert.new_state is CookieHealthState.AUTH_FAILED
    persisted = store.load(CookieService.INSTAGRAM)
    assert persisted is not None
    assert persisted.status is CookieHealthState.AUTH_FAILED
    assert persisted.active is not None
    assert persisted.active.safe_reason == "cookies were rejected"


def test_runtime_auth_failure_log_does_not_expose_secret_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _service(FakeStore())

    service.update_from_auth_failure(
        CookieService.INSTAGRAM,
        safe_reason="sanitized-runtime-detail",
    )

    output = capsys.readouterr().out
    assert "cookie_health_runtime_auth_failure" in output
    assert "sanitized-runtime-detail" not in output


def test_static_refresh_preserves_only_passive_runtime_auth_failure() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
    )
    service.update_from_auth_failure(CookieService.INSTAGRAM, safe_reason="rejected")

    updated, _alerts = service.refresh_static((CookieService.INSTAGRAM,))

    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.AUTH_FAILED


def test_upload_refresh_clears_passive_auth_failure_without_network_validation() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.UNVERIFIED}),
    )
    service.update_from_auth_failure(CookieService.INSTAGRAM, safe_reason="rejected")

    updated, _alerts = service.refresh_static(
        (CookieService.INSTAGRAM,), clear_runtime_auth_failure=True
    )

    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.UNVERIFIED
    assert updated[CookieService.INSTAGRAM].active is None


def test_static_refresh_discards_legacy_network_probe_success() -> None:
    store = FakeStore()
    provider = CookieService.INSTAGRAM
    store.save(
        ProviderCookieHealth(
            provider=provider,
            status=CookieHealthState.HEALTHY,
            static=StaticCookieCheck(provider, CookieHealthState.UNVERIFIED, file_ok=True),
            active=ActiveProbeResult(
                provider,
                CookieHealthState.HEALTHY,
                probed_url="https://redacted.invalid/probe",
                auth_required_endpoint=True,
            ),
            last_successful_auth_check_at=_NOW,
        )
    )
    service = _service(
        store,
        checker=StubChecker({provider: CookieHealthState.UNVERIFIED}),
    )

    updated, _alerts = service.refresh_static((provider,))

    assert updated[provider].status is CookieHealthState.UNVERIFIED
    assert updated[provider].active is None
    assert updated[provider].last_successful_auth_check_at is None


def test_restart_persistence_prevents_repeat_alert(tmp_path: Path) -> None:
    store = SqliteCookieHealthRepository(tmp_path / "health.sqlite3")
    store.initialize()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    # A brand-new service instance over the same persisted store sees the notified state.
    restarted = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.EXPIRED}),
    )
    _updated, alerts = restarted.refresh_static()
    assert alerts == ()
