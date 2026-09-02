"""Admin VIP management tests (T025).

Gift/test grants are a distinct `admin_grant` economic source (never a fake payment), stack by
calendar-month rules, are idempotent, are audited to the Operator Logger, and only admin-issued
grants are reversible. Suspension is operational only and never touches payment history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.entitlements import EntitlementService
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.application.services.vip_admin import VipAdminService
from telegram_media_bot.domain.subscriptions import (
    Capability,
    PlanId,
    Subscription,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)

PLAN = SubscriptionPlan(
    plan_id=PlanId("vip-1"),
    name="VIP",
    duration_months=1,
    price_minor=250_000,
    currency="IRT",
    enabled=True,
    capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
)


def _now() -> datetime:
    return datetime(2026, 1, 15, tzinfo=UTC)


def _make(
    tmp_path: Path,
) -> tuple[VipAdminService, SqliteSubscriptionRepository, SqliteAuditRepository]:
    subscriptions = SqliteSubscriptionRepository(tmp_path / "state.sqlite3")
    subscriptions.initialize()
    subscriptions.save_plan(PLAN)
    entitlements = EntitlementService(plans=subscriptions, subscriptions=subscriptions)
    audit_repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    audit_repository.initialize()
    audit_repository.reconcile_config((-1001234567890,))
    audit = AuditService(audit_repository, enabled=True)
    logger = PaymentAuditLogger(audit, payment_events_enabled=True)
    admin = VipAdminService(
        entitlements=entitlements,
        plans=subscriptions,
        subscriptions=subscriptions,
        logger=logger,
    )
    return admin, subscriptions, audit_repository


def test_grant_gift_creates_admin_source_grant_and_audits(tmp_path: Path) -> None:
    admin, subscriptions, audit_repo = _make(tmp_path)

    result = admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PLAN.plan_id, duration_months=2, now=_now()
    )
    assert result.ok
    grants = subscriptions.get_grants(7)
    assert len(grants) == 1
    grant = grants[0]
    assert grant.source_type == "admin_grant"
    assert grant.source_reference.startswith("admin:99:")
    assert grant.duration_months == 2
    assert result.authorized_until is not None
    item = audit_repo.claim_pending()[0]
    assert "admin_id: 99" in item.event.message and "user_id: 7" in item.event.message


def test_grant_gift_requires_enabled_plan(tmp_path: Path) -> None:
    admin, _, _ = _make(tmp_path)
    admin.save_plan(
        actor_user_id=99,
        plan=SubscriptionPlan(
            plan_id=PlanId("vip-2"),
            name="VIP-2",
            duration_months=1,
            price_minor=99_000,
            currency="IRT",
            enabled=False,
            capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
        ),
        now=_now(),
    )
    result = admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PlanId("vip-2"), duration_months=1, now=_now()
    )
    assert not result.ok
    assert "غیرفعال" in result.message
    # No phantom grant or payment row: entitlement volume unchanged.
    assert admin.inspect_user(7)["grants"] == ()


def test_gift_stacks_with_paid_and_revoke_keeps_paid_time(tmp_path: Path) -> None:
    admin, subscriptions, _ = _make(tmp_path)
    # A paid grant exists first (created out-of-band as a real subscription source).
    from telegram_media_bot.domain.subscriptions import EntitlementGrant, GrantId

    paid = EntitlementGrant(
        grant_id=GrantId("grant-paid-1"),
        user_id=7,
        plan_id=PLAN.plan_id,
        duration_months=1,
        confirmed_at=_now() - timedelta(days=10),
        source_type="provider_payment",
        source_reference="order:paid-1",
        created_at=_now() - timedelta(days=10),
    )
    subscriptions.create_grant_with_subscription(
        paid,
        Subscription(
            user_id=7,
            authorized_until=_now() + timedelta(days=20),
            cancelled_at=None,
            updated_at=_now(),
        ),
    )
    gift = admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PLAN.plan_id, duration_months=1, now=_now()
    )
    assert gift.ok
    assert len(subscriptions.get_grants(7)) == 2

    revoked = admin.revoke_gifts(actor_user_id=99, target_user_id=7, now=_now())
    assert revoked.ok
    grants = subscriptions.get_grants(7)
    assert len(grants) == 2  # both rows persist; the admin grant is reversed
    reversed_grant = grants[0] if grants[0].reversed else grants[1]
    assert reversed_grant.reversed
    assert (
        grants[0].source_type == "provider_payment" or grants[1].source_type == "provider_payment"
    )
    # Paid time remains: authorized_until is still in the future from the paid grant.
    remaining = (grants[0].source_type != "admin_grant" and grants[0]) or grants[1]
    assert not remaining.reversed
    assert revoked.authorized_until is not None
    assert revoked.authorized_until > _now()


def test_revoke_without_gifts_is_noop(tmp_path: Path) -> None:
    admin, _, _ = _make(tmp_path)
    result = admin.revoke_gifts(actor_user_id=99, target_user_id=7, now=_now())
    assert not result.ok
    assert "هدیه" in result.message


def test_suspend_does_not_mutate_payment_history(tmp_path: Path) -> None:
    admin, subscriptions, _ = _make(tmp_path)
    admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PLAN.plan_id, duration_months=1, now=_now()
    )

    suspended = admin.set_suspended(
        actor_user_id=99, target_user_id=7, suspended=True, reason="abuse", now=_now()
    )
    assert suspended.ok
    state = admin.inspect_user(7)
    subscription = state["subscription"]
    assert isinstance(subscription, Subscription)
    assert subscription.suspended_at is not None
    # Grants untouched: no reversal happened.
    grants = subscriptions.get_grants(7)
    assert len(grants) == 1 and not grants[0].reversed

    unsuspended = admin.set_suspended(
        actor_user_id=99, target_user_id=7, suspended=False, reason=None, now=_now()
    )
    assert unsuspended.ok
    state = admin.inspect_user(7)
    subscription = state["subscription"]
    assert isinstance(subscription, Subscription)
    assert subscription.suspended_at is None


def test_suspended_subscription_denies_entitlement(tmp_path: Path) -> None:
    admin, subscriptions, _ = _make(tmp_path)
    admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PLAN.plan_id, duration_months=1, now=_now()
    )
    admin.set_suspended(
        actor_user_id=99, target_user_id=7, suspended=True, reason="abuse", now=_now()
    )
    entitlements = EntitlementService(plans=subscriptions, subscriptions=subscriptions)
    from telegram_media_bot.domain.errors import EntitlementSuspendedError

    try:
        entitlements.authorize(7, Capability.INSTAGRAM_PRIVATE_MEDIA, accepted_at=_now())
        raise AssertionError("suspended subscription must deny authorization")
    except EntitlementSuspendedError:
        pass


def test_plan_catalog_admin_ops(tmp_path: Path) -> None:
    admin, _, _ = _make(tmp_path)
    created = SubscriptionPlan(
        plan_id=PlanId("vip-3"),
        name="VIP3",
        duration_months=3,
        price_minor=600_000,
        currency="IRT",
        enabled=True,
        capabilities=frozenset(
            {
                Capability.INSTAGRAM_PRIVATE_MEDIA,
                Capability.INSTAGRAM_USER_SESSION_PREFERENCE,
            }
        ),
    )
    admin.save_plan(actor_user_id=99, plan=created, now=_now())
    plans = admin.list_plans()
    assert any(plan.plan_id == PlanId("vip-3") for plan in plans)
    disabled = SubscriptionPlan(
        plan_id=PlanId("vip-3"),
        name="VIP3",
        duration_months=3,
        price_minor=600_000,
        currency="IRT",
        enabled=False,
        capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
    )
    admin.save_plan(actor_user_id=99, plan=disabled, now=_now())
    plans = admin.list_plans()
    disabled_plan = next(plan for plan in plans if plan.plan_id == PlanId("vip-3"))
    assert not disabled_plan.enabled
    # Disabled plan cannot open a new gift; existing grants remain valid.
    result = admin.grant_gift(
        actor_user_id=99, target_user_id=11, plan_id=PlanId("vip-3"), duration_months=1, now=_now()
    )
    assert not result.ok


def test_inspect_user_sanitized_state(tmp_path: Path) -> None:
    admin, _, _ = _make(tmp_path)
    admin.grant_gift(
        actor_user_id=99, target_user_id=7, plan_id=PLAN.plan_id, duration_months=1, now=_now()
    )
    state = admin.inspect_user(7)
    assert state["user_id"] == 7
    assert isinstance(state["admin_grants"], tuple) and len(state["admin_grants"]) == 1
    assert state["paid_grants"] == ()
    assert isinstance(state["subscription"], Subscription)
