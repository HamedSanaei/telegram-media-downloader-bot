"""VIP entitlement authorization and ledger recomputation (T014 foundation).

``EntitlementService.authorize(...)`` is the application boundary a future protected operation calls
before durable acceptance. Public flows do not call it. Authorization fails closed when the
entitlement backend is unavailable, and ``UserProfile.is_premium`` (Telegram's own flag) is never
consulted here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_media_bot.application.ports.subscriptions import (
    PlanCatalogRepository,
    SubscriptionRepository,
)
from telegram_media_bot.domain.errors import (
    EntitlementBackendError,
    EntitlementCancelledError,
    EntitlementCapabilityMissingError,
    EntitlementExpiredError,
    EntitlementGrantNotFoundError,
    EntitlementInactiveError,
    EntitlementNoValidGrantError,
    EntitlementSuspendedError,
    PersistenceError,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    EntitlementSnapshot,
    GrantId,
    PlanId,
    Subscription,
    SubscriptionStatus,
    compute_authorized_until,
    grant_windows,
    reserve_covering_window,
)


class EntitlementService:
    """Deterministic, clock-injected VIP entitlement operations.

    The current time is always supplied by the caller (``accepted_at``/``reversed_at``/``now``) so
    tests control time precisely; business logic never calls ``datetime.now()``. All durable
    timestamps are UTC.
    """

    def __init__(
        self,
        *,
        plans: PlanCatalogRepository,
        subscriptions: SubscriptionRepository,
    ) -> None:
        self._plans = plans
        self._subscriptions = subscriptions

    def authorize(
        self,
        user_id: int,
        capability: Capability,
        *,
        accepted_at: datetime,
    ) -> EntitlementSnapshot:
        """Authorize ``capability`` for ``user_id`` at ``accepted_at`` or raise a typed denial.

        The returned snapshot records the acceptance instant and the authorized expiry so an
        already accepted job may finish even after the subscription lapses.
        """
        known = self._load_account(user_id)
        if known is None:
            raise EntitlementInactiveError("User has no subscription history")
        subscription, grants = known
        if subscription.suspended_at is not None:
            raise EntitlementSuspendedError(
                "Subscription is operationally suspended; access fails closed"
            )
        if subscription.cancelled_at is not None:
            raise EntitlementCancelledError("Subscription is cancelled")
        valid = grant_windows(grants)
        if not valid:
            raise EntitlementNoValidGrantError("No valid paid time remains")
        authorized_until = valid[-1][2]
        if authorized_until <= accepted_at:
            raise EntitlementExpiredError("Entitlement has expired")
        capabilities = self._capabilities_for(user_id, grants)
        covering = reserve_covering_window(
            grants,
            capability=capability,
            at=accepted_at,
            plan_capabilities=capabilities,
        )
        if covering is None:
            raise EntitlementCapabilityMissingError(
                "Capability is not covered by this subscription"
            )
        grant, _end = covering
        return EntitlementSnapshot(
            capability=capability,
            accepted_at=accepted_at.replace(tzinfo=UTC),
            authorized_until=authorized_until.replace(tzinfo=UTC),
            plan_id=grant.plan_id,
            grant_id=grant.grant_id,
        )

    def activate_grant(
        self,
        grant: EntitlementGrant,
        *,
        now: datetime,
    ) -> Subscription:
        """Persist one new grant and the deterministically recomputed subscription atomically."""
        self._require_plan(grant.plan_id)
        grants = self._grants(grant.user_id)
        if any(
            other.source_type == grant.source_type
            and other.source_reference == grant.source_reference
            for other in grants
        ):
            # Duplicate economic source: the exact-once guard lives at the schema too, but surface
            # a clear denial before the repository write when the in-memory source set already saw
            # this (provider, order) reference.
            from telegram_media_bot.domain.errors import DuplicateEntitlementGrantError

            raise DuplicateEntitlementGrantError("A grant for this economic source already exists")
        granted = (*grants, grant)
        authorized_until = compute_authorized_until(granted)
        subscription = Subscription(
            user_id=grant.user_id,
            authorized_until=authorized_until,
            cancelled_at=None,
            updated_at=now,
        )
        self._subscriptions.create_grant_with_subscription(grant, subscription)
        return subscription

    def reverse_grant(
        self,
        grant_id: GrantId,
        *,
        reason: str,
        reversed_at: datetime,
        now: datetime,
    ) -> Subscription:
        """Recompute the projection after reversing one grant; grant rows are retained.

        A reversal never subtracts arbitrary seconds. Every remaining valid grant is replayed in
        confirmation order; if no paid time remains the authorized expiry becomes ``None`` and
        access ends immediately.
        """
        try:
            target = self._subscriptions.get_grant(grant_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; authorization failed closed"
            ) from exc
        if target is None:
            raise EntitlementGrantNotFoundError("Grant does not exist")
        grants = self._grants(target.user_id)
        preserved = [grant for grant in grants if grant.grant_id != grant_id and not grant.reversed]
        authorized_until = compute_authorized_until(preserved)
        subscription = Subscription(
            user_id=target.user_id,
            authorized_until=authorized_until,
            cancelled_at=None,
            updated_at=now,
        )
        self._subscriptions.reverse_grant_with_subscription(
            grant_id,
            reason=reason,
            reversed_at=reversed_at,
            subscription=subscription,
        )
        return subscription

    def get_subscription(
        self,
        user_id: int,
        *,
        now: datetime,
    ) -> Subscription | None:
        try:
            return self._subscriptions.get_subscription(user_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; authorization failed closed"
            ) from exc

    # -- internal helpers ----------------------------------------------------

    def _load_account(
        self,
        user_id: int,
    ) -> tuple[Subscription, tuple[EntitlementGrant, ...]] | None:
        try:
            subscription = self._subscriptions.get_subscription(user_id)
            grants = self._subscriptions.get_grants(user_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; authorization failed closed"
            ) from exc
        if subscription is None:
            return None
        return subscription, grants

    def _grants(self, user_id: int) -> tuple[EntitlementGrant, ...]:
        try:
            return self._subscriptions.get_grants(user_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; authorization failed closed"
            ) from exc

    def _capabilities_for(
        self, user_id: int, grants: tuple[EntitlementGrant, ...]
    ) -> dict[PlanId, frozenset[Capability]]:
        mapping: dict[PlanId, frozenset[Capability]] = {}
        for grant in grants:
            if grant.plan_id in mapping:
                continue
            try:
                plan = self._plans.get_plan(grant.plan_id)
            except PersistenceError as exc:
                raise EntitlementBackendError(
                    "Entitlement backend is unavailable; authorization failed closed"
                ) from exc
            mapping[grant.plan_id] = plan.capabilities if plan is not None else frozenset()
        return mapping

    def _require_plan(self, plan_id: PlanId) -> None:
        try:
            plan = self._plans.get_plan(plan_id)
        except PersistenceError as exc:
            raise EntitlementBackendError(
                "Entitlement backend is unavailable; authorization failed closed"
            ) from exc
        if plan is None:
            from telegram_media_bot.domain.errors import ConfigurationError

            raise ConfigurationError("Referenced subscription plan does not exist")


def subscription_status(subscription: Subscription | None, now: datetime) -> SubscriptionStatus:
    """Derive the durable account state from a projection row at ``now`` (UTC)."""
    if subscription is None:
        return SubscriptionStatus.INACTIVE
    if subscription.cancelled_at is not None:
        return SubscriptionStatus.CANCELLED
    if subscription.suspended_at is not None:
        return SubscriptionStatus.SUSPENDED
    if subscription.authorized_until is None:
        return SubscriptionStatus.INACTIVE
    if subscription.authorized_until <= now:
        return SubscriptionStatus.EXPIRED
    return SubscriptionStatus.ACTIVE
