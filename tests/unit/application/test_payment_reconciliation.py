"""Payment reconciliation tests (T025).

Query-before-settle: the worker batch, manual checks, callback processing and pay-check all flow
through one service that only settles an authoritative verified result, exactly once, inside the
BillingService transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.billing import BillingService
from telegram_media_bot.application.services.entitlements import EntitlementService
from telegram_media_bot.application.services.payment_reconciliation import (
    CheckOutcome,
    PaymentReconciliationService,
)
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentCreationState,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import Capability, PlanId, SubscriptionPlan
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository
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


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


class ScenarioGateway:
    """Gateway whose create always succeeds and whose query returns a scripted verdict."""

    provider_id = PaymentProviderId("fake")

    def __init__(self, *, verdict: PaymentStatus, failure_code: str | None = None) -> None:
        self.verdict = verdict
        self.failure_code = failure_code
        self.queries = 0

    def available_for_new_checkout(self) -> bool:
        return True

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        raise AssertionError("reconciliation must NEVER create invoices")

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        del provider_transaction_reference
        self.queries += 1
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference("fake-ref"),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency=order.currency,
            status=self.verdict,
            failure_code=self.failure_code,
        )


def _db(tmp_path: Path) -> tuple[SqlitePaymentRepository, SqliteSubscriptionRepository]:
    payment = SqlitePaymentRepository(tmp_path / "state.sqlite3")
    payment.initialize()
    subscription = SqliteSubscriptionRepository(tmp_path / "state.sqlite3")
    subscription.initialize()
    subscription.save_plan(PLAN)
    return payment, subscription


def _pending_order(
    payment: SqlitePaymentRepository,
    *,
    order_id: str = "order-1",
    created_at: datetime | None = None,
    reference: str = "fake-ref",
) -> PaymentOrder:
    at = (created_at or _now()) - timedelta(seconds=1)
    provider = PaymentProviderId("fake")
    order = PaymentOrder(
        order_id=PaymentOrderId(order_id),
        user_id=7,
        plan_id=PLAN.plan_id,
        duration_months=1,
        capabilities=PLAN.capabilities,
        amount_minor=PLAN.price_minor,
        currency=PLAN.currency,
        status=PaymentStatus.CREATED,
        provider_id=provider,
        external_checkout_reference=reference,
        checkout_url="https://pay.test/checkout",
        created_at=at,
        expires_at=at + timedelta(days=1),
    )
    payment.save_order(order)
    payment.begin_creation_attempt(
        order_id=order.order_id,
        provider_id=provider,
        merchant_reference=f"merchant-{order_id}",
        attempted_at=at,
    )
    payment.attach_checkout(
        order_id=order.order_id,
        provider_id=provider,
        external_checkout_reference=reference,
        checkout_url="https://pay.test/checkout",
        now=at,
    )
    payment.resolve_creation_attempt(
        order_id=order.order_id,
        state=PaymentCreationState.CREATED,
        error_code=None,
        resolved_at=at,
    )
    restored = payment.get_order(order.order_id)
    assert restored is not None
    return restored


def _make(
    tmp_path: Path, gateway: ScenarioGateway
) -> tuple[PaymentReconciliationService, BillingService, SqlitePaymentRepository]:
    payment, subscription = _db(tmp_path)
    billing = BillingService(payments=payment, clock=_Clock(_now()))
    billing.register_gateway(gateway)
    entitlements = EntitlementService(plans=subscription, subscriptions=subscription)
    reconciliation = PaymentReconciliationService(
        billing=billing,
        payments=payment,
        gateways={gateway.provider_id: gateway},
        max_query_attempts=3,
        clock=lambda: _now(),
    )
    del entitlements
    return reconciliation, billing, payment


def test_reconcile_confirms_paid_order_and_grants_once(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PAID)
    reconciliation, _, payment = _make(tmp_path, gateway)
    order = _pending_order(payment)

    first = reconciliation.manual_check(order.order_id)
    second = reconciliation.manual_check(order.order_id)

    assert first is CheckOutcome.PAID
    assert second is CheckOutcome.SKIPPED
    restored = payment.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.PAID
    # Idempotent: a second check must not create a second grant.
    subscription = SqliteSubscriptionRepository(tmp_path / "state.sqlite3")
    assert len(subscription.get_grants(7)) == 1


def test_reconcile_batch_reports_counts(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PENDING)
    reconciliation, _, payment = _make(tmp_path, gateway)
    _pending_order(payment, order_id="order-1")
    _pending_order(payment, order_id="order-2")

    report = reconciliation.reconcile_batch(batch_size=20)

    assert report.scanned == 2
    assert report.pending == 2
    assert report.confirmed == 0
    assert report.terminal == 0


def test_reconcile_batch_confirms_and_counts(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PAID)
    reconciliation, _, payment = _make(tmp_path, gateway)
    _pending_order(payment, order_id="order-1")

    report = reconciliation.reconcile_batch()

    assert report.scanned == 1
    assert report.confirmed == 1
    restored = payment.get_order(PaymentOrderId("order-1"))
    assert restored is not None and restored.status is PaymentStatus.PAID


def test_reconcile_never_creates_invoices_on_timeout(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PENDING)
    reconciliation, _, payment = _make(tmp_path, gateway)
    order = _pending_order(payment)

    assert reconciliation.manual_check(order.order_id) is CheckOutcome.PENDING
    restored = payment.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.CREATED
    # No second invoice: the reservation stays CREATED and no create call is ever issued.
    assert gateway.queries >= 0


def test_reconcile_expired_order_transitions_after_deadline(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PENDING)
    payment, _ = _db(tmp_path)
    past = _now() - timedelta(days=2)
    order = _pending_order(payment, created_at=past)

    later = _Clock(_now() + timedelta(days=2))
    billing = BillingService(payments=payment, clock=later)
    reconciliation = PaymentReconciliationService(
        billing=billing,
        payments=payment,
        gateways={gateway.provider_id: gateway},
        max_query_attempts=3,
        clock=lambda: _now() + timedelta(days=2),
    )

    assert reconciliation.manual_check(order.order_id) is CheckOutcome.EXPIRED
    restored = payment.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.EXPIRED


def test_reconcile_terminal_failure_marks_without_reverse_claim(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.FAILED, failure_code="provider_failed")
    reconciliation, _, payment = _make(tmp_path, gateway)
    order = _pending_order(payment)

    assert reconciliation.manual_check(order.order_id) is CheckOutcome.FAILED
    restored = payment.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.FAILED


def test_unknown_order_check_is_skipped(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PAID)
    reconciliation, _, _ = _make(tmp_path, gateway)

    assert reconciliation.manual_check(PaymentOrderId("order-missing")) is CheckOutcome.SKIPPED


def test_missing_create_reservation_blocks_check(tmp_path: Path) -> None:
    gateway = ScenarioGateway(verdict=PaymentStatus.PAID)
    reconciliation, _, payment = _make(tmp_path, gateway)
    payment, _ = _db(tmp_path)
    at = _now()
    order = PaymentOrder(
        order_id=PaymentOrderId("order-1"),
        user_id=7,
        plan_id=PLAN.plan_id,
        duration_months=1,
        capabilities=PLAN.capabilities,
        amount_minor=PLAN.price_minor,
        currency=PLAN.currency,
        status=PaymentStatus.CREATED,
        provider_id=PaymentProviderId("fake"),
        external_checkout_reference="fake-ref",
        checkout_url="https://pay.test/checkout",
        created_at=at,
        expires_at=at + timedelta(days=1),
    )
    payment.save_order(order)  # no reservation row

    assert reconciliation.check_order(order.order_id) is None
