"""Provider callback adapters: normalize untrusted callbacks into bounded triggers (T025).

Each adapter knows ONE provider's callback contract and produces only a ``PaymentCallbackTrigger``
carrying the LOCAL order reference. No adapter settles anything, queries the provider, or exposes
secrets. The companion processor follows a trigger with the authoritative read-only query.

- UniquePay: unsigned form POST to the configured callback. ``orderId`` (or the fallback body
  ``order``/query ``order``) identifies the local order; there is no signature to verify.
- Tetraminator: unsigned GET with NO body; the ``order`` query parameter is the local identity.
- HooshPay: signed JSON IPN. ``X-HooshPay-Signature`` must verify against the configured IPN
  secret with fixed-time comparison; only then is the trigger considered, and settlement still
  requires the authoritative ``verify`` query.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from telegram_media_bot.application.ports.companion import (
    PaymentCallbackAdapter,
    PaymentCallbackTrigger,
)
from telegram_media_bot.infrastructure.payments.hooshpay import (
    HOOSHPAY,
    hooshpay_verify_signature,
)
from telegram_media_bot.infrastructure.payments.tetraminator import TETRAMINATOR
from telegram_media_bot.infrastructure.payments.uniquepay import UNIQUEPAY

_SIGNATURE_HEADER = "X-HooshPay-Signature"


def _order_reference_from_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 256:
        return None
    if not cleaned.startswith("order-"):
        return None
    return cleaned


class UniquePayCallbackAdapter(PaymentCallbackAdapter):
    """Unsigned wake-up callback. Never payment proof; query-before-settle only."""

    def normalize(
        self,
        *,
        method: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        body: bytes,
    ) -> PaymentCallbackTrigger:
        del headers
        if method.upper() != "POST":
            return PaymentCallbackTrigger(UNIQUEPAY, None, authentic=False)
        order_reference: str | None = None
        try:
            raw = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            raw = {}
        parsed = raw if isinstance(raw, dict) else {}
        for candidate in (
            parsed.get("orderId"),
            parsed.get("order"),
            query.get("order"),
        ):
            order_reference = _order_reference_from_text(candidate)
            if order_reference is not None:
                break
        if order_reference is None:
            order_reference = _order_reference_from_text(query.get("orderId"))
        # Unsigned by design; the wake-up itself is authentic-as-trigger but powerless.
        return PaymentCallbackTrigger(UNIQUEPAY, order_reference, authentic=True)


class TetraminatorCallbackAdapter(PaymentCallbackAdapter):
    """GET webhook with no body; the provider always calls with query ``order``."""

    def normalize(
        self,
        *,
        method: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        body: bytes,
    ) -> PaymentCallbackTrigger:
        del headers, body
        if method.upper() != "GET":
            return PaymentCallbackTrigger(TETRAMINATOR, None, authentic=False)
        order_reference = _order_reference_from_text(query.get("order"))
        return PaymentCallbackTrigger(TETRAMINATOR, order_reference, authentic=True)


class HooshPayCallbackAdapter(PaymentCallbackAdapter):
    """Signed JSON IPN: fixed-time signature check must pass before any lookup/settle."""

    def __init__(self, *, ipn_secret: str) -> None:
        self._secret = ipn_secret

    def normalize(
        self,
        *,
        method: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        body: bytes,
    ) -> PaymentCallbackTrigger:
        del query
        if method.upper() != "POST" or not body:
            return PaymentCallbackTrigger(HOOSHPAY, None, authentic=False)
        received = headers.get(_SIGNATURE_HEADER)
        if not hooshpay_verify_signature(body, received, self._secret):
            return PaymentCallbackTrigger(HOOSHPAY, None, authentic=False)
        order_reference: str | None = None
        try:
            raw = json.loads(body.decode("utf-8"))
        except ValueError:
            raw = None
        parsed = raw if isinstance(raw, dict) else {}
        order_reference = _order_reference_from_text(parsed.get("order_id"))
        return PaymentCallbackTrigger(HOOSHPAY, order_reference, authentic=True)


class RegistryPaymentCallbacks:
    """Composition-resolved registry mapping provider ids to their callback adapters."""

    def __init__(self, adapters: Mapping[str, PaymentCallbackAdapter]) -> None:
        self._adapters = dict(adapters)

    def adapter_for(self, provider_id: str) -> PaymentCallbackAdapter | None:
        return self._adapters.get(provider_id)


__all__ = [
    "HooshPayCallbackAdapter",
    "PaymentCallbackAdapter",
    "RegistryPaymentCallbacks",
    "TetraminatorCallbackAdapter",
    "UniquePayCallbackAdapter",
]
