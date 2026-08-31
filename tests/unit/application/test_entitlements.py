"""T014 EntitlementService tests: authorization outcomes, grants, reversal, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_media_bot.application.services.entitlements import (
    EntitlementService,
    subscription_status,
)
from telegram_media_bot.domain.errors import (
    ConfigurationError,
    DuplicateEntitlementGrantError,
    EntitlementBackendError,
    EntitlementCancelledError,
    EntitlementCapabilityMissingError,
    EntitlementExpiredError,
    EntitlementGrantNotFoundError,
    EntitlementInactiveError,
    EntitlementNoValidGrantError,
    PersistenceError,
)
from telegram_media_bot.domain.models import UserProfile
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    GrantId,
    PlanId,
    SubscriptionPlan,
    SubscriptionStatus,
)
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)

VIP_CAP = Capability.INSTAGRAM_PRIVATE_MEDIA
OTHER_CAP = Capability.INSTAGRAM_USER_SESSION_PREFERENCE


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _plan(
    plan_id: str = "vip-1",
    months: int = 1,
    caps: frozenset[Capability] = frozenset({VIP_CAP}),
) -> SubscriptionPlan:
    return SubscriptionPlan(
        plan_id=PlanId(plan_id),
        name="VIP",
        duration_months=months,
        price_minor=4900,
        currency="USD",
        enabled=True,
        capabilities=caps,
    )


def _grant(
    grant_id: str,
    user_id: int,
    confirmed: datetime,
    plan_id: str = "vip-1",
    months: int = 1,
    source_reference: str | None = None,
) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(grant_id),
        user_id=user_id,
        plan_id=PlanId(plan_id),
        duration_months=months,
        confirmed_at=confirmed,
        source_type="test",
        source_reference=source_reference or grant_id,
        created_at=confirmed,
    )


def _grant_months(
    grant_id: str, user_id: int, confirmed: datetime, months: int
) -> EntitlementGrant:
    return _grant(grant_id, user_id, confirmed, months=months)


def _service(tmp_path: Path) -> tuple[EntitlementService, SqliteSubscriptionRepository]:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    return EntitlementService(plans=repo, subscriptions=repo), repo


# --------------------------------------------------------------------------- #
# No-subscription authorization
# --------------------------------------------------------------------------- #


def test_free_user_without_subscription_is_denied(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(EntitlementInactiveError):
        service.authorize(7, VIP_CAP, accepted_at=_utc(2026, 1, 1))


def test_telegram_premium_alone_grants_zero_bot_vip(tmp_path: Path) -> None:
    # Telegram's is_premium flag is never consulted; a Premium user with no subscription is denied
    # exactly like any Free user.
    profile = UserProfile(
        user_id=99,
        private_chat_id=100,
        username="tg_premium",
        first_name="T",
        last_name=None,
        language_code="en",
        is_premium=True,
    )
    assert profile.is_premium is True
    service, _ = _service(tmp_path)
    with pytest.raises(EntitlementInactiveError):
        service.authorize(99, VIP_CAP, accepted_at=_utc(2026, 1, 1))


# --------------------------------------------------------------------------- #
# Active / expired / capability scenarios
# --------------------------------------------------------------------------- #


def test_first_grant_activation_and_authorization(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    accepted = _utc(2026, 1, 15)
    service.activate_grant(_grant("g1", 1, accepted), now=accepted)
    snapshot = service.authorize(1, VIP_CAP, accepted_at=accepted)
    assert snapshot.capability is VIP_CAP
    assert snapshot.accepted_at == accepted
    assert snapshot.authorized_until == _utc(2026, 2, 15)


def test_renewal_preserves_paid_time(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    t1 = _utc(2026, 1, 10)
    t2 = _utc(2026, 1, 20)
    service.activate_grant(_grant("g1", 1, t1), now=t1)
    service.activate_grant(_grant("g2", 1, t2), now=t2)
    sub = service.get_subscription(1, now=t2)
    assert sub is not None
    assert sub.authorized_until == _utc(2026, 3, 10)


def test_active_subscription_status(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    t = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, t), now=t)
    sub = service.get_subscription(1, now=_utc(2026, 1, 15))
    assert subscription_status(sub, _utc(2026, 1, 15)) is SubscriptionStatus.ACTIVE


def test_expired_subscription_denied(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    accepted = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, accepted), now=accepted)
    with pytest.raises(EntitlementExpiredError):
        service.authorize(1, VIP_CAP, accepted_at=_utc(2026, 2, 2))
    sub = service.get_subscription(1, now=_utc(2026, 2, 2))
    assert subscription_status(sub, _utc(2026, 2, 2)) is SubscriptionStatus.EXPIRED


def test_capability_missing_denied(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan(caps=frozenset({VIP_CAP})))
    service = EntitlementService(plans=repo, subscriptions=repo)
    accepted = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, accepted), now=accepted)
    with pytest.raises(EntitlementCapabilityMissingError):
        service.authorize(1, OTHER_CAP, accepted_at=_utc(2026, 1, 15))


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_cancelled_subscription_denied(tmp_path: Path) -> None:
    service, repo = _service(tmp_path)
    accepted = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 5, accepted), now=accepted)
    repo.cancel_subscription(5, cancelled_at=_utc(2026, 1, 10))
    with pytest.raises(EntitlementCancelledError):
        service.authorize(5, VIP_CAP, accepted_at=_utc(2026, 1, 15))
    sub = service.get_subscription(5, now=_utc(2026, 1, 15))
    assert subscription_status(sub, _utc(2026, 1, 15)) is SubscriptionStatus.CANCELLED


# --------------------------------------------------------------------------- #
# Reversal recomputation
# --------------------------------------------------------------------------- #


def test_reversal_recomputes_from_remaining_grants(tmp_path: Path) -> None:
    service, repo = _service(tmp_path)
    t1 = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, t1), now=t1)
    service.activate_grant(_grant_months("g2", 1, _utc(2026, 1, 5), months=3), now=_utc(2026, 1, 5))
    # g1 (1 month) then g2 (+3 months, stacked from g1's end) -> authorized until May 1.
    projected = service.get_subscription(1, now=_utc(2026, 1, 10))
    assert projected is not None
    assert projected.authorized_until == _utc(2026, 5, 1)
    service.reverse_grant(
        GrantId("g1"), reason="refund", reversed_at=_utc(2026, 2, 5), now=_utc(2026, 2, 5)
    )
    # Reversal never subtracts arbitrary seconds: g2 alone is replayed from its confirmation.
    remaining = service.get_subscription(1, now=_utc(2026, 2, 5))
    assert remaining is not None
    assert remaining.authorized_until == _utc(2026, 4, 5)
    # The reversed grant is retained, not deleted.
    grants = repo.get_grants(1)
    assert any(g.grant_id == GrantId("g1") and g.reversed for g in grants)


def test_all_grants_reversed_ends_access_immediately(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    accepted = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, accepted), now=accepted)
    service.reverse_grant(
        GrantId("g1"), reason="refund", reversed_at=_utc(2026, 1, 15), now=_utc(2026, 1, 15)
    )
    sub = service.get_subscription(1, now=_utc(2026, 1, 15))
    assert sub is not None
    assert sub.authorized_until is None
    assert subscription_status(sub, _utc(2026, 1, 15)) is SubscriptionStatus.INACTIVE
    with pytest.raises(EntitlementNoValidGrantError):
        service.authorize(1, VIP_CAP, accepted_at=_utc(2026, 1, 16))


def test_reverse_unknown_grant_raises(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(EntitlementGrantNotFoundError):
        service.reverse_grant(
            GrantId("nope"), reason="x", reversed_at=_utc(2026, 1, 1), now=_utc(2026, 1, 1)
        )


# --------------------------------------------------------------------------- #
# Exact-once grant creation
# --------------------------------------------------------------------------- #


def test_duplicate_source_reference_is_rejected(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    accepted = _utc(2026, 1, 1)
    service.activate_grant(_grant("g1", 1, accepted, source_reference="order-1"), now=accepted)
    with pytest.raises(DuplicateEntitlementGrantError):
        service.activate_grant(_grant("g2", 1, accepted, source_reference="order-1"), now=accepted)
    # The schema-level unique index ALSO blocks it (in-app check bypassed).
    fresh = _service(tmp_path)[1]
    with pytest.raises(DuplicateEntitlementGrantError):
        service.activate_grant(_grant("g3", 1, accepted, source_reference="order-1"), now=accepted)
    assert fresh.get_grant_by_source(1, "test", "order-1") is not None


def test_missing_plan_is_configured_error(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    service = EntitlementService(plans=repo, subscriptions=repo)
    with pytest.raises(ConfigurationError):
        service.activate_grant(
            _grant("g1", 1, _utc(2026, 1, 1), plan_id="nope"), now=_utc(2026, 1, 1)
        )


# --------------------------------------------------------------------------- #
# Fail-closed on backend unavailability
# --------------------------------------------------------------------------- #


class _FailingRepo:
    """A SubscriptionRepository whose reads raise PersistenceError."""

    def __init__(self) -> None:
        self.calls = 0

    def get_subscription(self, user_id: int) -> None:
        self.calls += 1
        raise PersistenceError("store down")

    def get_grants(self, user_id: int) -> None:
        raise PersistenceError("store down")


class _StubPlans:
    def get_plan(self, plan_id: PlanId) -> SubscriptionPlan:
        return _plan()


def test_backend_failure_fails_closed(tmp_path: Path) -> None:
    failing = _FailingRepo()
    service = EntitlementService(plans=_StubPlans(), subscriptions=failing)  # type: ignore[arg-type]
    with pytest.raises(EntitlementBackendError):
        service.authorize(1, VIP_CAP, accepted_at=_utc(2026, 1, 1))
    assert failing.calls >= 1
