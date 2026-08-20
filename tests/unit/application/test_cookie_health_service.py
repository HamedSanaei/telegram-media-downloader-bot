from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


class StubProbe:
    def __init__(self, results: dict[CookieService, ActiveProbeResult]) -> None:
        self._results = results
        self.calls: list[CookieService] = []

    async def probe(self, provider: CookieService) -> ActiveProbeResult:
        self.calls.append(provider)
        return self._results.get(
            provider,
            ActiveProbeResult(provider, CookieHealthState.UNVERIFIED),
        )


def _service(
    store: CookieHealthRepository,
    *,
    checker: StubChecker | None = None,
    probe: StubProbe | None = None,
    reminder_minutes: int = 180,
    recovery: bool = True,
    now: Callable[[], datetime] | None = None,
) -> CookieHealthService:
    return CookieHealthService(
        store=store,
        checker=checker or StubChecker({}),
        probe=probe or StubProbe({}),
        expiring_soon_hours=24,
        reminder_interval_minutes=reminder_minutes,
        recovery_notifications=recovery,
        probe_concurrency=2,
        now=now or (lambda: _NOW),
    )


def _probe(provider: CookieService, status: CookieHealthState) -> ActiveProbeResult:
    return ActiveProbeResult(provider=provider, status=status)


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
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.AUTH_FAILED}),
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
        recovery=True,
        now=lambda: _NOW + timedelta(hours=1),
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    assert alerts[0].recovery is True


def test_recovery_notification_can_be_disabled() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.AUTH_FAILED}),
    )
    _updated, alerts = service.refresh_static()
    assert len(alerts) == 1
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
        recovery=False,
        now=lambda: _NOW + timedelta(hours=1),
    )
    _updated, alerts = service.refresh_static()
    assert alerts == ()


async def test_active_probe_success_marks_healthy() -> None:
    store = FakeStore()
    service = _service(
        store,
        probe=StubProbe(
            {CookieService.INSTAGRAM: _probe(CookieService.INSTAGRAM, CookieHealthState.HEALTHY)}
        ),
    )
    results = await service.run_active_probes((CookieService.INSTAGRAM,))
    updated, alerts = service.apply_probe_results(results)
    health = updated[CookieService.INSTAGRAM]
    assert health.status is CookieHealthState.HEALTHY
    assert health.last_successful_auth_check_at == _NOW
    # Healthy after an AUTH_FAILED notification -> recovery alert.
    assert alerts == ()


async def test_active_probe_auth_failure_marks_auth_failed_and_alerts() -> None:
    store = FakeStore()
    service = _service(
        store,
        probe=StubProbe(
            {
                CookieService.INSTAGRAM: _probe(
                    CookieService.INSTAGRAM, CookieHealthState.AUTH_FAILED
                )
            }
        ),
    )
    results = await service.run_active_probes((CookieService.INSTAGRAM,))
    updated, alerts = service.apply_probe_results(results)
    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.AUTH_FAILED
    assert len(alerts) == 1
    assert alerts[0].new_state is CookieHealthState.AUTH_FAILED


async def test_active_probe_check_error_keeps_static_status() -> None:
    store = FakeStore()
    service = _service(
        store,
        checker=StubChecker({CookieService.INSTAGRAM: CookieHealthState.HEALTHY}),
        probe=StubProbe(
            {
                CookieService.INSTAGRAM: _probe(
                    CookieService.INSTAGRAM, CookieHealthState.CHECK_ERROR
                )
            }
        ),
    )
    service.refresh_static()
    results = await service.run_active_probes((CookieService.INSTAGRAM,))
    updated, alerts = service.apply_probe_results(results)
    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.HEALTHY
    assert updated[CookieService.INSTAGRAM].active is not None
    assert alerts == ()


async def test_unverified_probe_does_not_mark_healthy() -> None:
    store = FakeStore()
    service = _service(
        store,
        probe=StubProbe(
            {CookieService.INSTAGRAM: _probe(CookieService.INSTAGRAM, CookieHealthState.UNVERIFIED)}
        ),
    )
    results = await service.run_active_probes((CookieService.INSTAGRAM,))
    updated, alerts = service.apply_probe_results(results)
    assert updated[CookieService.INSTAGRAM].status is CookieHealthState.UNVERIFIED
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


async def test_probe_concurrency_is_bounded() -> None:
    store = FakeStore()
    probe = StubProbe({})
    service = _service(store, probe=probe, checker=StubChecker({}))
    service._probe_concurrency = 2
    results = await service.run_active_probes()
    assert set(results) == set(CookieService)
    assert len(probe.calls) == len(CookieService)
