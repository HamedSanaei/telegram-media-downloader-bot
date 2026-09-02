"""Billing/payment persistence and gateway contracts (T015/T024/T025).

Keeps the domain and application layers free of ``sqlite3`` and provider implementation details.
The gateway is resolved through composition/infrastructure, never a provider ``if/elif`` chain in
domain or application code. ``BillingService`` accepts only a ``VerifiedPaymentResult``; it never
receives a raw provider callback.

Callback contract: providers call the companion with provider-specific bodies (signed JSON IPN,
unsigned form POST, or a GET with no body). None of them is payment proof. A registered
``PaymentCallbackAdapter`` normalizes the untrusted trigger, then the reconciliation service runs a
point-in-time ``PaymentGateway.query_payment`` and ``BillingService`` settles only the normalized
``VerifiedPaymentResult`` inside one SQLite transaction.

Creation contract: the one permitted provider create POST is durably reserved before any network
byte leaves the process (``begin_creation_attempt``), and surviving states (``created``,
``ambiguous``) resolve only through read-only inquiry — never by issuing another create.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentAttempt,
    PaymentCreationReservation,
    PaymentCreationState,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import EntitlementGrant, Subscription


class PaymentGateway(Protocol):
    """Provider-neutral gateway adapter contract implemented at the infrastructure boundary.

    ``create_payment`` must follow the durable creation-reservation protocol defined on the
    payment repository: the single create mutation is reserved before the POST and resolved
    (created/ambiguous/failed) after it. ``query_payment`` is read-only and is the only path that
    may retry transient failures. Callbacks are handled by separate callback adapters, never here.
    """

    #: Stable provider identity owned by the adapter/registry (a bounded operator-controlled set).
    provider_id: PaymentProviderId

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        """Create exactly one external checkout. The returned result is a redirect, never proof."""

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        """Pop point-in-time payment state. Read-only; may retry only transient failures."""

    def available_for_new_checkout(self) -> bool:
        """Whether NEW checkout creation is currently allowed (credentials/URL/switch valid).

        Disabling a provider blocks new checkout only; existing pending orders remain queryable
        and confirmable through ``query_payment``.
        """


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

    # -- exactly-once provider create mutation (T024) --------------------------

    def get_creation_reservation(
        self, order_id: PaymentOrderId
    ) -> PaymentCreationReservation | None:
        """Load the durable create-reservation row, if any."""

    def begin_creation_attempt(
        self,
        *,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        merchant_reference: str,
        attempted_at: datetime,
    ) -> PaymentCreationReservation:
        """Durably reserve the ONE create POST and classify everything before the network call.

        Raises ``PaymentCreationReservedError`` when the order already has a started creation
        attempt; recovery from any surviving state is inquiry-only.
        """

    def resolve_creation_attempt(
        self,
        *,
        order_id: PaymentOrderId,
        state: PaymentCreationState,
        error_code: str | None,
        resolved_at: datetime,
    ) -> None:
        """Persist the single-attempt outcome. A definitive failure stops automatic inquiry."""

    def record_creation_inquiry(
        self,
        *,
        order_id: PaymentOrderId,
        now: datetime,
        next_inquiry_at: datetime | None,
    ) -> None:
        """Record one read-only recovery inquiry and its bounded backoff."""

    def attach_checkout(
        self,
        *,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        external_checkout_reference: str,
        checkout_url: str,
        now: datetime,
    ) -> None:
        """Durably persist the provider checkout identity/URL before ``create_payment`` returns.

        A crash after a successful provider response must never lose the external reference;
        recovery re-displays the same checkout instead of creating another invoice.
        """

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
