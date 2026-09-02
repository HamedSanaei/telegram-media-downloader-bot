"""Provider callback adapter tests (T024/T025).

The three provider contracts: UniquePay unsigned form wake-up, Tetraminator GET with no body, and
the HooshPay signed JSON IPN. None of them ever proves payment; each normalizes to a bounded
trigger that the companion follows with an authoritative query. Local order references carry the
`order-` prefix so provider payloads can never be confused with arbitrary identifiers.
"""

from __future__ import annotations

import json

from telegram_media_bot.infrastructure.payments.callbacks import (
    HooshPayCallbackAdapter,
    RegistryPaymentCallbacks,
    TetraminatorCallbackAdapter,
    UniquePayCallbackAdapter,
)


def test_uniquepay_callback_extracts_order_only() -> None:
    adapter = UniquePayCallbackAdapter()
    body = json.dumps({"orderId": "order-1", "other": 1}).encode()
    trigger = adapter.normalize(
        method="POST",
        headers={"content-type": "application/json"},
        query={},
        body=body,
    )
    assert trigger.provider_id == "uniquepay"
    assert trigger.order_reference == "order-1"
    assert trigger.authentic  # wake-up only; the processor still queries before settlement


def test_uniquepay_callback_accepts_query_reference() -> None:
    adapter = UniquePayCallbackAdapter()
    trigger = adapter.normalize(
        method="POST",
        headers={},
        query={"order": "order-5"},
        body=b"",
    )
    assert trigger.order_reference == "order-5"
    assert trigger.authentic


def test_uniquepay_callback_without_order_reference_is_generic() -> None:
    adapter = UniquePayCallbackAdapter()
    trigger = adapter.normalize(method="POST", headers={}, query={}, body=b"")
    assert trigger.order_reference is None
    assert trigger.authentic


def test_tetraminator_callback_is_get_with_no_body() -> None:
    adapter = TetraminatorCallbackAdapter()
    trigger = adapter.normalize(
        method="GET",
        headers={},
        query={"order": "order-9"},
        body=b"",  # provider documents GET with NO body
    )
    assert trigger.provider_id == "tetraminator"
    assert trigger.order_reference == "order-9"
    assert trigger.authentic


def test_tetraminator_callback_without_order_is_generic() -> None:
    adapter = TetraminatorCallbackAdapter()
    trigger = adapter.normalize(method="GET", headers={}, query={}, body=b"")
    assert trigger.order_reference is None


def test_tetraminator_callback_rejects_non_get() -> None:
    adapter = TetraminatorCallbackAdapter()
    trigger = adapter.normalize(method="POST", headers={}, query={"order": "order-9"}, body=b"")
    assert not trigger.authentic
    assert trigger.order_reference is None


def test_hooshpay_ipn_requires_valid_signature() -> None:
    adapter = HooshPayCallbackAdapter(ipn_secret="ipn-secret")
    body = json.dumps({"uid": "hoosh-1", "order_id": "order-1", "amount": 250000}).encode()
    from telegram_media_bot.infrastructure.payments.hooshpay import hooshpay_signature

    valid = adapter.normalize(
        method="POST",
        headers={"X-HooshPay-Signature": hooshpay_signature(body, "ipn-secret")},
        query={},
        body=body,
    )
    assert valid.authentic
    assert valid.order_reference == "order-1"

    forged = adapter.normalize(
        method="POST",
        headers={"X-HooshPay-Signature": "0" * 64},
        query={},
        body=body,
    )
    assert not forged.authentic
    assert forged.order_reference is None

    missing = adapter.normalize(method="POST", headers={}, query={}, body=body)
    assert not missing.authentic
    assert missing.order_reference is None


def test_hooshpay_ipn_rejects_non_json_body() -> None:
    adapter = HooshPayCallbackAdapter(ipn_secret="ipn-secret")
    trigger = adapter.normalize(method="POST", headers={}, query={}, body=b"not-json")
    assert not trigger.authentic
    assert trigger.order_reference is None


def test_hooshpay_ipn_rejects_unprefixed_order_id() -> None:
    """Without the order- prefix the payload cannot locate a local order (generic 404)."""
    adapter = HooshPayCallbackAdapter(ipn_secret="ipn-secret")
    body = json.dumps({"uid": "hoosh-1", "order_id": "hoosh-1"}).encode()
    from telegram_media_bot.infrastructure.payments.hooshpay import hooshpay_signature

    trigger = adapter.normalize(
        method="POST",
        headers={"X-HooshPay-Signature": hooshpay_signature(body, "ipn-secret")},
        query={},
        body=body,
    )
    assert trigger.authentic
    assert trigger.order_reference is None


def test_registry_resolves_adapters_by_provider() -> None:
    registry = RegistryPaymentCallbacks(
        {
            "uniquepay": UniquePayCallbackAdapter(),
            "tetraminator": TetraminatorCallbackAdapter(),
            "hooshpay": HooshPayCallbackAdapter(ipn_secret="s"),
        }
    )
    assert registry.adapter_for("uniquepay") is not None
    assert registry.adapter_for("tetraminator") is not None
    assert registry.adapter_for("hooshpay") is not None
    assert registry.adapter_for("unknown") is None
