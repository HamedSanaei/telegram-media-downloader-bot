"""Durable bounded pending-order reconciliation (T025).

One rule separates this service from settlement: a provider callback/redirect/button press is a
TRIGGER, never payment proof. Every confirmation path runs the same core — resolve the order to
its registered provider adapter, perform a read-only ``query_payment``, and settle ONLY a
``VerifiedPaymentResult`` whose status is paid through ``BillingService.handle_verified_result``
(the atomic SQLite transaction with the exactly-once provider-transaction claim).

Pending orders survive callback loss, process restart, and provider outages: the worker cron
rescans the same durable rows with bounded attempts/backoff and never creates a second invoice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from telegram_media_bot.application.ports.payments import PaymentGateway, PaymentRepository
from telegram_media_bot.application.services.billing import BillingService
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.domain.errors import (
    PaymentBackendError,
    PaymentOrderExpiredError,
    PaymentOrderNotFoundError,
    PaymentTransactionReplayError,
)
from telegram_media_bot.domain.payments import (
    PaymentCreationReservation,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import Subscription

#: Bounded constant backoff between automatic worker inquiries (never hammer providers).
_INQUIRY_BACKOFF = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    scanned: int = 0
    confirmed: int = 0
    terminal: int = 0
    pending: int = 0
    skipped: int = 0


class PaymentReconciliationService:
    """Query-before-settle core shared by the worker cron, callbacks, and manual checks."""

    def __init__(
        self,
        *,
        billing: BillingService,
        payments: PaymentRepository,
        gateways: dict[PaymentProviderId, PaymentGateway],
        max_query_attempts: int,
        clock: Callable[[], datetime] | None = None,
        payment_logger: PaymentAuditLogger | None = None,
    ) -> None:
        self._billing = billing
        self._payments = payments
        self._gateways = dict(gateways)
        self._max_query_attempts = max(1, max_query_attempts)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._payment_logger = payment_logger

    # -- worker batch ---------------------------------------------------------

    def reconcile_batch(self, *, batch_size: int | None = None) -> ReconcileReport:
        """Scan durable pending orders and settle verified paid results (bounded, read-only)."""
        now = self._clock()
        report = ReconcileReport()
        orders = self._payments.list_pending_orders(before=now)
        selected = orders[:batch_size] if batch_size is not None else orders
        for order in selected:
            outcome = self.check_order(order.order_id)
            if outcome is None:
                continue
            if outcome is CheckOutcome.PAID:
                report = replace(report, scanned=report.scanned + 1, confirmed=report.confirmed + 1)
            elif outcome in {CheckOutcome.EXPIRED, CheckOutcome.CANCELLED, CheckOutcome.FAILED}:
                report = replace(report, scanned=report.scanned + 1, terminal=report.terminal + 1)
            elif outcome is CheckOutcome.PENDING:
                report = replace(report, scanned=report.scanned + 1, pending=report.pending + 1)
            else:
                report = replace(report, scanned=report.scanned + 1, skipped=report.skipped + 1)
        return report

    # -- single order (worker / callback / manual check share this path) -------

    def check_order(self, order_id: PaymentOrderId) -> CheckOutcome | None:
        """One bounded check of one order; returns a :class:`CheckOutcome` or ``None`` when the
        order must not be probed (unknown, un-routed, budget exhausted, definitive failure)."""
        order = self._payments.get_order(order_id)
        if order is None or order.provider_id is None or not order.external_checkout_reference:
            if order is not None and order.status is PaymentStatus.CREATED:
                self._expire_if_past_deadline(order)
            return None
        reservation = self._payments.get_creation_reservation(order_id)
        if reservation is None:
            return None
        if not reservation.state.inquiry_allowed:
            return CheckOutcome.REJECTED
        result = self._query_bounded(order, reservation)
        if result is None or result.status is PaymentStatus.PENDING:
            if order.expires_at < self._clock():
                self._safe_call(self._billing.expire_order, order_id)
                return CheckOutcome.EXPIRED
            return CheckOutcome.PENDING
        if result.status is not PaymentStatus.PAID:
            return self._settle_terminal(order_id, result.status)
        try:
            subscription = self._billing.handle_verified_result(result)
        except PaymentOrderNotFoundError:
            return CheckOutcome.SKIPPED
        except PaymentOrderExpiredError:
            return CheckOutcome.SKIPPED
        except PaymentTransactionReplayError:
            # Duplicate wake-up/reconciliation of an already-processed order: benign, done.
            return CheckOutcome.SKIPPED
        if self._payment_logger is not None:
            self._log_confirmed(order, result, subscription)
        return CheckOutcome.PAID

    def manual_check(self, order_id: PaymentOrderId) -> CheckOutcome:
        """Explicit user/admin check; same settle path, no worker budget."""
        outcome = self.check_order(order_id)
        return outcome if outcome is not None else CheckOutcome.SKIPPED

    # -- internal helpers -------------------------------------------------------

    def _query_bounded(
        self, order: PaymentOrder, reservation: PaymentCreationReservation
    ) -> VerifiedPaymentResult | None:
        if order.provider_id is None:
            return None
        gateway = self._gateways.get(order.provider_id)
        if gateway is None:
            return None
        now = self._clock()
        if reservation.inquiry_attempts >= self._max_query_attempts:
            return None
        if order.external_checkout_reference is None:
            return None
        try:
            result = gateway.query_payment(
                order, ProviderTransactionReference(order.external_checkout_reference)
            )
        except Exception:
            self._payments.record_creation_inquiry(
                order_id=order.order_id,
                now=now,
                next_inquiry_at=now + _INQUIRY_BACKOFF,
            )
            return None
        self._payments.record_creation_inquiry(
            order_id=order.order_id,
            now=now,
            next_inquiry_at=now + _INQUIRY_BACKOFF,
        )
        return result

    def _log_confirmed(
        self, order: PaymentOrder, result: VerifiedPaymentResult, subscription: Subscription
    ) -> None:
        """Emit the single safe purchase event after the settlement committed.

        Failure isolation contract: if logging fails for any reason the payment remains settled and
        VIP stays active; the deterministic idempotency key prevents any duplicate from a replay.
        """
        if self._payment_logger is None:
            return
        try:
            self._payment_logger.log_purchase_confirmed(
                order_id=str(order.order_id),
                user_id=order.user_id,
                provider_id=str(result.provider_id),
                plan_id=str(order.plan_id),
                plan_name=str(order.plan_id),
                duration_months=order.duration_months,
                amount_toman=order.amount_minor,
                currency=order.currency,
                authorized_until=subscription.authorized_until or order.expires_at,
                confirmed_at=self._clock(),
            )
        except Exception:
            return

    def _settle_terminal(self, order_id: PaymentOrderId, status: PaymentStatus) -> CheckOutcome:
        if status is PaymentStatus.EXPIRED:
            self._safe_call(self._billing.expire_order, order_id)
            return CheckOutcome.EXPIRED
        if status is PaymentStatus.CANCELLED:
            self._safe_call(self._billing.cancel_order, order_id)
            return CheckOutcome.CANCELLED
        if status is PaymentStatus.FAILED:
            self._safe_call(self._billing.fail_order, order_id, failure_code="provider_failed")
            return CheckOutcome.FAILED
        return CheckOutcome.SKIPPED

    def _expire_if_past_deadline(self, order: PaymentOrder) -> None:
        if order.expires_at < self._clock():
            self._safe_call(self._billing.expire_order, order.order_id)

    def _safe_call(
        self, callable_: Callable[..., object], *args: object, **kwargs: object
    ) -> object | None:
        try:
            return callable_(*args, **kwargs)
        except PaymentBackendError, PaymentOrderNotFoundError:
            return None


class CheckOutcome(StrEnum):
    """Stable, non-secret outcomes of one bounded check."""

    PAID = "paid"
    PENDING = "pending"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED = "skipped"


__all__ = ["CheckOutcome", "PaymentReconciliationService", "ReconcileReport"]
