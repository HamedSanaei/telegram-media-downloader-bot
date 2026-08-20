"""Cookie Health Center domain model.

Static checks are network-free and only inspect the canonical combined cookie file. Authentication
failures are learned passively from real user-requested extraction operations. Persisted state
drives transition alert deduplication and survives worker/container restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from telegram_media_bot.domain.cookies import CookieService


class CookieHealthState(StrEnum):
    HEALTHY = "healthy"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    AUTH_FAILED = "auth_failed"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNVERIFIED = "unverified"
    CHECK_ERROR = "check_error"


#: States that definitively block authenticated Instagram collection jobs (Part E).
BLOCKING_COOKIE_STATES = frozenset(
    {
        CookieHealthState.EXPIRED,
        CookieHealthState.AUTH_FAILED,
        CookieHealthState.MISSING,
        CookieHealthState.MALFORMED,
    }
)


@dataclass(frozen=True, slots=True)
class StaticCookieCheck:
    """Result of the network-free static validation for one provider."""

    provider: CookieService
    status: CookieHealthState
    #: Whether the canonical cookie file itself is readable and parseable.
    file_ok: bool
    record_count: int = 0
    earliest_expiry: datetime | None = None
    latest_expiry: datetime | None = None
    #: Number of records that failed Netscape parsing / were malformed.
    malformed_record_count: int = 0
    #: Safe, sanitized failure reason; never cookie values.
    safe_reason: str | None = None
    #: Whether the canonical file meets the expected security/permission contract.
    permission_ok: bool = True


@dataclass(frozen=True, slots=True)
class ActiveProbeResult:
    """Backward-compatible persisted shape for passive runtime authentication evidence.

    The historical field/class names remain so existing SQLite rows can be read. New records are
    created only from failures already returned by a real user-requested extraction; no probe is
    initiated by Cookie Health.
    """

    provider: CookieService
    status: CookieHealthState
    probed_url: str | None = None
    #: Legacy field retained for persisted-row compatibility; new passive signals leave it false.
    auth_required_endpoint: bool = False
    http_status: int | None = None
    elapsed_seconds: float | None = None
    safe_reason: str | None = None

    @property
    def conclusive(self) -> bool:
        return (
            self.status is CookieHealthState.HEALTHY or self.status is CookieHealthState.AUTH_FAILED
        )


@dataclass(frozen=True, slots=True)
class ProviderCookieHealth:
    """Complete persisted health snapshot for one provider."""

    provider: CookieService
    status: CookieHealthState
    static: StaticCookieCheck
    active: ActiveProbeResult | None = None
    last_checked_at: datetime | None = None
    last_successful_auth_check_at: datetime | None = None
    #: State that last produced an administrator alert (transition deduplication).
    last_notified_state: CookieHealthState | None = None
    last_reminder_at: datetime | None = None

    @property
    def expiry_delta(self) -> float | None:
        if self.static.earliest_expiry is None:
            return None
        return (self.static.earliest_expiry - datetime.now(UTC)).total_seconds()

    def with_status(self, status: CookieHealthState) -> Self:
        return type(self)(
            provider=self.provider,
            status=status,
            static=self.static,
            active=self.active,
            last_checked_at=self.last_checked_at,
            last_successful_auth_check_at=self.last_successful_auth_check_at,
            last_notified_state=self.last_notified_state,
            last_reminder_at=self.last_reminder_at,
        )
