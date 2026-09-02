"""Tetraminator adapter contract tests (T024/T025).

Covers: JSON create with X-API-KEY, no create retry, amount minimum, callback is a wake-up with no
body (handled by the callback adapter), and authoritative inquiry with pay_id/amount/status
invariants.
"""

from __future__ import annotations

import json as _json
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
from telegram_media_bot.infrastructure.payments.tetraminator import (
    TETRAMINATOR,
    TetraminatorGateway,
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
        provider_id=TETRAMINATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


class ScriptedRequester:
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
        del form, json_body, timeout_seconds
        self.calls.append((method, url))
        for fragment in self.raise_on:
            if fragment in url:
                raise ProviderHttpRequestError("timeout")
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"unscripted request: {method} {url}")

    def json(self, status: int, payload: dict[str, Any]) -> ProviderHttpResponse:
        return ProviderHttpResponse(status, {}, _json.dumps(payload).encode("utf-8"))


def _repo(tmp_path: Path) -> SqlitePaymentRepository:
    repo = SqlitePaymentRepository(tmp_path / "state.sqlite3")
    repo.initialize()
    return repo


def _gateway(repo: SqlitePaymentRepository, requester: ScriptedRequester) -> TetraminatorGateway:
    return TetraminatorGateway(
        base_url="https://api.tetraminator.test/v1",
        api_key="test-api-key",
        callback_url="https://companion.test/payment/callback/tetraminator",
        timeout_seconds=5.0,
        inquiry_retry_count=2,
        payments=repo,
        requester=requester,
    )


def test_create_success_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["invoice/create"] = requester.json(
        200, {"status": True, "pay_id": "tetra-1", "payment_link": "https://tetra.test/pay/tetra-1"}
    )
    gateway = _gateway(repo, requester)

    checkout = gateway.create_payment(order)

    assert checkout.external_checkout_reference == "tetra-1"
    assert checkout.checkout_url == "https://tetra.test/pay/tetra-1"
    method, url = requester.calls[0]
    assert method == "POST" and url.endswith("/invoice/create")
    reservation = repo.get_creation_reservation(order.order_id)
    assert reservation is not None
    assert reservation.state is PaymentCreationState.CREATED


def test_create_never_retries_ambiguous_transport_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.raise_on = ["invoice/create"]
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


def test_create_rejects_status_false_or_missing_fields(tmp_path: Path) -> None:
    payloads: list[dict[str, Any]] = [
        {"status": False, "pay_id": "x", "payment_link": "https://x.test/pay/1"},
        {"status": True, "payment_link": "https://x.test/pay/1"},
        {"status": True, "pay_id": "x"},
    ]
    for index, payload in enumerate(payloads):
        repo = _repo(tmp_path)
        order = _order(order_id=f"ord-{index}")
        repo.save_order(order)
        requester = ScriptedRequester()
        requester.responses["invoice/create"] = requester.json(200, payload)
        gateway = _gateway(repo, requester)
        with pytest.raises(CheckoutUnavailableError):
            gateway.create_payment(order)
        reservation = repo.get_creation_reservation(order.order_id)
        assert reservation is not None
        assert reservation.state is PaymentCreationState.FAILED


def test_amount_below_minimum_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order(amount=10_000)
    repo.save_order(order)
    requester = ScriptedRequester()
    requester.responses["invoice/create"] = requester.json(
        200, {"status": True, "pay_id": "t", "payment_link": "https://t.test/pay/1"}
    )
    gateway = _gateway(repo, requester)
    with pytest.raises(CheckoutUnavailableError):
        gateway.create_payment(order)


def test_query_paid_requires_pay_id_amount_and_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    repo.attach_checkout(
        order_id=order.order_id,
        provider_id=TETRAMINATOR,
        external_checkout_reference="tetra-1",
        checkout_url="https://tetra.test/pay/tetra-1",
        now=order.created_at,
    )
    order = repo.get_order(order.order_id)  # type: ignore[assignment]
    assert order is not None
    requester = ScriptedRequester()
    requester.responses["inquiry/tetra-1"] = requester.json(
        200, {"status": True, "payment_status": "paid", "pay_id": "tetra-1", "amount": 250_000}
    )
    gateway = _gateway(repo, requester)

    verified = gateway.query_payment(order, None)

    assert verified.status is PaymentStatus.PAID
    assert verified.failure_code is None
    assert verified.amount_minor == 250_000
    assert verified.currency == "IRT"


@pytest.mark.parametrize(
    "response, failure_code",
    [
        ({"status": False, "payment_status": "paid"}, "provider_inquiry_unsuccessful"),
        (
            {"status": True, "payment_status": "pending", "pay_id": "tetra-1", "amount": 250_000},
            "provider_not_paid",
        ),
        (
            {"status": True, "payment_status": "paid", "pay_id": "other", "amount": 250_000},
            "provider_pay_id_mismatch",
        ),
        (
            {"status": True, "payment_status": "paid", "pay_id": "tetra-1", "amount": 1},
            "provider_amount_mismatch",
        ),
        (
            {"status": True, "payment_status": "expired", "pay_id": "tetra-1", "amount": 250_000},
            "provider_expired",
        ),
    ],
)
def test_query_fails_closed(tmp_path: Path, response: dict[str, object], failure_code: str) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    repo.attach_checkout(
        order_id=order.order_id,
        provider_id=TETRAMINATOR,
        external_checkout_reference="tetra-1",
        checkout_url="https://tetra.test/pay/tetra-1",
        now=order.created_at,
    )
    order = repo.get_order(order.order_id)  # type: ignore[assignment]
    assert order is not None
    requester = ScriptedRequester()
    requester.responses["inquiry/tetra-1"] = requester.json(200, response)
    gateway = _gateway(repo, requester)

    verified = gateway.query_payment(order, None)

    assert verified.status is not PaymentStatus.PAID
    assert verified.failure_code == failure_code


def test_inquiry_retries_only_transient_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order()
    repo.save_order(order)
    repo.attach_checkout(
        order_id=order.order_id,
        provider_id=TETRAMINATOR,
        external_checkout_reference="tetra-1",
        checkout_url="https://tetra.test/pay/tetra-1",
        now=order.created_at,
    )
    order = repo.get_order(order.order_id)  # type: ignore[assignment]
    assert order is not None
    requester = ScriptedRequester()
    requester.responses["inquiry/tetra-1"] = ProviderHttpResponse(503, {}, b"")
    gateway = _gateway(repo, requester)

    verified = gateway.query_payment(order, None)

    assert verified.status is PaymentStatus.PENDING
    assert verified.failure_code == "provider_inquiry_unsuccessful"
    # bounded retries: 1 attempt + inquiry_retry_count retries
    assert len(requester.calls) == 1 + 2


def test_provider_id_identity() -> None:
    assert PaymentProviderId("tetraminator") == TETRAMINATOR
