"""T014 domain unit tests: models, calendar arithmetic, and snapshot helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_media_bot.domain.models import UserProfile
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    EntitlementSnapshot,
    GrantId,
    PlanId,
    SubscriptionPlan,
    add_calendar_months,
    compute_authorized_until,
    entitlement_snapshot_from_dict,
    entitlement_snapshot_to_dict,
    grant_windows,
    ordered_valid_grants,
    reserve_covering_window,
)


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Capability values
# --------------------------------------------------------------------------- #


def test_capability_values_are_stable() -> None:
    assert Capability.INSTAGRAM_PRIVATE_MEDIA.value == "instagram_private_media"
    assert Capability.INSTAGRAM_USER_SESSION_PREFERENCE.value == "instagram_user_session_preference"


# --------------------------------------------------------------------------- #
# Plan model validation
# --------------------------------------------------------------------------- #


def test_plan_supports_arbitrary_positive_duration() -> None:
    plan = SubscriptionPlan(
        plan_id=PlanId("p-99"),
        name="Anything",
        duration_months=37,
        price_minor=123,
        currency="eur",
        enabled=True,
        capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
    )
    assert plan.duration_months == 37


@pytest.mark.parametrize("months", [0, -1, -30])
def test_plan_rejects_non_positive_duration(months: int) -> None:
    with pytest.raises(ValueError):
        SubscriptionPlan(
            plan_id=PlanId("p"),
            name="x",
            duration_months=months,
            price_minor=100,
            currency="USD",
            enabled=True,
        )


@pytest.mark.parametrize("price", [-1, 1.5, True])
def test_plan_rejects_invalid_price(price: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        SubscriptionPlan(
            plan_id=PlanId("p"),
            name="x",
            duration_months=1,
            price_minor=price,  # type: ignore[arg-type]
            currency="USD",
            enabled=True,
        )


@pytest.mark.parametrize("currency", ["", "US", "USDollar", "12D"])
def test_plan_rejects_invalid_currency(currency: str) -> None:
    with pytest.raises(ValueError):
        SubscriptionPlan(
            plan_id=PlanId("p"),
            name="x",
            duration_months=1,
            price_minor=100,
            currency=currency,
            enabled=True,
        )


def test_plan_normalizes_currency_to_uppercase() -> None:
    plan = SubscriptionPlan(
        plan_id=PlanId("p"),
        name="x",
        duration_months=1,
        price_minor=100,
        currency="usd",
        enabled=True,
    )
    assert plan.currency == "USD"


def test_integer_minor_price_is_preserved() -> None:
    plan = SubscriptionPlan(
        plan_id=PlanId("p"),
        name="x",
        duration_months=2,
        price_minor=4900,
        currency="USD",
        enabled=True,
    )
    assert plan.price_minor == 4900


# --------------------------------------------------------------------------- #
# Grant model validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("months", [0, -5])
def test_grant_rejects_non_positive_duration(months: int) -> None:
    with pytest.raises(ValueError):
        EntitlementGrant(
            grant_id=GrantId("g"),
            user_id=1,
            plan_id=PlanId("p"),
            duration_months=months,
            confirmed_at=_utc(2026, 1, 1),
            source_type="test",
            source_reference="o1",
            created_at=_utc(2026, 1, 1),
        )


def test_grant_reversed_flag() -> None:
    grant = EntitlementGrant(
        grant_id=GrantId("g"),
        user_id=1,
        plan_id=PlanId("p"),
        duration_months=1,
        confirmed_at=_utc(2026, 1, 1),
        source_type="test",
        source_reference="o1",
        created_at=_utc(2026, 1, 1),
    )
    assert grant.reversed is False
    reversed_grant = EntitlementGrant(
        grant_id=GrantId("g2"),
        user_id=1,
        plan_id=PlanId("p"),
        duration_months=1,
        confirmed_at=_utc(2026, 1, 1),
        source_type="test",
        source_reference="o2",
        created_at=_utc(2026, 1, 1),
        reversed_at=_utc(2026, 2, 1),
        reversal_reason="refund",
    )
    assert reversed_grant.reversed is True


# --------------------------------------------------------------------------- #
# Calendar-month arithmetic
# --------------------------------------------------------------------------- #


def test_jan_31_plus_one_month_clamps_to_feb() -> None:
    assert add_calendar_months(_utc(2026, 1, 31), 1) == _utc(2026, 2, 28)
    # Leap year.
    assert add_calendar_months(_utc(2024, 1, 31), 1) == _utc(2024, 2, 29)


def test_jan_31_plus_two_months_sequential() -> None:
    assert add_calendar_months(_utc(2026, 1, 31), 2) == _utc(2026, 3, 31)


def test_feb_29_leap_year_transition() -> None:
    assert add_calendar_months(_utc(2024, 2, 29), 1) == _utc(2024, 3, 29)
    assert add_calendar_months(_utc(2024, 2, 29), 12) == _utc(2025, 2, 28)


def test_december_to_january() -> None:
    assert add_calendar_months(_utc(2026, 12, 15), 1) == _utc(2027, 1, 15)


def test_utc_midnight_boundary_is_stable() -> None:
    value = _utc(2026, 3, 1)
    result = add_calendar_months(value, 0)
    assert result == value
    assert result.tzinfo is UTC


def test_utc_boundary_full_year() -> None:
    assert add_calendar_months(_utc(2026, 8, 31), 1) == _utc(2026, 9, 30)


def test_month_arithmetic_requires_aware_datetime() -> None:
    with pytest.raises(ValueError):
        add_calendar_months(datetime(2026, 1, 31), 1)


def test_month_arithmetic_rejects_negative() -> None:
    with pytest.raises(ValueError):
        add_calendar_months(_utc(2026, 1, 31), -1)


# --------------------------------------------------------------------------- #
# Grant windows / stacking / reversal recomputation helpers
# --------------------------------------------------------------------------- #


def _grant(grant_id: str, confirmed: datetime, months: int = 1) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(grant_id),
        user_id=1,
        plan_id=PlanId("p"),
        duration_months=months,
        confirmed_at=confirmed,
        source_type="test",
        source_reference=grant_id,
        created_at=confirmed,
    )


def test_stacked_grants_extend_continuously() -> None:
    first = _grant("g1", _utc(2026, 1, 10), months=1)
    second = _grant("g2", _utc(2026, 1, 15), months=1)
    windows = grant_windows([first, second])
    assert windows[0][1] == _utc(2026, 1, 10)
    assert windows[0][2] == _utc(2026, 2, 10)
    # Second grant starts where the first ended, preserving continuous paid time.
    assert windows[1][1] == _utc(2026, 2, 10)
    assert windows[1][2] == _utc(2026, 3, 10)
    assert compute_authorized_until([first, second]) == _utc(2026, 3, 10)


def test_later_confirmed_grant_starts_late() -> None:
    first = _grant("g1", _utc(2026, 1, 1), months=1)
    late = _grant("g2", _utc(2026, 3, 1), months=1)
    windows = grant_windows([first, late])
    assert windows[1][1] == _utc(2026, 3, 1)  # gap respected
    assert compute_authorized_until([first, late]) == _utc(2026, 4, 1)


def test_reversed_grants_are_excluded_from_windows() -> None:
    first = _grant("g1", _utc(2026, 1, 1), months=1)
    reversed_grant = _grant("g2", _utc(2026, 1, 15), months=3)
    reversed_grant = EntitlementGrant(
        grant_id=reversed_grant.grant_id,
        user_id=1,
        plan_id=reversed_grant.plan_id,
        duration_months=reversed_grant.duration_months,
        confirmed_at=reversed_grant.confirmed_at,
        source_type="test",
        source_reference="g2",
        created_at=reversed_grant.created_at,
        reversed_at=_utc(2026, 2, 5),
        reversal_reason="refund",
    )
    assert ordered_valid_grants([first, reversed_grant]) == (first,)
    assert compute_authorized_until([first, reversed_grant]) == _utc(2026, 2, 1)


def test_all_reversed_grants_yield_no_valid_time() -> None:
    grant = _grant("g1", _utc(2026, 1, 1))
    reversed_grant = EntitlementGrant(
        grant_id=grant.grant_id,
        user_id=1,
        plan_id=grant.plan_id,
        duration_months=1,
        confirmed_at=grant.confirmed_at,
        source_type="test",
        source_reference="g1",
        created_at=grant.created_at,
        reversed_at=_utc(2026, 1, 20),
        reversal_reason="refund",
    )
    assert compute_authorized_until([reversed_grant]) is None


def test_capability_coverage_depends_on_plan() -> None:
    grant = _grant("g1", _utc(2026, 1, 1), months=1)
    plan_capabilities = {PlanId("p"): frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA})}
    covering = reserve_covering_window(
        [grant],
        capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
        at=_utc(2026, 1, 5),
        plan_capabilities=plan_capabilities,
    )
    assert covering is not None
    missing = reserve_covering_window(
        [grant],
        capability=Capability.INSTAGRAM_USER_SESSION_PREFERENCE,
        at=_utc(2026, 1, 5),
        plan_capabilities=plan_capabilities,
    )
    assert missing is None


# --------------------------------------------------------------------------- #
# Snapshot serialization (safe, JSON-compatible, replayable)
# --------------------------------------------------------------------------- #


def test_snapshot_round_trip() -> None:
    snapshot = EntitlementSnapshot(
        capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
        accepted_at=_utc(2026, 1, 31, 12, 30),
        authorized_until=_utc(2026, 2, 28, 23, 59),
        plan_id=PlanId("vip-1"),
        grant_id=GrantId("g1"),
    )
    restored = entitlement_snapshot_from_dict(entitlement_snapshot_to_dict(snapshot))
    assert restored == snapshot


# --------------------------------------------------------------------------- #
# Telegram Premium is NOT bot VIP (regression)
# --------------------------------------------------------------------------- #


def test_telegram_is_premium_enum_is_kept_separate() -> None:
    # UserProfile.is_premium is Telegram's own flag and is stored verbatim. The invariant that it
    # grants zero bot VIP capability is asserted at the service boundary in
    # tests/unit/application/test_entitlements.py: a Premium-only user is denied because a bot
    # subscription is decided exclusively by durable entitlement grants.
    profile = UserProfile(
        user_id=42,
        private_chat_id=900,
        username="premium_user",
        first_name="P",
        last_name=None,
        language_code="en",
        is_premium=True,
    )
    assert profile.is_premium is True
