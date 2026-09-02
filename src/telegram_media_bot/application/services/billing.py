"""Provider-neutral billing orchestration (T015).

``BillingService`` owns the economic/payment facts. It creates durable orders from an operator-owned
plan snapshot, opens external checkouts through a gateway port, accepts ONLY a verified project
result (never a raw provider callback), and delegates the atomic confirmation/refund to the payment
repository. It never reads ``UserProfile.is_premium`` and never contains provider ``if/elif``
branches.

The entitlement grant and subscription recomputation intentionally live under T014's authority: the
payment repository calls T014 connection-scoped helpers inside its single transaction, so billing
does not duplicate access-grant semantics.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol

from telegram_media_bot.application.ports.payments import PaymentGateway, PaymentRepository
from telegram_media_bot.domain.errors import (
    CheckoutAlreadyStartedError,
    CheckoutUnavailableError,
    PaymentAlreadyRefundedError,
    PaymentBackendError,
    PaymentCreationReservedError,
    PaymentOrderExpiredError,
    PaymentOrderNotFoundError,
    PersistenceError,
    ProviderNotRegisteredError,
)
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentAttempt,
    PaymentAttemptId,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    VerifiedPaymentResult,
    payment_status_transition,
)
from telegram_media_bot.domain.subscriptions import (
    EntitlementGrant,
    GrantId,
    Subscription,
    SubscriptionPlan,
)


class PaymentClock(Protocol):
    def now(self) -> datetime:
        """Return the current UTC instant (injectable for deterministic tests)."""


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class BillingService:
    """Deterministic, clock-injected billing operations.

    ``create_order`` accepts an operator-owned ``SubscriptionPlan`` and snapshots its commercial
    facts at creation time; a later plan price change never alters an already-created order. The
    expiry is supplied by the caller (never inferred from a hardcoded commercial duration), so
    callers control expiration deterministically.
    """

    def __init__(
        self,
        *,
        payments: PaymentRepository,
        clock: PaymentClock,
        gateways: dict[PaymentProviderId, PaymentGateway] | None = None,
    ) -> None:
        self._payments = payments
        self._clock = clock
        self._gateways = dict(gateways or {})

    def register_gateway(self, gateway: PaymentGateway) -> None:
        """Compose a gateway adapter for a bounded provider identity."""
        self._gateways[gateway.provider_id] = gateway

    def _gateway(self, provider_id: PaymentProviderId) -> PaymentGateway:
        try:
            return self._gateways[provider_id]
        except KeyError as exc:
            raise ProviderNotRegisteredError(
                f"No gateway registered for provider {provider_id}"
            ) from exc

    def _now(self) -> datetime:
        return self._clock.now()

    # -- order lifecycle ------------------------------------------------------

    def create_order(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        *,
        provider_id: PaymentProviderId | None = None,
        expires_at: datetime,
        order_id: PaymentOrderId | None = None,
    ) -> PaymentOrder:
        """Create a durable order snapshotting the plan's commercial facts (never pricing-mutable)."""
        now = self._now()
        order = PaymentOrder(
            order_id=order_id or PaymentOrderId(f"order-{uuid.uuid4().hex}"),
            user_id=user_id,
            plan_id=plan.plan_id,
            duration_months=plan.duration_months,
            capabilities=plan.capabilities,
            amount_minor=plan.price_minor,
            currency=plan.currency,
            created_at=now,
            expires_at=expires_at,
            status=PaymentStatus.CREATED,
            provider_id=provider_id,
        )
        try:
            self._payments.save_order(order)
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        return order

    def start_checkout(
        self, order_id: PaymentOrderId, *, provider_id: PaymentProviderId
    ) -> CheckoutResult:
        """Route a created order to a provider and persist a PENDING attempt (redirect only).

        Economic-safety contract (T024): the adapter durably reserves the single provider create
        mutation before the POST (``begin_creation_attempt``) and persists the external checkout
        identity before returning (``attach_checkout``). If the order already carries an external
        checkout, recovery re-displays the SAME checkout; another provider invoice is never
        created.
        """
        order = self._load_order(order_id)
        if order.status is PaymentStatus.PENDING and order.external_checkout_reference:
            raise CheckoutAlreadyStartedError(
                "This order already has an external checkout; recover from /vip"
            )
        payment_status_transition(order.status, PaymentStatus.PENDING)
        gateway = self._gateway(provider_id)
        if not gateway.available_for_new_checkout():
            raise CheckoutUnavailableError("Provider cannot accept new checkouts")
        attempt_id = PaymentAttemptId(f"attempt-{uuid.uuid4().hex}")
        now = self._now()
        try:
            self._payments.save_attempt(
                PaymentAttempt(
                    attempt_id=attempt_id,
                    order_id=order.order_id,
                    provider_id=provider_id,
                    status=PaymentStatus.PENDING,
                    created_at=now,
                    updated_at=now,
                )
            )
            checkout = gateway.create_payment(order)
        except PaymentBackendError:
            raise
        except PaymentCreationReservedError as exc:
            raise CheckoutAlreadyStartedError(
                "This order already has an external checkout; recover from /vip"
            ) from exc
        except Exception as exc:
            raise CheckoutUnavailableError("Provider could not create a checkout") from exc

        # Persist the PENDING order transition after a successful external checkout. The adapter
        # already attached the external reference; reload to carry it forward.
        attached = self._load_order(order_id)
        pending = _with_status(attached, PaymentStatus.PENDING, provider_id)
        try:
            self._payments.save_order(pending)
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        return checkout

    def cancel_order(self, order_id: PaymentOrderId) -> PaymentOrder:
        """Cancel an un-paid order; a later confirmation can never activate it."""
        order = self._load_order(order_id)
        payment_status_transition(order.status, PaymentStatus.CANCELLED)
        cancelled = self._update_order_status(order_id, PaymentStatus.CANCELLED, order.provider_id)
        assert cancelled is not None
        return cancelled

    def expire_order(self, order_id: PaymentOrderId) -> PaymentOrder | None:
        """Expire an order past its deterministic UTC expiry; returns None when unchanged."""
        order = self._load_order(order_id)
        now = self._now()
        if now <= order.expires_at:
            return None
        try:
            payment_status_transition(order.status, PaymentStatus.EXPIRED)
        except Exception:
            return None
        return self._update_order_status(order_id, PaymentStatus.EXPIRED, order.provider_id)

    def fail_order(
        self, order_id: PaymentOrderId, *, failure_code: str | None = None
    ) -> PaymentOrder:
        """Terminally fail an un-paid order (provider rejection). Never revisits the provider."""
        order = self._load_order(order_id)
        payment_status_transition(order.status, PaymentStatus.FAILED)
        failed = self._update_order_status(order_id, PaymentStatus.FAILED, order.provider_id)
        assert failed is not None
        # Keep the failure classification on the durable attempt row when one exists.
        if failure_code is not None:
            self._payments.save_attempt(
                PaymentAttempt(
                    attempt_id=PaymentAttemptId(f"attempt-{order_id!s}-failed"),
                    order_id=order.order_id,
                    provider_id=order.provider_id,
                    status=PaymentStatus.FAILED,
                    created_at=self._now(),
                    updated_at=self._now(),
                    failure_code=failure_code[:64],
                )
            )
        return failed

    # -- verified result handling ----------------------------------------------

    def handle_verified_result(self, result: VerifiedPaymentResult) -> Subscription:
        """Accept ONLY a verified project result and confirm the order atomically (exactly-one grant)."""
        if result.status is not PaymentStatus.PAID:
            raise PaymentOrderNotFoundError("Verified result is not a payment confirmation")
        order = self._load_order_by_reference(result.order_reference)
        if order.expires_at < self._now():
            raise PaymentOrderExpiredError("Payment order has expired")
        confirmed_at = self._now()
        grant = _grant_for_result(order, result, confirmed_at)
        try:
            return self._payments.confirm_order_atomic(
                grant=grant,
                order_id=order.order_id,
                provider_id=result.provider_id,
                provider_transaction_reference=result.provider_transaction_reference,
                expected_amount_minor=result.amount_minor,
                expected_currency=result.currency,
                expected_order_reference=str(order.order_id),
                paid_at=confirmed_at,
                now=confirmed_at,
            )
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        except PaymentOrderExpiredError:
            raise
        except PaymentOrderNotFoundError:
            raise

    # -- refund / reversal ------------------------------------------------------

    def refund_verified_payment(
        self,
        result: VerifiedPaymentResult,
        *,
        reason: str,
    ) -> Subscription | None:
        """Reverse the grant for an already-paid order and recompute entitlement atomically."""
        order = self._load_order_by_reference(result.order_reference)
        if order.status is PaymentStatus.REFUNDED:
            raise PaymentAlreadyRefundedError("Payment is already refunded")
        if order.status is not PaymentStatus.PAID:
            raise PaymentOrderNotFoundError("Only a paid order can be refunded")
        now = self._now()
        try:
            return self._payments.reverse_order_atomic(
                order_id=order.order_id,
                provider_id=result.provider_id,
                provider_transaction_reference=result.provider_transaction_reference,
                reason=reason,
                reversed_at=now,
                now=now,
            )
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc

    # -- reconciliation foundation -----------------------------------------------

    def list_pending_orders(self, *, before: datetime | None = None) -> tuple[PaymentOrder, ...]:
        """Durable pending/created order query for future reconciliation (no scheduled poller here)."""
        try:
            return self._payments.list_pending_orders(before=before or self._now())
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc

    def count_orders_by_status(self) -> dict[str, int]:
        try:
            return self._payments.count_orders_by_status()
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc

    # -- internal helpers --------------------------------------------------------

    def _load_order(self, order_id: PaymentOrderId) -> PaymentOrder:
        try:
            order = self._payments.get_order(order_id)
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        if order is None:
            raise PaymentOrderNotFoundError("Payment order does not exist")
        return order

    def _load_order_by_reference(self, order_reference: str) -> PaymentOrder:
        try:
            order = self._payments.get_order(PaymentOrderId(order_reference))
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        if order is None:
            raise PaymentOrderNotFoundError("Payment order does not exist")
        return order

    def _update_order_status(
        self,
        order_id: PaymentOrderId,
        status: PaymentStatus,
        provider_id: PaymentProviderId | None,
    ) -> PaymentOrder:
        order = self._load_order(order_id)
        updated = _with_status(order, status, provider_id)
        try:
            self._payments.save_order(updated)
        except PersistenceError as exc:
            raise PaymentBackendError("Payment backend is unavailable") from exc
        return updated


def _with_status(
    order: PaymentOrder,
    status: PaymentStatus,
    provider_id: PaymentProviderId | None,
) -> PaymentOrder:
    return PaymentOrder(
        order_id=order.order_id,
        user_id=order.user_id,
        plan_id=order.plan_id,
        duration_months=order.duration_months,
        capabilities=order.capabilities,
        amount_minor=order.amount_minor,
        currency=order.currency,
        created_at=order.created_at,
        expires_at=order.expires_at,
        status=status,
        provider_id=provider_id,
        external_checkout_reference=order.external_checkout_reference,
        checkout_url=order.checkout_url,
    )


def _grant_for_result(
    order: PaymentOrder,
    result: VerifiedPaymentResult,
    confirmed_at: datetime,
) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(f"grant-{result.provider_id!s}-{result.provider_transaction_reference!s}"),
        user_id=order.user_id,
        plan_id=order.plan_id,
        duration_months=order.duration_months,
        confirmed_at=confirmed_at,
        source_type=str(result.provider_id),
        source_reference=str(result.provider_transaction_reference),
        created_at=confirmed_at,
    )
