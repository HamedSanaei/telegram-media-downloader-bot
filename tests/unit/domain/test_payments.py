"""T015 domain tests: payment order snapshot semantics and the order state machine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_media_bot.domain.errors import InvalidPaymentTransitionError
from telegram_media_bot.domain.payments import (
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
    payment_status_transition,
)
from telegram_media_bot.domain.subscriptions import Capability, PlanId

VIP = Capability.INSTAGRAM_PRIVATE_MEDIA


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _order(status: PaymentStatus = PaymentStatus.CREATED) -> PaymentOrder:
    return PaymentOrder(
        order_id=PaymentOrderId("order-1"),
        user_id=7,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        capabilities=frozenset({VIP}),
        amount_minor=4900,
        currency="usd",
        created_at=_utc(2026, 1, 1),
        expires_at=_utc(2026, 2, 1),
        status=status,
    )


def test_order_validates_snapshot_fields() -> None:
    order = _order()
    assert order.currency == "USD"  # normalized uppercase
    order2 = _order()
    assert order2.capabilities == frozenset({VIP})  # normalized frozenset


def test_order_rejects_invalid_duration() -> None:
    with pytest.raises(ValueError):
        _order_override(duration_months=0)


def test_order_rejects_negative_amount() -> None:
    with pytest.raises(ValueError):
        _order_override(amount_minor=-1)


def test_order_rejects_bad_currency() -> None:
    with pytest.raises(ValueError):
        _order_override(currency="dollar")


def _order_override(
    duration_months: int | None = None, amount_minor: int | None = None, currency: str | None = None
) -> PaymentOrder:
    base = _order()
    return PaymentOrder(
        order_id=base.order_id,
        user_id=base.user_id,
        plan_id=base.plan_id,
        duration_months=duration_months if duration_months is not None else base.duration_months,
        capabilities=base.capabilities,
        amount_minor=amount_minor if amount_minor is not None else base.amount_minor,
        currency=currency if currency is not None else base.currency,
        created_at=base.created_at,
        expires_at=base.expires_at,
        status=base.status,
    )


def test_status_enum_values() -> None:
    assert PaymentStatus.CREATED.value == "created"
    assert PaymentStatus.PENDING.value == "pending"
    assert PaymentStatus.PAID.value == "paid"
    assert PaymentStatus.FAILED.value == "failed"
    assert PaymentStatus.CANCELLED.value == "cancelled"
    assert PaymentStatus.EXPIRED.value == "expired"
    assert PaymentStatus.REFUNDED.value == "refunded"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PaymentStatus.CREATED, PaymentStatus.PENDING),
        (PaymentStatus.CREATED, PaymentStatus.PAID),
        (PaymentStatus.CREATED, PaymentStatus.FAILED),
        (PaymentStatus.CREATED, PaymentStatus.CANCELLED),
        (PaymentStatus.CREATED, PaymentStatus.EXPIRED),
        (PaymentStatus.PENDING, PaymentStatus.PAID),
        (PaymentStatus.PENDING, PaymentStatus.FAILED),
        (PaymentStatus.PENDING, PaymentStatus.CANCELLED),
        (PaymentStatus.PENDING, PaymentStatus.EXPIRED),
        (PaymentStatus.PAID, PaymentStatus.REFUNDED),
    ],
)
def test_allowed_transitions(current: PaymentStatus, target: PaymentStatus) -> None:
    assert payment_status_transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Backward/terminal rewriting must never be allowed.
        (PaymentStatus.PAID, PaymentStatus.PENDING),
        (PaymentStatus.REFUNDED, PaymentStatus.PAID),
        (PaymentStatus.EXPIRED, PaymentStatus.PAID),
        (PaymentStatus.CANCELLED, PaymentStatus.PAID),
        (PaymentStatus.PAID, PaymentStatus.CANCELLED),
        (PaymentStatus.EXPIRED, PaymentStatus.PENDING),
        (PaymentStatus.REFUNDED, PaymentStatus.PENDING),
        (PaymentStatus.CANCELLED, PaymentStatus.EXPIRED),
        (PaymentStatus.FAILED, PaymentStatus.PAID),
        (PaymentStatus.CANCELLED, PaymentStatus.PAID),
    ],
)
def test_forbidden_transitions(current: PaymentStatus, target: PaymentStatus) -> None:
    with pytest.raises(InvalidPaymentTransitionError):
        payment_status_transition(current, target)


def test_same_status_is_identity() -> None:
    assert payment_status_transition(PaymentStatus.PAID, PaymentStatus.PAID) is PaymentStatus.PAID


def test_terminal_states_have_no_forwards() -> None:
    for terminal in (
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
        PaymentStatus.REFUNDED,
    ):
        with pytest.raises(InvalidPaymentTransitionError):
            payment_status_transition(terminal, PaymentStatus.PAID)


def test_verified_result_validates_currency_and_reference() -> None:
    with pytest.raises(ValueError):
        VerifiedPaymentResult(
            provider_id=PaymentProviderId("p"),
            provider_transaction_reference=ProviderTransactionReference(""),
            order_reference="o",
            amount_minor=4900,
            currency="USD",
            status=PaymentStatus.PAID,
        )
    with pytest.raises(ValueError):
        VerifiedPaymentResult(
            provider_id=PaymentProviderId("p"),
            provider_transaction_reference=ProviderTransactionReference("tx"),
            order_reference="o",
            amount_minor=4900,
            currency="badd",
            status=PaymentStatus.PAID,
        )
