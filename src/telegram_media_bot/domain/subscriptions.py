"""Provider-neutral VIP subscription and entitlement domain.

T014 introduces the durable foundation for the product-facing VIP feature. It deliberately and
only models plans, capabilities, immutable grants, an account projection, and authorization
snapshots. It contains no payment provider, Instagram credential, or Telegram purchasing logic.

``UserProfile.is_premium`` describes Telegram's own Premium flag and is completely unrelated: it
must never grant a bot VIP capability. VIP access is decided exclusively through
:class:`EntitlementService`, which reads durable, typed subscription grants.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

PlanId = NewType("PlanId", str)
GrantId = NewType("GrantId", str)


class Capability(StrEnum):
    """Typed product capability strings.

    T014 introduces the capability enum values as a foundation only. No Instagram behavior is
    activated by them in this milestone.
    """

    INSTAGRAM_PRIVATE_MEDIA = "instagram_private_media"
    INSTAGRAM_USER_SESSION_PREFERENCE = "instagram_user_session_preference"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    INACTIVE = "inactive"


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Add a whole number of calendar months in UTC, clamping to the last valid day.

    Example: ``2026-01-31`` + 1 month is ``2026-02-28`` (or ``2026-02-29`` on a leap year), and
    ``2026-01-31`` + 2 months is ``2026-03-31``. The input ``value`` must be timezone-aware so the
    arithmetic is deterministic and never uses a local timezone.
    """
    if months < 0:
        raise ValueError("months must be a non-negative integer")
    if isinstance(months, bool) or not isinstance(months, int):
        raise TypeError("months must be an int")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calendar-month arithmetic requires a timezone-aware datetime")
    total_months = value.month - 1 + months
    year = value.year + total_months // 12
    month = total_months % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    """An operator-owned, immutable catalog entry. Prices are integer minor units only.

    ``duration_months`` accepts any positive integer; the model never hardcodes 1/3/6/12. The
    commercial catalog is empty by default; T014 invents no pricing.
    """

    plan_id: PlanId
    name: str
    duration_months: int
    price_minor: int
    currency: str
    enabled: bool
    capabilities: frozenset[Capability] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.duration_months, bool) or not isinstance(self.duration_months, int):
            raise TypeError("duration_months must be a positive integer")
        if self.duration_months <= 0:
            raise ValueError("duration_months must be a positive integer")
        if isinstance(self.price_minor, bool) or not isinstance(self.price_minor, int):
            raise TypeError("price_minor must be an integer in minor units")
        if self.price_minor < 0:
            raise ValueError("price_minor cannot be negative")
        currency = str(self.currency).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter alphabetic code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    """An immutable economic entitlement row.

    A successful future payment will create one grant. Grants are never physically deleted:
    reversal marks ``reversed_at``/``reversal_reason`` and the row is retained as an audit record.
    ``source_type``/``source_reference`` form the stable unique (provider, order) reference used by
    a future billing layer to guarantee exactly-once grant creation.
    """

    grant_id: GrantId
    user_id: int
    plan_id: PlanId
    duration_months: int
    confirmed_at: datetime
    source_type: str
    source_reference: str
    created_at: datetime
    reversed_at: datetime | None = None
    reversal_reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.duration_months, bool) or not isinstance(self.duration_months, int):
            raise TypeError("duration_months must be a positive integer")
        if self.duration_months <= 0:
            raise ValueError("duration_months must be a positive integer")

    @property
    def reversed(self) -> bool:
        return self.reversed_at is not None


@dataclass(frozen=True, slots=True)
class Subscription:
    """Durable per-user subscription/account projection derived from valid grants."""

    user_id: int
    authorized_until: datetime | None
    cancelled_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    """Immutable, safe authorization snapshot persisted with an accepted protected job.

    Contains only what execution requires (capability, acceptance time, authorized expiry, and the
    grant/plan reference that justified the acceptance). It never contains payment secrets,
    Instagram credentials, cookies, or callback payloads. An accepted job may finish after the
    subscription expires because the snapshot was captured at durable acceptance time.
    """

    capability: Capability
    accepted_at: datetime
    authorized_until: datetime
    plan_id: PlanId
    grant_id: GrantId


def ordered_valid_grants(grants: Iterable[EntitlementGrant]) -> tuple[EntitlementGrant, ...]:
    """Non-reversed grants in deterministic payment-confirmation order."""
    valid = [grant for grant in grants if not grant.reversed]
    valid.sort(key=lambda grant: (grant.confirmed_at, str(grant.grant_id)))
    return tuple(valid)


def grant_windows(
    grants: Iterable[EntitlementGrant],
) -> tuple[tuple[EntitlementGrant, datetime, datetime], ...]:
    """Compute each valid grant's ``[start, end)`` window using sequential calendar arithmetic.

    ``start = max(grant.confirmed_at, preceding_expiry)`` and
    ``end = add_calendar_months(start, grant.duration_months)``. Stacking keeps paid time
    continuous because a later grant begins where the previous one ends.
    """
    windows: list[tuple[EntitlementGrant, datetime, datetime]] = []
    cursor: datetime | None = None
    for grant in ordered_valid_grants(grants):
        start = grant.confirmed_at if cursor is None else max(grant.confirmed_at, cursor)
        end = add_calendar_months(start, grant.duration_months)
        windows.append((grant, start, end))
        cursor = end
    return tuple(windows)


def compute_authorized_until(
    grants: Iterable[EntitlementGrant],
) -> datetime | None:
    """Authorized expiry from all valid, non-reversed grants (or ``None`` when none remain)."""
    windows = grant_windows(grants)
    return windows[-1][2] if windows else None


def reserve_covering_window(
    grants: Iterable[EntitlementGrant],
    *,
    capability: Capability,
    at: datetime,
    plan_capabilities: dict[PlanId, frozenset[Capability]],
) -> tuple[EntitlementGrant, datetime] | None:
    """Return the covering ``(grant, end)`` for ``capability`` at ``at``, if one exists.

    A grant window grants a capability only if the referenced plan includes it. Windows tile the
    subscription span deterministically, so the first matching window is the authoritative one.
    """
    for grant, start, end in grant_windows(grants):
        if start <= at < end and capability in plan_capabilities.get(grant.plan_id, frozenset()):
            return grant, end
    return None


def entitlement_snapshot_to_dict(snapshot: EntitlementSnapshot) -> dict[str, object]:
    return {
        "capability": snapshot.capability.value,
        "accepted_at": snapshot.accepted_at.isoformat(),
        "authorized_until": snapshot.authorized_until.isoformat(),
        "plan_id": str(snapshot.plan_id),
        "grant_id": str(snapshot.grant_id),
    }


def entitlement_snapshot_from_dict(data: dict[str, object]) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        capability=Capability(str(data["capability"])),
        accepted_at=datetime.fromisoformat(str(data["accepted_at"])),
        authorized_until=datetime.fromisoformat(str(data["authorized_until"])),
        plan_id=PlanId(str(data["plan_id"])),
        grant_id=GrantId(str(data["grant_id"])),
    )
