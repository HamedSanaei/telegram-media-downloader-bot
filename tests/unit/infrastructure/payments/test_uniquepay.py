"""UniquePay adapter contract tests (T024).

Covers the durable creation reservation, the fail-closed authoritative inquiry matrix, the
amount/currency/fee-payer contract, and the `isVerified`-is-not-proof rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.domain.errors import CheckoutUnavailableError
from telegram_media_bot.domain.payments import (
    PaymentCreationState,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
)
from telegram_media_bot.domain.subscriptions import PlanId, SubscriptionPlan
from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequestError,
    ProviderHttpResponse,
)
from telegram_media_bot.infrastructure.payments.uniquepay import (
    UNIQUEPAY,
    UniquePayGateway,
    uniquepay_hash_id,
)
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository

PLAN = SubscriptionPlan(
    plan_id=PlanId("vip-1"),
    name="VIP",
    duration_months=1,
    price_minor=250_000,
    currency="IRT",
    enabled=True,
    capabilities=frozenset(),
)


def _order(order_id: str = "ord-1", amount: int = 250_000) -> PaymentOrder:
    return PaymentOrder(
        order_id=PaymentOrderId(order_id),
        user_id=7,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        amount_minor=amount,
        currency="IRT",
        capabilities=frozenset(),
        status=PaymentStatus.CREATED,
        provider_id=UNIQUEPAY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


class ScriptedRequester:
    """Test transport: scripted responses per path; raises on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[str, ProviderHttpResponse] = {}
        self.raise_on: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: object | None = None,
        form: object | None = None,
        json_body: object | None = None,
        timeout_seconds: float,
    ) -> ProviderHttpResponse:
        del headers, form, json_body, timeout_seconds
        self.calls.append((method, url))
        for fragment in self.raise_on:
            if fragment in url:
                raise ProviderHttpRequestError("timeout")
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"unscripted request: {method} {url}")

    def json(self, status: int, payload: dict[str, Any]) -> ProviderHttpResponse:
        import json as _json

        return ProviderHttpResponse(status, {}, _json.dumps(payload).encode("utf-8"))


def _gateway(repo: SqlitePaymentRepository, requester: ScriptedRequester) -> UniquePayGateway:
    return UniquePayGateway(
        base_url="https://uniquepay.test",
        business_token="test-business-token",
        callback_url="https://companion.test/payment/callback/uniquepay",
        return_url="https://companion.test/return/uniquepay",
        timeout_seconds=5.0,
        inquiry_retry_count=2,
        payments=repo,
        requester=requester,
    )


def _repo(tmp_path: Path) -> SqlitePaymentRepository:
    repo = SqlitePaymentRepository(tmp_path / "state.sqlite3")
    repo.initialize()
    return repo


def _save_with_checkout(repo: SqlitePaymentRepository, order: PaymentOrder) -> PaymentOrder:
    """Full lifecycle: the order is attached at create time; queries verify against it."""
    repo.save_order(order)
    repo.attach_checkout(
        order_id=order.order_id,
        provider_id=UNIQUEPAY,
        external_checkout_reference="up-ref-1",
        checkout_url="https://uniquepay.test/pay/up-ref-1",
        now=order.created_at,
    )
    restored = repo.get_order(order.order_id)
    assert restored is not None
    return restored


