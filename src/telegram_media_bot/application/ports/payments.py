"""Billing/payment persistence and gateway contracts (T015).

Keeps the domain and application layers free of ``sqlite3`` and provider implementation details.
The gateway is resolved through composition/infrastructure, never a provider ``if/elif`` chain in
domain or application code. ``BillingService`` accepts only a ``VerifiedPaymentResult``; it never
receives a raw provider callback. The atomic confirmation/reversal methods hide the single SQLite
transaction so order transition, transaction claim, entitlement grant creation/reversal, and
subscription recomputation share one commit (or roll back together).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentAttempt,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import EntitlementGrant, Subscription


class PaymentGateway(Protocol):
    """Provider-neutral gateway adapter contract implemented at the infrastructure boundary."""

    #: Stable provider identity owned by the adapter/registry (a bounded operator-controlled set).
    provider_id: PaymentProviderId

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        """Create an external checkout. The returned result is a redirect, never proof of payment."""

    def verify_callback(
        self,
        order: PaymentOrder,
        provider_payload: object,
    ) -> VerifiedPaymentResult:
        """Verify an untrusted provider message and normalize it into a project-owned result."""

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        """Query a pending order's provider state (used by future reconciliation)."""


class PaymentRepository(Protocol):
    """Durable payment order/attempt/provider-transaction persistence (WAL/SQLite at composition).

    ``confirm_order_atomic`` and ``reverse_order_atomic`` implement the highest-risk invariant of
    T015: all required economic mutations (order transition, provider transaction claim, attempt
    update, entitlement grant, subscription recomputation) happen in ONE SQLite transaction. The
    implementing repository is expected to re-validate the order snapshot/state inside that
    transaction and roll everything back on any failure.
    """

    def initialize(self) -> None: ...

    def save_order(self, order: PaymentOrder) -> None: ...

    def get_order(self, order_id: PaymentOrderId) -> PaymentOrder | None: ...

    def save_attempt(self, attempt: PaymentAttempt) -> None: ...

    def list_orders_by_user(self, user_id: int) -> tuple[PaymentOrder, ...]: ...

    def list_pending_orders(self, *, before: datetime) -> tuple[PaymentOrder, ...]: ...

    def count_orders_by_status(self) -> dict[str, int]: ...

    def claim_provider_transaction(
        self,
        *,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        order_id: PaymentOrderId,
    ) -> None:
        """Durably claim an economic transaction exactly once."""

    def get_claim_order(
        self,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
    ) -> PaymentOrderId | None: ...

    def confirm_order_atomic(
        self,
        *,
        grant: EntitlementGrant,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        expected_amount_minor: int,
        expected_currency: str,
        expected_order_reference: str,
        paid_at: datetime,
        now: datetime,
    ) -> Subscription:
        """Confirm one verified payment atomically and persist the recomputed subscription."""

    def reverse_order_atomic(
        self,
        *,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        reason: str,
        reversed_at: datetime,
        now: datetime,
    ) -> Subscription | None:
        """Mark a paid order refunded, reverse its grant, and recompute the subscription atomically."""
