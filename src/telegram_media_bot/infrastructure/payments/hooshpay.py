"""HooshPay invoice adapter (T024).

Contract source: the proven 3xui-bot ``Domain/HooshPay.cs`` implementation (provider is not in the
supplied ZIP documentation). Default host ``https://pay.hooshnet.com`` with API root ``/api/v1``.
- Create: ``POST /api/v1/invoices`` (header ``X-API-KEY``, JSON ``{amount, fee_mode: "buyer",
  order_id, description, callback_url, return_url}``). Provider-enforced inclusive amount range:
  50,000..1,000,000 whole toman. Success returns the invoice with ``uid`` and ``payment_url``;
  the invoice ``uid`` is the durable provider transaction identity.
- Signature: IPN bodies carry ``X-HooshPay-Signature`` = lowercase hex HMAC-SHA256 of the
  canonical body (recursively sorted object keys, compact JSON) using the configured IPN secret.
  Comparisons use fixed-time equality; even a valid signature only triggers a point-in-time
  provider verification before settlement.
- Verify: ``POST /api/v1/invoices/{uid}/verify`` is the authoritative paid check; the paid
  verdict requires uid identity, order identity, exact base toman amount, ``fee_mode = buyer``,
  and the provider paid state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from telegram_media_bot.application.ports.payments import PaymentGateway, PaymentRepository
from telegram_media_bot.domain.errors import CheckoutUnavailableError
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
from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequester,
    ProviderHttpRequestError,
    ProviderHttpResponse,
    StdlibHttpRequester,
    is_transient_status,
    request_with_retries,
)

HOOSHPAY = PaymentProviderId("hooshpay")

_MIN_AMOUNT_TOMAN = 50_000
_MAX_AMOUNT_TOMAN = 1_000_000
_FEE_MODE_BUYER = "buyer"


def canonical_signable_json(payload: object) -> str:
    """Recursively sort object keys and serialize compactly (signature canonical form)."""

    def sort_token(value: object) -> object:
        if isinstance(value, dict):
            return {key: sort_token(value[key]) for key in sorted(value, key=str)}
        if isinstance(value, list):
            return [sort_token(item) for item in value]
        if isinstance(value, tuple):
            return [sort_token(item) for item in value]
        return value

    return json.dumps(sort_token(payload), separators=(",", ":"), ensure_ascii=False)


def hooshpay_signature(payload: bytes, secret: str) -> str:
    """Lowercase hex HMAC-SHA256 over the canonical JSON form of the body."""
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except ValueError:
        parsed = payload.decode("utf-8")
    canonical = canonical_signable_json(parsed)
    digest = hmac.new(
        secret.strip().encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest.lower()


def hooshpay_verify_signature(payload: bytes, received: str | None, secret: str) -> bool:
    """Fixed-time comparison of the received ``X-HooshPay-Signature`` value."""
    if not payload or not received or not secret:
        return False
    computed = hooshpay_signature(payload, secret)
    return hmac.compare_digest(computed, received.strip().lower())


__all__ = [
    "HOOSHPAY",
    "HooshPayGateway",
    "canonical_signable_json",
    "hooshpay_signature",
    "hooshpay_verify_signature",
]


class HooshPayGateway(PaymentGateway):
    """HooshPay adapter. ``payments`` carries the durable creation-reservation store."""

    provider_id = HOOSHPAY

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        callback_url: str,
        return_url: str,
        timeout_seconds: float,
        inquiry_retry_count: int,
        payments: PaymentRepository | None = None,
        requester: ProviderHttpRequester | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._callback_url = callback_url
        self._return_url = return_url
        self._timeout = timeout_seconds
        self._retries = max(0, inquiry_retry_count)
        self._payments = payments
        self._requester = requester or StdlibHttpRequester()

    def available_for_new_checkout(self) -> bool:
        return bool(self._api_key and self._callback_url and self._return_url)

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        if not self.available_for_new_checkout():
            raise CheckoutUnavailableError("HooshPay is not available for new checkouts")
        if self._payments is None:
            raise CheckoutUnavailableError("HooshPay creation store is not configured")
        if order.amount_minor < _MIN_AMOUNT_TOMAN or order.amount_minor > _MAX_AMOUNT_TOMAN:
            raise CheckoutUnavailableError(
                "HooshPay amount must be between 50,000 and 1,000,000 toman inclusive"
            )
        merchant_reference = f"tmb-hoosh-{order.order_id!s}"
        self._payments.begin_creation_attempt(
            order_id=order.order_id,
            provider_id=self.provider_id,
            merchant_reference=merchant_reference,
            attempted_at=datetime.now(UTC),
        )
        try:
            response = self._requester.request(
                "POST",
                self._base_url + "/api/v1/invoices",
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json_body={
                    "amount": order.amount_minor,
                    "fee_mode": _FEE_MODE_BUYER,
                    "order_id": str(order.order_id),
                    "description": "VIP subscription",
                    "callback_url": self._callback_url,
                    "return_url": self._return_url,
                },
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError(
                "HooshPay could not create the invoice (ambiguous)"
            ) from None
        parsed = self._parse_create(order, response)
        if parsed is None:
            self._resolve(order.order_id, PaymentCreationState.FAILED, "create_failed")
            raise CheckoutUnavailableError("HooshPay rejected the invoice creation")
        uid, payment_url = parsed
        self._resolve(order.order_id, PaymentCreationState.CREATED, None)
        self._payments.attach_checkout(
            order_id=order.order_id,
            provider_id=self.provider_id,
            external_checkout_reference=uid,
            checkout_url=payment_url,
            now=datetime.now(UTC),
        )
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,
            external_checkout_reference=uid,
            created_at=datetime.now(UTC),
            expires_at=order.expires_at,
            checkout_url=payment_url,
        )

    def _parse_create(
        self, order: PaymentOrder, response: ProviderHttpResponse
    ) -> tuple[str, str] | None:
        if response.status >= 400 and response.status < 500:
            return None
        if is_transient_status(response.status):
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError("HooshPay could not create the invoice (ambiguous)")
        try:
            body = response.json()
        except ValueError:
            return None
        invoice = body if isinstance(body, dict) else None
        if invoice is None:
            return None
        uid = invoice.get("uid")
        payment_url = invoice.get("payment_url")
        if not isinstance(uid, str) or not uid.strip():
            return None
        if not isinstance(payment_url, str) or not payment_url.strip():
            return None
        status_value = str(invoice.get("status", "")).strip().lower()
        if status_value and status_value not in {
            "created",
            "pending",
            "open",
            "active",
            "ok",
            "success",
        }:
            return None
        return uid.strip(), payment_url.strip()

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        uid = (
            provider_transaction_reference
            if provider_transaction_reference is not None
            else order.external_checkout_reference
        )
        if not uid:
            return VerifiedPaymentResult(
                provider_id=self.provider_id,
                provider_transaction_reference=ProviderTransactionReference(
                    order.external_checkout_reference or str(order.order_id)
                ),
                order_reference=str(order.order_id),
                amount_minor=order.amount_minor,
                currency="IRT",
                status=PaymentStatus.PENDING,
                failure_code="no_provider_reference",
            )
        try:
            response = request_with_retries(
                self._requester,
                "POST",
                self._base_url + "/api/v1/invoices/" + str(uid) + "/verify",
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json_body={},
                timeout_seconds=self._timeout,
                retry_count=self._retries,
            )
        except ProviderHttpRequestError:
            return VerifiedPaymentResult(
                provider_id=self.provider_id,
                provider_transaction_reference=ProviderTransactionReference(str(uid)),
                order_reference=str(order.order_id),
                amount_minor=order.amount_minor,
                currency="IRT",
                status=PaymentStatus.PENDING,
                failure_code="inquiry_unavailable",
            )
        error, paid = self._verify_inquiry(order, str(uid), response)
        status = PaymentStatus.PAID if paid else PaymentStatus.PENDING
        if error in {"provider_expired", "provider_cancelled", "provider_failed"}:
            status = PaymentStatus(error[9:])
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference(str(uid)),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency="IRT",
            status=status,
            failure_code=error,
        )

    def _verify_inquiry(
        self, order: PaymentOrder, uid: str, response: ProviderHttpResponse
    ) -> tuple[str | None, bool]:
        """Fail-closed: paid only when uid/order identity, exact base toman amount, buyer fee
        mode, and the provider paid state all match the local order snapshot."""
        if response.status >= 400 or is_transient_status(response.status):
            return "provider_inquiry_unsuccessful", False
        try:
            body = response.json()
        except ValueError:
            return "provider_inquiry_unsuccessful", False
        if not isinstance(body, dict):
            return "provider_inquiry_unsuccessful", False
        raw_invoice = body.get("invoice")
        invoice: dict[object, object] = raw_invoice if isinstance(raw_invoice, dict) else body
        returned_uid = invoice.get("uid")
        if returned_uid is not None and str(returned_uid).strip() != uid:
            return "provider_uid_mismatch", False
        order_id = invoice.get("order_id")
        if order_id is not None and str(order_id).strip() != str(order.order_id):
            return "provider_order_id_mismatch", False
        try:
            amount = int(str(invoice["amount"]))
        except KeyError, TypeError, ValueError:
            return "provider_amount_invalid", False
        if amount != order.amount_minor:
            return "provider_amount_mismatch", False
        fee_mode = str(invoice.get("fee_mode", "")).strip().lower()
        if fee_mode and fee_mode != _FEE_MODE_BUYER:
            return "provider_fee_mode_mismatch", False
        status_value = str(invoice.get("status", "")).strip().lower()
        is_paid = invoice.get("is_paid") is True or status_value == "paid"
        if is_paid:
            return None, True
        if status_value == "expired":
            return "provider_expired", False
        if status_value in {"cancelled", "canceled"}:
            return "provider_cancelled", False
        if status_value in {"failed", "rejected"}:
            return "provider_failed", False
        return "provider_not_paid", False

    def _resolve(
        self, order_id: PaymentOrderId, state: PaymentCreationState, error_code: str | None
    ) -> None:
        if self._payments is None:
            raise CheckoutUnavailableError("HooshPay creation store is not configured")
        self._payments.resolve_creation_attempt(
            order_id=order_id,
            state=state,
            error_code=error_code,
            resolved_at=datetime.now(UTC),
        )
