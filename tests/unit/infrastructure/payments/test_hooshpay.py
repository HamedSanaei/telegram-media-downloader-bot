"""HooshPay adapter contract tests (T024/T025).

Covers: sorted-key canonical signature, HMAC-SHA256 fixed-time verification, create payload
(fee_mode=buyer, order_id, callback/return URLs), the 50k-1M toman policy, authoritative verify
endpoint, and invoice UID identity/amount/fee-mode invariants.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from telegram_media_bot.domain.subscriptions import PlanId
from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequestError,
    ProviderHttpResponse,
)
from telegram_media_bot.infrastructure.payments.hooshpay import (
    HOOSHPAY,
    HooshPayGateway,
    canonical_signable_json,
    hooshpay_signature,
    hooshpay_verify_signature,
)
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository


def _order(order_id: str = "ord-1", amount: int = 250_000) -> PaymentOrder:
    return PaymentOrder(
        order_id=PaymentOrderId(order_id),
        user_id=7,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        capabilities=frozenset(),
        amount_minor=amount,
        currency="IRT",
        status=PaymentStatus.CREATED,
        provider_id=HOOSHPAY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


class ScriptedRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[object] = []
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
        del headers, form, timeout_seconds
        self.calls.append((method, url))
        self.bodies.append(json_body)
        for fragment in self.raise_on:
            if fragment in url:
                raise ProviderHttpRequestError("timeout")
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"unscripted request: {method} {url}")

    def json(self, status: int, payload: dict[str, Any]) -> ProviderHttpResponse:
        return ProviderHttpResponse(status, {}, json.dumps(payload).encode("utf-8"))


def _repo(tmp_path: Path) -> SqlitePaymentRepository:
    repo = SqlitePaymentRepository(tmp_path / "state.sqlite3")
    repo.initialize()
    return repo


def _gateway(repo: SqlitePaymentRepository, requester: ScriptedRequester) -> HooshPayGateway:
    return HooshPayGateway(
        base_url="https://pay.hooshpay.test",
        api_key="test-api-key",
        callback_url="https://companion.test/payment/callback/hooshpay",
        return_url="https://companion.test/return/hooshpay",
        timeout_seconds=5.0,
        inquiry_retry_count=2,
        payments=repo,
        requester=requester,
    )


# --------------------------------------------------------------------------- #
# Signature contract
# --------------------------------------------------------------------------- #


def test_signature_canonicalizes_sorted_keys() -> None:
    payload = {"b": 2, "a": {"d": 4, "c": 3}, "items": ["x", "y"]}
    canonical = canonical_signable_json(payload)
    assert canonical == json.dumps(
        {"a": {"c": 3, "d": 4}, "b": 2, "items": ["x", "y"]}, separators=(",", ":"), sort_keys=True
    )


def test_signature_roundtrip_and_tamper_detection() -> None:
    payload = json.dumps(
        {"uid": "u-1", "amount": 100000, "nested": {"x": 1}}, separators=(",", ":"), sort_keys=True
    ).encode()
    expected = hmac.new(b"ipn-secret", payload, hashlib.sha256).hexdigest().lower()
    assert hooshpay_signature(payload, "ipn-secret") == expected
    assert hooshpay_verify_signature(payload, expected, "ipn-secret")
    # Tampered body -> rejected; wrong secret -> rejected; missing signature -> rejected.
    assert not hooshpay_verify_signature(b"{}", expected, "ipn-secret")
    assert not hooshpay_verify_signature(payload, expected, "wrong-secret")
    assert not hooshpay_verify_signature(payload, None, "ipn-secret")


def test_signature_is_lowercase_hex() -> None:
    payload = json.dumps({"a": 1}, separators=(",", ":")).encode()
    sig = hooshpay_signature(payload, "s")
    assert sig == sig.lower()
    assert len(sig) == 64


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_payload_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["invoices"] = requester.json(
        200,
        {
            "uid": "hoosh-1",
            "amount": 250_000,
            "fee_mode": "buyer",
            "status": "created",
            "payment_url": "https://pay.hooshpay.test/i/hoosh-1",
        },
    )
    gateway = _gateway(repo, requester)

    checkout = gateway.create_payment(order)

    assert checkout.external_checkout_reference == "hoosh-1"
    assert checkout.checkout_url == "https://pay.hooshpay.test/i/hoosh-1"
    method, url = requester.calls[0]
    assert method == "POST" and url.endswith("/api/v1/invoices")
    sent = requester.bodies[0]
    assert isinstance(sent, dict)
    assert sent["amount"] == 250_000
    assert sent["fee_mode"] == "buyer"
    assert sent["order_id"] == "ord-1"
    assert sent["callback_url"] == "https://companion.test/payment/callback/hooshpay"
    assert sent["return_url"] == "https://companion.test/return/hooshpay"
    assert "X-API-KEY" not in sent  # key travels only in the header
    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.CREATED


def test_create_timeout_resolves_ambiguous_no_retry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.raise_on = ["invoices"]
    gateway = _gateway(repo, requester)
    with pytest.raises(CheckoutUnavailableError):
        gateway.create_payment(order)
    from telegram_media_bot.domain.errors import PaymentCreationReservedError

    with pytest.raises(PaymentCreationReservedError):
        gateway.create_payment(order)
    assert len(requester.calls) == 1
    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.AMBIGUOUS


def test_amount_policy_inclusive_bounds(tmp_path: Path) -> None:
    # Reference contract: 50,000..1,000,000 toman inclusive.
    for amount, accepted in (
        (49_999, False),
        (50_000, True),
        (1_000_000, True),
        (1_000_001, False),
    ):
        repo = _repo(tmp_path)
        order = _order(order_id=f"ord-{amount}", amount=amount)
        repo.save_order(order)
        requester = ScriptedRequester()
        requester.responses["invoices"] = requester.json(
            200,
            {
                "uid": f"hoosh-{amount}",
                "amount": amount,
                "fee_mode": "buyer",
                "status": "created",
                "payment_url": "https://pay.hooshpay.test/i/h",
            },
        )
        gateway = _gateway(repo, requester)
        if accepted:
            gateway.create_payment(order)
        else:
            with pytest.raises(CheckoutUnavailableError):
                gateway.create_payment(order)


# --------------------------------------------------------------------------- #
# Authoritative verify (POST /invoices/{uid}/verify)
# --------------------------------------------------------------------------- #


def _attach(repo: SqlitePaymentRepository, order: PaymentOrder) -> PaymentOrder:
    repo.save_order(order)
    repo.attach_checkout(
        order_id=order.order_id,
        provider_id=HOOSHPAY,
        external_checkout_reference="hoosh-1",
        checkout_url="https://pay.hooshpay.test/i/hoosh-1",
        now=order.created_at,
    )
    restored = repo.get_order(order.order_id)
    assert restored is not None
    return restored


def test_verify_paid_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _attach(repo, _order())
    requester = ScriptedRequester()
    requester.responses["/invoices/hoosh-1/verify"] = requester.json(
        200,
        {
            "uid": "hoosh-1",
            "order_id": "ord-1",
            "amount": 250_000,
            "fee_mode": "buyer",
            "fee_amount": 0,
            "payable_amount": 250_000,
            "status": "PAID",
            "payment_url": "https://pay.hooshpay.test/i/hoosh-1",
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
        ({"uid": "other"}, "provider_uid_mismatch"),
        ({"order_id": "other"}, "provider_order_id_mismatch"),
        ({"amount": 1}, "provider_amount_mismatch"),
        ({"fee_mode": "seller"}, "provider_fee_mode_mismatch"),
        ({"status": "PENDING"}, "provider_not_paid"),
        ({"status": "EXPIRED"}, "provider_expired"),
        ({"status": "CANCELLED"}, "provider_cancelled"),
    ],
)
def test_verify_fails_closed(
    tmp_path: Path, mutation: dict[str, object], failure_code: str
) -> None:
    repo = _repo(tmp_path)
    order = _attach(repo, _order())
    requester = ScriptedRequester()
    row: dict[str, Any] = {
        "uid": "hoosh-1",
        "order_id": "ord-1",
        "amount": 250_000,
        "fee_mode": "buyer",
        "fee_amount": 0,
        "payable_amount": 250_000,
        "status": "PAID",
        "payment_url": "https://pay.hooshpay.test/i/hoosh-1",
    }
    row.update(mutation)
    requester.responses["/invoices/hoosh-1/verify"] = requester.json(200, row)
    gateway = _gateway(repo, requester)

    verified = gateway.query_payment(order, None)

    assert verified.status is not PaymentStatus.PAID
    assert verified.failure_code == failure_code


def test_provider_id_identity() -> None:
    assert PaymentProviderId("hooshpay") == HOOSHPAY
