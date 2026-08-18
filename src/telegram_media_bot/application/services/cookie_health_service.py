"""Cookie Health Center orchestration.

The service combines the network-free static check with optional lightweight authenticated
probes, persists the combined health state, and decides which state transitions deserve an
administrator alert. Alert decisions are pure and based only on persisted state, so restarting
the worker never resets deduplication.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from telegram_media_bot.application.ports.cookie_health import (
    ActiveCookieProbe,
    StaticCookieChecker,
)
from telegram_media_bot.application.ports.cookie_health_repository import CookieHealthRepository
from telegram_media_bot.domain.cookie_health import (
    ActiveProbeResult,
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService

_REMINDER_STATES = frozenset(
    {
        CookieHealthState.EXPIRED,
        CookieHealthState.AUTH_FAILED,
        CookieHealthState.MISSING,
        CookieHealthState.MALFORMED,
        CookieHealthState.CHECK_ERROR,
    }
)

_COOKIE_SERVICES = tuple(CookieService)


@dataclass(frozen=True, slots=True)
class CookieHealthAlert:
    provider: CookieService
    previous_state: CookieHealthState | None
    new_state: CookieHealthState
    health: ProviderCookieHealth
    recovery: bool = False

    @property
    def reminder(self) -> bool:
        return self.previous_state == self.new_state


class CookieHealthService:
    def __init__(
        self,
        store: CookieHealthRepository,
        checker: StaticCookieChecker,
        probe: ActiveCookieProbe,
        *,
        expiring_soon_hours: float = 24,
        reminder_interval_minutes: int = 180,
        recovery_notifications: bool = True,
        probe_concurrency: int = 2,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._checker = checker
        self._probe = probe
        self._expiring_soon_hours = expiring_soon_hours
        self._reminder_interval = timedelta(minutes=reminder_interval_minutes)
        self._recovery_notifications = recovery_notifications
        self._probe_concurrency = max(1, probe_concurrency)
        self._now = now or (lambda: datetime.now(UTC))

    def providers(self) -> tuple[CookieService, ...]:
        return _COOKIE_SERVICES

    def all_health(self) -> dict[CookieService, ProviderCookieHealth]:
        stored = self._store.load_all()
        return {
            provider: stored.get(provider, _unchecked(provider)) for provider in _COOKIE_SERVICES
        }

    def refresh_static(
        self,
        providers: tuple[CookieService, ...] | None = None,
    ) -> tuple[dict[CookieService, ProviderCookieHealth], tuple[CookieHealthAlert, ...]]:
        """Run network-free static checks, persist the merged state, and return transitions."""
        checked_at = self._now()
        targets = providers or _COOKIE_SERVICES
        stored = self._store.load_all()
        updated: dict[CookieService, ProviderCookieHealth] = {}
        alerts: list[CookieHealthAlert] = []
        for provider in targets:
            check = self._checker.check(
                provider,
                now=checked_at,
                expiring_soon_hours=self._expiring_soon_hours,
            )
            previous = stored.get(provider)
            merged = _merge_static(previous, check, checked_at=checked_at)
            self._store.save(merged)
            updated[provider] = merged
            alert = self._transition_alert(previous, merged, now=checked_at)
            if alert is not None:
                alerts.append(alert)
                self._store.save(alert.health)
        return updated, tuple(alerts)

    async def run_active_probes(
        self,
        providers: tuple[CookieService, ...] | None = None,
    ) -> dict[CookieService, ActiveProbeResult]:
        """Run lightweight authenticated probes with bounded concurrency."""
        targets = providers or _COOKIE_SERVICES
        semaphore = asyncio.Semaphore(self._probe_concurrency)

        async def guarded(provider: CookieService) -> ActiveProbeResult:
            async with semaphore:
                return await self._probe.probe(provider)

        results = await asyncio.gather(*(guarded(provider) for provider in targets))
        return dict(zip(targets, results, strict=True))

    def apply_probe_results(
        self,
        results: dict[CookieService, ActiveProbeResult],
    ) -> tuple[dict[CookieService, ProviderCookieHealth], tuple[CookieHealthAlert, ...]]:
        """Merge probe outcomes into persisted health and return transitions."""
        checked_at = self._now()
        stored = self._store.load_all()
        updated: dict[CookieService, ProviderCookieHealth] = {}
        alerts: list[CookieHealthAlert] = []
        for provider, result in results.items():
            previous = stored.get(provider) or _unchecked(provider)
            merged = _merge_probe(previous, result, now=checked_at)
            self._store.save(merged)
            updated[provider] = merged
            alert = self._transition_alert(previous, merged, now=checked_at)
            if alert is not None:
                alerts.append(alert)
                self._store.save(alert.health)
        return updated, tuple(alerts)

    def update_from_auth_failure(
        self,
        provider: CookieService,
        *,
        safe_reason: str | None,
    ) -> CookieHealthAlert | None:
        """Mark a provider AUTH_FAILED after a real runtime authentication failure."""
        checked_at = self._now()
        previous = self._store.load(provider) or _unchecked(provider)
        active = ActiveProbeResult(
            provider=provider,
            status=CookieHealthState.AUTH_FAILED,
            safe_reason=safe_reason,
        )
        merged = _merge_probe(previous, active, now=checked_at)
        self._store.save(merged)
        return self._transition_alert(previous, merged, now=checked_at)

    def _transition_alert(
        self,
        previous: ProviderCookieHealth | None,
        current: ProviderCookieHealth,
        *,
        now: datetime,
    ) -> CookieHealthAlert | None:
        previous_state = previous.status if previous is not None else None
        previous_notified = previous.last_notified_state if previous is not None else None
        reminder_at = previous.last_reminder_at if previous is not None else None
        if not _should_alert(
            previous_notified=previous_notified,
            new_state=current.status,
            now=now,
            last_reminder_at=reminder_at,
            reminder_interval=self._reminder_interval,
            recovery_notifications=self._recovery_notifications,
        ):
            return None
        recovery = current.status is CookieHealthState.HEALTHY and previous_state not in {
            None,
            CookieHealthState.HEALTHY,
            CookieHealthState.UNVERIFIED,
        }
        # Persist the notification markers on the alert's health so worker/container restarts
        # never reset alert deduplication.
        notified_health = ProviderCookieHealth(
            provider=current.provider,
            status=current.status,
            static=current.static,
            active=current.active,
            last_checked_at=current.last_checked_at,
            last_successful_auth_check_at=current.last_successful_auth_check_at,
            last_notified_state=current.status,
            last_reminder_at=now,
        )
        return CookieHealthAlert(
            provider=current.provider,
            previous_state=previous_state,
            new_state=current.status,
            health=notified_health,
            recovery=recovery,
        )


def _should_alert(
    *,
    previous_notified: CookieHealthState | None,
    new_state: CookieHealthState,
    now: datetime,
    last_reminder_at: datetime | None,
    reminder_interval: timedelta,
    recovery_notifications: bool,
) -> bool:
    if previous_notified is None:
        # First observation: only report genuine problems, never a quiet healthy/unverified start.
        return new_state not in {CookieHealthState.HEALTHY, CookieHealthState.UNVERIFIED}
    if new_state == previous_notified:
        if new_state in _REMINDER_STATES:
            if last_reminder_at is None:
                return False
            return now - last_reminder_at >= reminder_interval
        return False
    if new_state is CookieHealthState.HEALTHY:
        return recovery_notifications
    return True


def _merge_static(
    previous: ProviderCookieHealth | None,
    check: StaticCookieCheck,
    *,
    checked_at: datetime,
) -> ProviderCookieHealth:
    active = previous.active if previous is not None else None
    status = _combined_status(check.status, active.status if active is not None else None)
    return ProviderCookieHealth(
        provider=check.provider,
        status=status,
        static=check,
        active=active,
        last_checked_at=checked_at,
        last_successful_auth_check_at=(
            previous.last_successful_auth_check_at if previous is not None else None
        ),
        last_notified_state=(previous.last_notified_state if previous is not None else None),
        last_reminder_at=previous.last_reminder_at if previous is not None else None,
    )


def _merge_probe(
    previous: ProviderCookieHealth,
    result: ActiveProbeResult,
    *,
    now: datetime,
) -> ProviderCookieHealth:
    status = _combined_status(previous.static.status, result.status)
    last_successful = previous.last_successful_auth_check_at
    if result.status is CookieHealthState.HEALTHY:
        last_successful = now
    return ProviderCookieHealth(
        provider=result.provider,
        status=status,
        static=previous.static,
        active=result,
        last_checked_at=now,
        last_successful_auth_check_at=last_successful,
        last_notified_state=previous.last_notified_state,
        last_reminder_at=previous.last_reminder_at,
    )


def _combined_status(
    static: CookieHealthState, active: CookieHealthState | None
) -> CookieHealthState:
    if active is CookieHealthState.AUTH_FAILED:
        return CookieHealthState.AUTH_FAILED
    if active is CookieHealthState.HEALTHY:
        return CookieHealthState.HEALTHY
    return static


def _unchecked(provider: CookieService) -> ProviderCookieHealth:
    return ProviderCookieHealth(
        provider=provider,
        status=CookieHealthState.UNVERIFIED,
        static=StaticCookieCheck(
            provider=provider,
            status=CookieHealthState.UNVERIFIED,
            file_ok=False,
            safe_reason="not checked yet",
        ),
    )
