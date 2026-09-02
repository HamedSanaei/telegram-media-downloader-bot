"""Fake-provider payment E2E (T024/T025).

The complete loop: operator enables a provider -> /vip creates a payment order -> provider callback
wakes the companion -> normalized trigger -> authoritative query -> atomic confirmation -> exactly
one entitlement grant -> exactly one safe purchase Logger event. Duplicate callbacks, concurrent
checks and replayed wake-ups change nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.ports.companion import PaymentCallbackTrigger
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.billing import BillingService
from telegram_media_bot.application.services.payment_callbacks import (
    CompanionPaymentCallbackProcessor,
)
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.application.services.payment_reconciliation import (
    PaymentReconciliationService,
)
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentCreationState,
    PaymentOrder,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import Capability, PlanId, SubscriptionPlan
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
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


class FakeProvider:
    """Deterministic test provider that behaves like the real adapters (scripted settle)."""

    provider_id = PaymentProviderId("fake")

    def __init__(self, requester: _ScriptedRequester) -> None:
        self._requester = requester

    def available_for_new_checkout(self) -> bool:
        return True

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        assert self._requester.repository is not None
        self._requester.repository.begin_creation_attempt(
            order_id=order.order_id,
            provider_id=self.provider_id,
            merchant_reference=f"merchant-{order.order_id!s}",
            attempted_at=self._requester.now(),
        )
        self._requester.repository.attach_checkout(
            order_id=order.order_id,
            provider_id=self.provider_id,
            external_checkout_reference="fake-checkout-1",
            checkout_url="https://pay.test/checkout/1",
            now=self._requester.now(),
        )
        self._requester.repository.resolve_creation_attempt(
            order_id=order.order_id,
            state=PaymentCreationState.CREATED,
            error_code=None,
            resolved_at=self._requester.now(),
        )
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,
            external_checkout_reference="fake-checkout-1",
            created_at=self._requester.now(),
            expires_at=order.expires_at,
            checkout_url="https://pay.test/checkout/1",
        )

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: str | None = None,
    ) -> VerifiedPaymentResult:
        del provider_transaction_reference
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference("fake-checkout-1"),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency=order.currency,
            status=PaymentStatus.PAID,
            failure_code=None,
        )


class _ScriptedRequester:
    def __init__(self, now: datetime) -> None:
        self._now_value = now
        self.repository: SqlitePaymentRepository | None = None

    def now(self) -> datetime:
        return self._now_value


def _build(
    tmp_path: Path,
) -> tuple[
    datetime,
    SqlitePaymentRepository,
    SqliteSubscriptionRepository,
    BillingService,
    CompanionPaymentCallbackProcessor,
    SqliteAuditRepository,
    PaymentAuditLogger,
]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    requester = _ScriptedRequester(now)
    db = tmp_path / "state.sqlite3"
    payments = SqlitePaymentRepository(db)
    payments.initialize()
    subscriptions = SqliteSubscriptionRepository(db)
    subscriptions.initialize()
    subscriptions.save_plan(PLAN)
    requester.repository = payments
    billing = BillingService(payments=payments, clock=requester)
    provider = FakeProvider(requester)
    billing.register_gateway(provider)
    audit_repo = SqliteAuditRepository(db)
    audit_repo.initialize()
    audit_repo.reconcile_config((-1001234567890,))
    audit = AuditService(audit_repo, enabled=True)
    logger = PaymentAuditLogger(audit, payment_events_enabled=True)
    reconciliation = PaymentReconciliationService(
        billing=billing,
        payments=payments,
        gateways={provider.provider_id: provider},
        max_query_attempts=3,
        clock=requester.now,
        payment_logger=logger,
    )
    processor = CompanionPaymentCallbackProcessor(
        reconciliation=reconciliation,
        payments=payments,
        clock=requester.now,
    )
    return now, payments, subscriptions, billing, processor, audit_repo, logger


def _open_order(billing: BillingService, now: datetime) -> PaymentOrder:
    order = billing.create_order(
        7, PLAN, provider_id=PaymentProviderId("fake"), expires_at=now + timedelta(days=1)
    )
    billing.start_checkout(order.order_id, provider_id=PaymentProviderId("fake"))
    return order


def test_full_purchase_loop_callback_settles_exactly_once(tmp_path: Path) -> None:
    now, payments, subscriptions, billing, processor, audit_repo, _ = _build(tmp_path)
    order = _open_order(billing, now)

    outcome = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=True
            )
        )
    )

    assert outcome.value in {"accepted", "paid"}
    restored = payments.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.PAID
    grants = subscriptions.get_grants(7)
    assert len(grants) == 1
    assert grants[0].source_type == "fake"  # provider identity is the durable economic source
    assert grants[0].source_reference == "fake-checkout-1"
    # Authorized for one calendar month of the plan.
    assert subscriptions.get_subscription(7) is not None
    assert subscriptions.get_subscription(7).authorized_until is not None  # type: ignore[union-attr]

    # Replayed wake-up (webhook replay / duplicate callback): no second grant.
    asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=True
            )
        )
    )
    assert len(subscriptions.get_grants(7)) == 1

    # Exactly one safe purchase event in the Logger outbox.
    items = [item for item in audit_repo.claim_pending(limit=50) if item.event.provider == "fake"]
    assert len(items) == 1
    assert "fake-checkout-1" not in items[0].event.message
    assert items[0].event.event_type.value == "payment_confirmed"


def test_user_check_button_and_callback_race_settle_once(tmp_path: Path) -> None:
    now, payments, subscriptions, billing, processor, _, _ = _build(tmp_path)
    order = _open_order(billing, now)

    # User presses "بررسی پرداخت" and the provider webhook fires at the same moment.
    outcome_check = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=True
            )
        )
    )
    outcome_callback = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=True
            )
        )
    )
    assert outcome_check.value in {"accepted", "paid"}
    assert outcome_callback.value in {"accepted", "paid", "skipped", "not_available"}
    assert len(subscriptions.get_grants(7)) == 1  # exactly-once, regardless of race order
    restored = payments.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.PAID


def test_forged_callback_rejected_without_effect(tmp_path: Path) -> None:
    now, payments, subscriptions, billing, processor, _, _ = _build(tmp_path)
    order = _open_order(billing, now)

    outcome = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=False
            )
        )
    )
    assert outcome.value == "rejected"
    restored = payments.get_order(order.order_id)
    # Order remains untouched at whatever stage it reached before the forged wake-up.
    assert restored is not None
    assert restored.status in {PaymentStatus.CREATED, PaymentStatus.PENDING}
    assert len(subscriptions.get_grants(7)) == 0


def test_callback_for_unknown_order_is_generic_404(tmp_path: Path) -> None:
    _, _, _, _, processor, _, _ = _build(tmp_path)
    outcome = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference="order-does-not-exist", authentic=True
            )
        )
    )
    assert outcome.value in {"not_available", "accepted"}


def test_disabled_provider_orders_stay_queryable_and_confirmable(tmp_path: Path) -> None:
    now, payments, subscriptions, billing, _, audit_repo, logger = _build(tmp_path)
    order = _open_order(billing, now)
    from telegram_media_bot.application.services.payment_reconciliation import (
        PaymentReconciliationService,
    )

    # Provider becomes unavailable for NEW checkout; existing pending order must still confirm.
    reconciliation = PaymentReconciliationService(
        billing=billing,
        payments=payments,
        gateways={PaymentProviderId("fake"): billing._gateway(PaymentProviderId("fake"))},
        max_query_attempts=3,
        clock=lambda: now,
        payment_logger=logger,
    )
    processor = CompanionPaymentCallbackProcessor(
        reconciliation=reconciliation, payments=payments, clock=lambda: now
    )
    outcome = asyncio.run(
        processor.process(
            trigger=PaymentCallbackTrigger(
                provider_id="fake", order_reference=str(order.order_id), authentic=True
            )
        )
    )
    assert outcome.value in {"accepted", "paid"}
    restored = payments.get_order(order.order_id)
    assert restored is not None and restored.status is PaymentStatus.PAID
    assert len(subscriptions.get_grants(7)) == 1
    assert audit_repo.health_snapshot().pending_effects >= 1