def test_creation_success_reserves_and_attaches(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["create-invoice"] = requester.json(
        200,
        {
            "status": True,
            "code": 200,
            "hashId": uniquepay_hash_id(order),
            "refId": "up-ref-1",
            "paymentLink": "https://uniquepay.test/pay/up-ref-1",
        },
    )
    gateway = _gateway(repo, requester)

    checkout = gateway.create_payment(order)

    assert checkout.external_checkout_reference == "up-ref-1"
    assert checkout.checkout_url == "https://uniquepay.test/pay/up-ref-1"
    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.CREATED
    assert reservation.merchant_reference == uniquepay_hash_id(order)
    restored = repo.get_order(order.order_id)
    assert restored is not None
    assert restored.external_checkout_reference == "up-ref-1"


def test_creation_timeout_resolves_ambiguous_and_never_retries(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.raise_on = ["create-invoice"]
    gateway = _gateway(repo, requester)

    with pytest.raises(CheckoutUnavailableError):
        gateway.create_payment(order)

    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.AMBIGUOUS
    # A second create attempt must be refused by the durable reservation.
    from telegram_media_bot.domain.errors import PaymentCreationReservedError

    with pytest.raises(PaymentCreationReservedError):
        gateway.create_payment(order)
    assert len(requester.calls) == 1  # exactly one POST ever issued
    assert all(call[1].endswith("/create-invoice") for call in requester.calls)


def test_creation_400_rejected_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["create-invoice"] = requester.json(400, {"status": False})
    gateway = _gateway(repo, requester)

    with pytest.raises(CheckoutUnavailableError):
        gateway.create_payment(order)

    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.FAILED
    restored = repo.get_order(order.order_id)
    assert restored is not None and restored.external_checkout_reference is None


def test_creation_missing_ref_or_link_is_not_success(tmp_path: Path) -> None:
    for index, missing in enumerate(("refId", "paymentLink")):
        repo = _repo(tmp_path / f"state-{index}.sqlite3")
        order = _order(order_id=f"ord-{index}")
        repo.save_order(order)
        requester = ScriptedRequester()
        payload: dict[str, Any] = {
            "status": True,
            "code": 200,
            "hashId": uniquepay_hash_id(order),
            "refId": "up-ref-1",
            "paymentLink": "https://uniquepay.test/pay/up-ref-1",
        }
        payload.pop(missing)
        requester.responses["create-invoice"] = requester.json(200, payload)
        gateway = _gateway(repo, requester)

        with pytest.raises(CheckoutUnavailableError):
            gateway.create_payment(order)
        reservation = repo.get_creation_reservation(order.order_id)
        assert reservation is not None
        assert reservation.state is PaymentCreationState.FAILED


def test_creation_hash_id_mismatch_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["create-invoice"] = requester.json(
        200,
        {
            "status": True,
            "code": 200,
            "hashId": "another-hash",
            "refId": "up-ref-1",
            "paymentLink": "https://uniquepay.test/pay/up-ref-1",
        },
    )
    gateway = _gateway(repo, requester)
    with pytest.raises(CheckoutUnavailableError):
        gateway.create_payment(order)
    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.FAILED


def test_query_paid_requires_full_invariant_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _save_with_checkout(repo, _order())
    requester = ScriptedRequester()
    requester.responses["check-invoice"] = requester.json(
        200,
        {
            "status": True,
            "code": 200,
            "hashId": uniquepay_hash_id(order),
            "refId": "up-ref-1",
            "invoice": {
                "id": "up-ref-1",
                "currency": "IRT",
                "feePayer": "buyer",
                "amount": 250_000,
                "fee": 0,
                "payableAmount": 250_000,
                "uniqueAmount": 0,
                "isPaid": True,
            },
        },
    )
    gateway = _gateway(repo, requester)
    verified = gateway.query_payment(order, None)
    assert verified.status is PaymentStatus.PAID
    assert verified.failure_code is None
    assert verified.amount_minor == 250_000
    assert verified.currency == "IRT"


@pytest.mark.parametrize(
    "mutation, failure_code",
    [
        ({"status": False}, "provider_inquiry_unsuccessful"),
        ({"code": 500}, "provider_inquiry_unsuccessful"),
        ({"hashId": "wrong"}, "provider_hash_id_mismatch"),
        ({"refId": "wrong-ref"}, "provider_ref_id_mismatch"),
        ({"invoice": {"id": "wrong-ref"}}, "provider_ref_id_mismatch"),
        ({"invoice": {"currency": "USD"}}, "provider_currency_mismatch"),
        ({"invoice": {"feePayer": "merchant"}}, "provider_fee_payer_mismatch"),
        ({"invoice": {"amount": 1}}, "provider_base_amount_mismatch"),
        ({"invoice": {"payableAmount": 1}}, "provider_payable_amount_mismatch"),
    ],
)
def test_query_paid_fails_closed_on_any_invariant_mismatch(
    tmp_path: Path, mutation: dict[str, object], failure_code: str
) -> None:
    repo = _repo(tmp_path)
    order = _save_with_checkout(repo, _order())
    requester = ScriptedRequester()
    base: dict[str, Any] = {
        "status": True,
        "code": 200,
        "hashId": uniquepay_hash_id(order),
        "refId": "up-ref-1",
        "invoice": {
            "id": "up-ref-1",
            "currency": "IRT",
            "feePayer": "buyer",
            "amount": 250_000,
            "fee": 0,
            "payableAmount": 250_000,
            "uniqueAmount": 0,
            "isPaid": True,
        },
    }
    for key, value in mutation.items():
        if key == "invoice":
            base["invoice"].update(value)
        else:
            base[key] = value
    requester.responses["check-invoice"] = requester.json(200, base)
    gateway = _gateway(repo, requester)
    verified = gateway.query_payment(order, None)
    assert verified.status is not PaymentStatus.PAID
    assert verified.failure_code == failure_code


def test_is_verified_alone_never_confirms(tmp_path: Path) -> None:
    """The provider may set isVerified; it must not flip our economic verdict."""
    repo = _repo(tmp_path)
    order = _save_with_checkout(repo, _order())
    requester = ScriptedRequester()
    requester.responses["check-invoice"] = requester.json(
        200,
        {
            "status": True,
            "code": 200,
            "hashId": uniquepay_hash_id(order),
            "refId": "up-ref-1",
            "invoice": {
                "id": "up-ref-1",
                "currency": "IRT",
                "feePayer": "buyer",
                "amount": 250_000,
                "fee": 0,
                "payableAmount": 250_000,
                "uniqueAmount": 0,
                "isVerified": True,  # never proof
                "isPaid": False,
            },
        },
    )
    gateway = _gateway(repo, requester)
    verified = gateway.query_payment(order, None)
    assert verified.status is not PaymentStatus.PAID
    assert verified.failure_code == "provider_not_paid"


def test_query_terminal_states_mapped(tmp_path: Path) -> None:
    row_template: dict[str, Any] = {
        "status": True,
        "code": 200,
        "hashId": "ignored-ok",
        "refId": "up-ref-1",
        "invoice": {
            "id": "up-ref-1",
            "currency": "IRT",
            "feePayer": "buyer",
            "amount": 250_000,
            "fee": 0,
            "payableAmount": 250_000,
            "uniqueAmount": 0,
            "isPaid": False,
        },
    }
    cases: list[tuple[dict[str, object], PaymentStatus]] = [
        ({"invoice": {"isExpired": True}}, PaymentStatus.EXPIRED),
        ({"invoice": {"isCancelled": True}}, PaymentStatus.CANCELLED),
        ({"invoice": {"paymentStatus": "failed"}}, PaymentStatus.FAILED),
    ]
    for mutation, expected in cases:
        repo = _repo(tmp_path)
        order = _save_with_checkout(repo, _order(order_id=f"ord-{expected.value}"))
        requester = ScriptedRequester()
        row = _deep_copy(row_template)
        row["hashId"] = uniquepay_hash_id(order)
        row["invoice"]["id"] = "up-ref-1"
        invoice_row = row["invoice"]
        assert isinstance(invoice_row, dict)
        for value in mutation.values():
            assert isinstance(value, dict)
            invoice_row.update(value)
        requester.responses["check-invoice"] = requester.json(200, row)
        gateway = _gateway(repo, requester)
        verified = gateway.query_payment(order, None)
        assert verified.status is expected


def test_inquiry_transient_returns_pending_not_paid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["check-invoice"] = ProviderHttpResponse(503, {}, b"")
    gateway = _gateway(repo, requester)
    verified = gateway.query_payment(order, None)
    assert verified.status is PaymentStatus.PENDING
    assert verified.failure_code == "provider_inquiry_unavailable"


def test_inquiry_transport_error_returns_pending(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.raise_on = ["check-invoice"]
    gateway = _gateway(repo, requester)
    verified = gateway.query_payment(order, None)
    assert verified.status is PaymentStatus.PENDING
    assert verified.failure_code == "inquiry_unavailable"


def _deep_copy(payload: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(payload)


def test_provider_id_identity() -> None:
    assert PaymentProviderId("uniquepay") == UNIQUEPAY
