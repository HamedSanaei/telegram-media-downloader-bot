"""Role-authorized VIP administration (T025, sections 24-27).

Admin operations NEVER fabricate a paid payment: a gift/test grant is an ``admin_grant``
entitlement with its own durable unique source reference, created through ``EntitlementService``
calendar-month stacking and audited to the Operator Logger. Revoking a gift reverses ONLY
admin-issued grants; paid time stays untouched. Suspension is a separate reversible operational
state that never mutates payment history. Every mutating method revalidates that the actor is an
admin at the Telegram layer; this service assumes that has already happened.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from telegram_media_bot.application.ports.subscriptions import (
    PlanCatalogRepository,
    SubscriptionRepository,
)
from telegram_media_bot.application.services.entitlements import EntitlementService
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.domain.errors import (
    EntitlementBackendError,
    PersistenceError,
)
from telegram_media_bot.domain.subscriptions import (
    EntitlementGrant,
    GrantId,
    PlanId,
    SubscriptionPlan,
)


@dataclass(frozen=True, slots=True)
class AdminVipActionResult:
    ok: bool
    message: str
    authorized_until: datetime | None = None
    grant_ids: tuple[str, ...] = ()


class VipAdminService:
    """Admin VIP management backed by the entitlement store, plans, and the payment logger."""

    def __init__(
        self,
        *,
        entitlements: EntitlementService,
        plans: PlanCatalogRepository,
        subscriptions: SubscriptionRepository,
        logger: PaymentAuditLogger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._plans = plans
        self._subscriptions = subscriptions
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- inspection -------------------------------------------------------------

    def inspect_user(self, user_id: int) -> dict[str, object]:
        subscription = self._subscriptions.get_subscription(user_id)
        grants = self._subscriptions.get_grants(user_id)
        return {
            "user_id": user_id,
            "subscription": subscription,
            "grants": grants,
            "admin_grants": tuple(g for g in grants if g.source_type == "admin_grant"),
            "paid_grants": tuple(g for g in grants if g.source_type != "admin_grant"),
        }

    # -- gift / test grant (NOT a fake payment) ---------------------------------

    def grant_gift(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        plan_id: PlanId,
        duration_months: int,
        now: datetime,
    ) -> AdminVipActionResult:
        try:
            plan = self._plans.get_plan(plan_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; action failed closed"
            ) from exc
        if plan is None:
            return AdminVipActionResult(False, "چنین پلنی وجود ندارد")
        if not plan.enabled:
            return AdminVipActionResult(False, "این پلن غیرفعال است")
        if duration_months <= 0:
            return AdminVipActionResult(False, "مدت باید مثبت باشد")
        grant = EntitlementGrant(
            grant_id=GrantId(f"grant-admin-{uuid.uuid4().hex}"),
            user_id=target_user_id,
            plan_id=plan.plan_id,
            duration_months=duration_months,
            confirmed_at=now,
            source_type="admin_grant",
            source_reference=f"admin:{actor_user_id}:{uuid.uuid4().hex}",
            created_at=now,
        )
        subscription = self._entitlements.activate_grant(grant, now=now)
        if self._logger is not None:
            self._logger.log_admin_vip_granted(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                grant=grant,
                authorized_until=subscription.authorized_until,
                now=now,
            )
        return AdminVipActionResult(
            True,
            "have grant",
            authorized_until=subscription.authorized_until,
            grant_ids=(str(grant.grant_id),),
        )

    # -- revoke ONLY admin-issued gifts ------------------------------------------

    def revoke_gifts(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        now: datetime,
    ) -> AdminVipActionResult:
        grants = self._subscriptions.get_grants(target_user_id)
        admin_grants = [g for g in grants if g.source_type == "admin_grant" and not g.reversed]
        if not admin_grants:
            return AdminVipActionResult(False, "هیچ هدیه فعالی برای این کاربر وجود ندارد")
        authorized: datetime | None = None
        revoked: list[str] = []
        for grant in admin_grants:
            subscription = self._entitlements.reverse_grant(
                grant.grant_id,
                reason="admin_revoke_gift",
                reversed_at=now,
                now=now,
            )
            authorized = subscription.authorized_until
            revoked.append(str(grant.grant_id))
        if self._logger is not None:
            self._logger.log_admin_vip_revoked(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                grant_ids=tuple(revoked),
                authorized_until=authorized,
                now=now,
            )
        return AdminVipActionResult(
            True,
            "revoked",
            authorized_until=authorized,
            grant_ids=tuple(revoked),
        )

    # -- operational suspension (no payment mutation) -----------------------------

    def set_suspended(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        suspended: bool,
        reason: str | None,
        now: datetime,
    ) -> AdminVipActionResult:
        self._subscriptions.set_suspension(
            target_user_id,
            suspended_at=now if suspended else None,
            reason=(reason or "admin_suspend") if suspended else None,
            now=now,
        )
        if self._logger is not None:
            self._logger.log_admin_vip_suspension(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                suspended=suspended,
                reason=reason,
                now=now,
            )
        subscription = self._subscriptions.get_subscription(target_user_id)
        return AdminVipActionResult(
            True,
            "suspended" if suspended else "unsuspended",
            authorized_until=subscription.authorized_until if subscription else None,
        )

    # -- plan catalog -------------------------------------------------------------

    def list_plans(self) -> tuple[SubscriptionPlan, ...]:
        return self._plans.list_plans()

    def save_plan(
        self,
        *,
        actor_user_id: int,
        plan: SubscriptionPlan,
        now: datetime,
        action: str = "upsert",
    ) -> None:
        self._plans.save_plan(plan)
        if self._logger is not None:
            self._logger.log_admin_plan_changed(
                actor_user_id=actor_user_id,
                plan_id=str(plan.plan_id),
                action=action,
                now=now,
            )


__all__ = ["AdminVipActionResult", "VipAdminService"]
