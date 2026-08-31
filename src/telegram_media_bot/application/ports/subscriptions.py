"""Entitlement persistence contracts (T014).

Keeps the domain and application layers free of ``sqlite3`` implementation details. A narrowly
scoped plan-catalog port and a subscription/grant repository mirror the smaller repository
boundary the project already uses (see ``application/ports/job_repository.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.subscriptions import (
    EntitlementGrant,
    GrantId,
    PlanId,
    Subscription,
    SubscriptionPlan,
)


class PlanCatalogRepository(Protocol):
    def initialize(self) -> None: ...

    def save_plan(self, plan: SubscriptionPlan) -> None: ...

    def get_plan(self, plan_id: PlanId) -> SubscriptionPlan | None: ...


class SubscriptionRepository(Protocol):
    def initialize(self) -> None: ...

    def get_grants(self, user_id: int) -> tuple[EntitlementGrant, ...]: ...

    def get_grant(self, grant_id: GrantId) -> EntitlementGrant | None: ...

    def get_grant_by_source(
        self,
        user_id: int,
        source_type: str,
        source_reference: str,
    ) -> EntitlementGrant | None: ...

    def create_grant_with_subscription(
        self,
        grant: EntitlementGrant,
        subscription: Subscription,
    ) -> None:
        """Insert a grant and upsert the recomputed subscription in one transaction."""

    def reverse_grant_with_subscription(
        self,
        grant_id: GrantId,
        *,
        reason: str,
        reversed_at: datetime,
        subscription: Subscription,
    ) -> None:
        """Mark a grant reversed (retaining the row) and persist the recomputed subscription."""

    def get_subscription(self, user_id: int) -> Subscription | None: ...

    def cancel_subscription(self, user_id: int, *, cancelled_at: datetime) -> None: ...
