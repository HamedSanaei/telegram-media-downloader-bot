"""T015 BillingService tests: order snapshot, verified-result boundary, refund, cancellation.

Uses a real (WAL) SQLite payment store so the atomic confirmation/refund transaction is exercised,
together with the deterministic test-only fake gateway from ``tests/helpers/payment_fakes``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_media_bot.application.ports.payments import PaymentRepository
from telegram_media_bot.application.services.billing import BillingService
from telegram_media_bot.domain.errors import (
    CheckoutUnavailableError,
    InvalidPaymentTransitionError,
    PaymentAlreadyRefundedError,
    PaymentOrderExpiredError,
    PaymentOrderNotFoundError,
    PaymentTransactionReplayError,
    ProviderNotRegisteredError,
)
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    PlanId,
    Subscription,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)

VIP = Capability.INSTAGRAM_PRIVATE_MEDIA
FAKE = PaymentProviderId("fake")


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class FakeGateway:
    """Deterministic test-only gateway adapter (never used in production business logic)."""

    def __init__(self, *, provider_id: PaymentProviderId, scenario: str = "success") -> None:
        self.provider_id = provider_id
        self.scenario = scenario

    def create_payment(self, order: object, **_: object) -> CheckoutResult:
        if self.scenario == "checkout_failure":
            raise RuntimeError("provider unavailable")
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,  # type: ignore[attr-defined]
            external_checkout_reference=f"fake-txn-{order.order_id}",  # type: ignore[attr-defined]
            created_at=order.created_at,  # type: ignore[attr-defined]
            expires_at=order.expires_at,  # type: ignore[attr-defined]
        )

    def verify_callback(
        self, order: PaymentOrder, provider_payload: object
    ) -> VerifiedPaymentResult:
        # A real adapter would strip everything except a normalized project result; the fake never
        # leaks the raw payload back to the service.
        assert provider_payload is not None  # adversarial input never reaches the service
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference(
                f"fake-txn-{order.order_id!s}"
            ),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency=order.currency,
            status=PaymentStatus.PAID if self.scenario == "success" else PaymentStatus.PENDING,
        )

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        return self.verify_callback(order, {})


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _raw_callback_sample() -> dict[str, str]:
    return {"signature": "secret-signature", "order_id": "order-abcd", "raw_amount": "12.34"}


def _deref(repo: PaymentRepository, order_id: PaymentOrderId) -> PaymentOrder:
    restored = repo.get_order(order_id)
    assert restored is not None
    return restored


def _plan(months: int = 1, price: int = 4900) -> SubscriptionPlan:
    return SubscriptionPlan(
        plan_id=PlanId("vip-1"),
        name="VIP",
        duration_months=months,
        price_minor=price,
        currency="USD",
        enabled=True,
        capabilities=frozenset({VIP}),
    )


def _make_service(
    tmp_path: Path,
    *,
    now: datetime | None = None,
) -> tuple[BillingService, SqlitePaymentRepository, _Clock, SubscriptionPlan]:
    clock = _Clock(now or _utc(2026, 1, 1))
    payment_repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    payment_repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    plan = _plan()
    sub_store.save_plan(plan)
    service = BillingService(payments=payment_repo, clock=clock)
    service.register_gateway(FakeGateway(provider_id=FAKE))
    return service, payment_repo, clock, plan


# --------------------------------------------------------------------------- #
# Order lifecycle + snapshot
# --------------------------------------------------------------------------- #


def test_create_order_snapshots_commercial_facts(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    assert order.amount_minor == 4900
    assert order.currency == "USD"
    assert order.duration_months == 1
    assert order.capabilities == frozenset({VIP})
    assert order.status is PaymentStatus.CREATED
    restored = repo.get_order(order.order_id)
    assert restored is not None
    assert restored == order


def test_order_snapshot_not_affected_by_later_price_change(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    # Operator reprices the plan for FUTURE orders only.
    plan2 = SubscriptionPlan(
        plan_id=PlanId("vip-1"),
        name="VIP",
        duration_months=1,
        price_minor=9900,
        currency="USD",
        enabled=True,
        capabilities=frozenset({VIP}),
    )
    from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
        SqliteSubscriptionRepository,
    )

    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.save_plan(plan2)
    # The existing order still holds its snapshotted 100-credit amount.
    restored = repo.get_order(order.order_id)
    assert restored is not None
    assert restored.amount_minor == 4900
    assert restored.currency == "USD"


def test_create_order_without_provider_is_redirect_free_and_pending(tmp_path: Path) -> None:
    # Provider routing is optional at order creation; a redirect alone never confirms.
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, expires_at=_utc(2026, 2, 1))
    assert order.provider_id is None
    checkout = service.start_checkout(order.order_id, provider_id=FAKE)
    assert checkout.redirect_only is True  # never payment proof
    restored = repo.get_order(order.order_id)
    assert restored is not None
    assert restored.status is PaymentStatus.PENDING
    assert restored.provider_id == FAKE


def test_start_checkout_records_pending_attempt(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    checkout = service.start_checkout(order.order_id, provider_id=FAKE)
    assert checkout.provider_id == FAKE
    assert checkout.order_id == order.order_id
    with repo._connect() as connection:
        rows = connection.execute(
            "SELECT COUNT(*) AS n FROM payment_attempts WHERE order_id=?",
            (str(order.order_id),),
        ).fetchall()
    assert rows[0]["n"] == 1


def test_unregistered_provider_raises(tmp_path: Path) -> None:
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, expires_at=_utc(2026, 2, 1))
    with pytest.raises(ProviderNotRegisteredError):
        service.start_checkout(order.order_id, provider_id=PaymentProviderId("nope"))


def test_checkout_provider_failure_is_safe(tmp_path: Path) -> None:
    payment_repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    payment_repo.initialize()
    SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3").initialize()
    service = BillingService(payments=payment_repo, clock=_Clock(_utc(2026, 1, 1)))
    service.register_gateway(FakeGateway(provider_id=FAKE, scenario="checkout_failure"))
    plan = _plan()
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    with pytest.raises(CheckoutUnavailableError):
        service.start_checkout(order.order_id, provider_id=FAKE)


# --------------------------------------------------------------------------- #
# Verified-result boundary
# --------------------------------------------------------------------------- #


def test_verified_result_confirms_and_creates_exactly_one_grant(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    subscription = service.handle_verified_result(result)
    assert isinstance(subscription, Subscription)
    assert subscription.authorized_until == _utc(2026, 2, 1)
    assert _deref(repo, order.order_id).status is PaymentStatus.PAID
    assert len(sub_store.get_grants(7)) == 1


def test_billing_service_never_receives_raw_callback(tmp_path: Path) -> None:
    # Raw provider callbacks are verified by the adapter into a VerifiedPaymentResult; the service
    # has only the typed project result to act on, never the raw payload or its signature/receipt.
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    gateway = FakeGateway(provider_id=FAKE)
    verified = gateway.verify_callback(order, _raw_callback_sample())
    assert verified.provider_id == FAKE
    raw = [f for f in verified.__dataclass_fields__ if f in _raw_callback_sample()]
    assert raw == []  # signature/raw_amount are never surfaced on the verified result
    # The service only accepts the verified result.
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    service.handle_verified_result(result)


def test_redirect_only_status_never_confirms(tmp_path: Path) -> None:
    # A browser-return/redirect state (PENDING) can display a status but cannot activate VIP.
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    pending_result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PENDING,
    )
    with pytest.raises(PaymentOrderNotFoundError):
        service.handle_verified_result(pending_result)
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    assert _deref(service._payments, order.order_id).status is PaymentStatus.PENDING
    assert len(sub_store.get_grants(7)) == 0  # still pending, no grant


def test_timeout_status_never_confirms(tmp_path: Path) -> None:
    # A provider timeout/unknown stays pending; no grant is issued from uncertainty.
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    timeout_result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PENDING,
        failure_code="timeout",
    )
    with pytest.raises(PaymentOrderNotFoundError):
        service.handle_verified_result(timeout_result)
    assert _deref(repo, order.order_id).status is PaymentStatus.PENDING


def test_duplicate_callback_creates_no_second_grant(tmp_path: Path) -> None:
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    service.handle_verified_result(result)
    with pytest.raises(PaymentTransactionReplayError):
        service.handle_verified_result(result)  # same result, replayed
    assert len(sub_store.get_grants(7)) == 1  # exactly one grant


def test_expired_order_rejected(tmp_path: Path) -> None:
    service, _, _, plan = _make_service(tmp_path, now=_utc(2026, 1, 10))
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 1, 5))
    service.start_checkout(order.order_id, provider_id=FAKE)
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    with pytest.raises(PaymentOrderExpiredError):
        service.handle_verified_result(result)


def test_wrong_amount_result_fails_closed(tmp_path: Path) -> None:
    from telegram_media_bot.domain.errors import PaymentAmountMismatchError

    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=999999,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    with pytest.raises(PaymentAmountMismatchError):
        service.handle_verified_result(result)
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    assert len(sub_store.get_grants(7)) == 0


# --------------------------------------------------------------------------- #
# Cancellation + expiry
# --------------------------------------------------------------------------- #


def test_cancel_order_before_payment_blocks_later_confirmation(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    service.cancel_order(order.order_id)
    assert _deref(repo, order.order_id).status is PaymentStatus.CANCELLED
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    with pytest.raises(InvalidPaymentTransitionError):
        service.handle_verified_result(result)
    assert _deref(repo, order.order_id).status is PaymentStatus.CANCELLED


def test_refund_after_cancel_is_rejected(tmp_path: Path) -> None:
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.cancel_order(order.order_id)
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    with pytest.raises(PaymentOrderNotFoundError):
        service.refund_verified_payment(result, reason="cancel")


def test_refund_works_through_service(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    service.handle_verified_result(result)
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    assert len(sub_store.get_grants(7)) == 1
    refunded = service.refund_verified_payment(result, reason="refund")
    assert refunded is not None
    assert refunded.authorized_until is None  # access ends immediately
    assert _deref(repo, order.order_id).status is PaymentStatus.REFUNDED


def test_duplicate_refund_rejected(tmp_path: Path) -> None:
    service, repo, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    result = VerifiedPaymentResult(
        provider_id=FAKE,
        provider_transaction_reference=ProviderTransactionReference(f"txn-{order.order_id}"),
        order_reference=str(order.order_id),
        amount_minor=4900,
        currency="USD",
        status=PaymentStatus.PAID,
    )
    service.handle_verified_result(result)
    service.refund_verified_payment(result, reason="refund")
    with pytest.raises(PaymentAlreadyRefundedError):
        service.refund_verified_payment(result, reason="refund-again")
    assert _deref(repo, order.order_id).status is PaymentStatus.REFUNDED


def test_reconciliation_foundation_queries_pending(tmp_path: Path) -> None:
    service, _, _, plan = _make_service(tmp_path)
    order = service.create_order(7, plan, provider_id=FAKE, expires_at=_utc(2026, 2, 1))
    service.start_checkout(order.order_id, provider_id=FAKE)
    # The durable pending-order query bounds by creation time; the order was just created.
    pending = service.list_pending_orders(before=_utc(2026, 1, 2))
    assert any(o.order_id == order.order_id for o in pending)
    counts = service.count_orders_by_status()
    assert counts.get("pending", 0) >= 1
