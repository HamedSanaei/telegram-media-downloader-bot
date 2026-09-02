"""UniquePay DDBot invoice adapter (T024).

Contract source: repository ``api_docs/uniquepay`` plus the hardened 3xui-bot verifier.
- Create: ``POST /api/ddbot/create-invoice`` (form: hashId, amount, orderId, callbackUrl,
  redirectUrl; ``Authorization: Bearer <business_token>``). Success requires status==true,
  code==200, matching hashId, non-empty refId and paymentLink.
- Inquiry: ``POST /api/check-invoice`` (form: hashId). Authoritative paid verdict requires the
  full identity/currency/fee-payer/amount/payable equation; a browser redirect, callback, created
  link, or ``isVerified`` alone NEVER confirms.
- The one create POST is reserved durably (``begin_creation_attempt``) before any network byte;
  ambiguity survives as ``ambiguous`` and recovery is inquiry-only. The merchant ``hashId`` is a
  deterministic function of the local order, so recovery queries the same invoice.
"""

from __future__ import annotations

import hashlib
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

UNIQUEPAY = PaymentProviderId("uniquepay")

_CREATE_PATH = "/api/ddbot/create-invoice"
_CHECK_PATH = "/api/check-invoice"
_CURRENCIES = frozenset({"IRT", "TOMAN"})
_FEE_PAYERS = frozenset({"buyer", "user", "owner"})


def uniquepay_hash_id(order: PaymentOrder) -> str:
    """Deterministic merchant hash derived from the local order (never a credential)."""
    return hashlib.sha256(f"tmb:uniquepay:{order.order_id!s}".encode()).hexdigest()


class UniquePayGateway(PaymentGateway):
    """UniquePay adapter. ``payments`` carries the durable creation-reservation store."""

    provider_id = UNIQUEPAY

    def __init__(
        self,
        *,
        base_url: str,
        business_token: str,
        callback_url: str,
        return_url: str,
        timeout_seconds: float,
        inquiry_retry_count: int,
        payments: PaymentRepository | None = None,
        requester: ProviderHttpRequester | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = business_token
        self._callback_url = callback_url
        self._return_url = return_url
        self._timeout = timeout_seconds
        self._retries = max(0, inquiry_retry_count)
        self._payments = payments
        self._requester = requester or StdlibHttpRequester()

    def available_for_new_checkout(self) -> bool:
        return bool(self._token and self._callback_url and self._return_url)

    # -- create (exactly once, durably reserved) --------------------------------

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        if not self.available_for_new_checkout():
            raise CheckoutUnavailableError("UniquePay is not available for new checkouts")
        if self._payments is None:
            raise CheckoutUnavailableError("UniquePay creation store is not configured")
        hash_id = uniquepay_hash_id(order)
        self._payments.begin_creation_attempt(
            order_id=order.order_id,
            provider_id=self.provider_id,
            merchant_reference=hash_id,
            attempted_at=datetime.now(UTC),
        )
        try:
            response = self._requester.request(
                "POST",
                self._base_url + _CREATE_PATH,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                form={
                    "hashId": hash_id,
                    "amount": str(order.amount_minor),
                    "orderId": str(order.order_id),
                    "callbackUrl": self._callback_url,
                    "redirectUrl": self._return_url,
                },
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError(
                "UniquePay checkout could not be created (ambiguous)"
            ) from None
        parsed = self._parse_create(hash_id, order, response)
        if parsed is None:
            self._resolve(order.order_id, PaymentCreationState.FAILED, "create_failed")
            raise CheckoutUnavailableError("UniquePay rejected the checkout creation")
        ref_id, payment_link = parsed
        self._resolve(order.order_id, PaymentCreationState.CREATED, None)
        self._payments.attach_checkout(
            order_id=order.order_id,
            provider_id=self.provider_id,
            external_checkout_reference=ref_id,
            checkout_url=payment_link,
            now=datetime.now(UTC),
        )
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,
            external_checkout_reference=ref_id,
            created_at=datetime.now(UTC),
            expires_at=order.expires_at,
            checkout_url=payment_link,
        )

    def _parse_create(
        self, hash_id: str, order: PaymentOrder, response: ProviderHttpResponse
    ) -> tuple[str, str] | None:
        if response.status >= 400 and response.status < 500:
            return None
        if is_transient_status(response.status):
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError("UniquePay checkout could not be created (ambiguous)")
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        if body.get("status") is not True or body.get("code") not in (200, "200"):
            return None
        echo = body.get("hashId")
        if isinstance(echo, str) and echo.strip() and echo.strip() != hash_id:
            return None
        ref_id = body.get("refId")
        payment_link = body.get("paymentLink")
        if not isinstance(ref_id, str) or not ref_id.strip():
            return None
        if not isinstance(payment_link, str) or not payment_link.strip():
            return None
        if order.amount_minor <= 50000:
            # Provider rejects exactly 50,000 toman; refuse a payable link otherwise.
            return None
        return ref_id.strip(), payment_link.strip()

    # -- authoritative inquiry -------------------------------------------------

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        del provider_transaction_reference
        fallback_ref = order.external_checkout_reference or uniquepay_hash_id(order)
        try:
            response = request_with_retries(
                self._requester,
                "POST",
                self._base_url + _CHECK_PATH,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                form={"hashId": uniquepay_hash_id(order)},
                timeout_seconds=self._timeout,
                retry_count=self._retries,
            )
        except ProviderHttpRequestError:
            return VerifiedPaymentResult(
                provider_id=self.provider_id,
                provider_transaction_reference=ProviderTransactionReference(fallback_ref),
                order_reference=str(order.order_id),
                amount_minor=order.amount_minor,
                currency="IRT",
                status=PaymentStatus.PENDING,
                failure_code="inquiry_unavailable",
            )
        error, paid = self._verify_inquiry(order, response)
        status = PaymentStatus.PENDING
        if paid:
            status = PaymentStatus.PAID
        elif error == "provider_not_paid":
            status = PaymentStatus.PENDING
        elif error == "provider_expired":
            status = PaymentStatus.EXPIRED
        elif error == "provider_cancelled":
            status = PaymentStatus.CANCELLED
        elif error == "provider_failed":
            status = PaymentStatus.FAILED
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference(fallback_ref),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency="IRT",
            status=status,
            failure_code=error,
        )

    def _verify_inquiry(
        self, order: PaymentOrder, response: ProviderHttpResponse
    ) -> tuple[str | None, bool]:
        """Fail-closed verification: ``(error_code, paid)`` with ``paid`` True only when every
        identity/financial invariant matches the local order snapshot."""
        if response.status >= 500 or is_transient_status(response.status):
            return "provider_inquiry_unavailable", False
        if response.status >= 400 and response.status < 500:
            return "provider_inquiry_unsuccessful", False
        try:
            body = response.json()
        except ValueError:
            return "provider_inquiry_unsuccessful", False
        if not isinstance(body, dict):
            return "provider_inquiry_unsuccessful", False
        if body.get("status") is not True or body.get("code") not in (200, "200"):
            return "provider_inquiry_unsuccessful", False
        echo = body.get("hashId")
        if isinstance(echo, str) and echo.strip() and echo.strip() != uniquepay_hash_id(order):
            return "provider_hash_id_mismatch", False
        invoice = body.get("invoice")
        if not isinstance(invoice, dict):
            return "provider_invoice_missing", False
        raw_ref = body.get("refId")
        identity = raw_ref if isinstance(raw_ref, str) and raw_ref.strip() else invoice.get("id")
        if (
            isinstance(identity, str)
            and identity.strip()
            and order.external_checkout_reference
            and identity.strip() != order.external_checkout_reference
        ):
            return "provider_ref_id_mismatch", False
        # When the provider reports BOTH the top-level refId and the invoice id, both must agree
        # with the saved checkout identity (fail closed on any ambiguity).
        invoice_id = invoice.get("id")
        if (
            isinstance(raw_ref, str)
            and raw_ref.strip()
            and isinstance(invoice_id, str)
            and invoice_id.strip()
            and raw_ref.strip() != invoice_id.strip()
        ):
            return "provider_ref_id_mismatch", False
        currency = str(invoice.get("currency", "")).strip().upper()
        if currency not in _CURRENCIES:
            return "provider_currency_mismatch", False
        fee_payer = str(invoice.get("feePayer", "")).strip().lower()
        if fee_payer not in _FEE_PAYERS:
            return "provider_fee_payer_mismatch", False
        try:
            amount = int(invoice["amount"])
            fee = int(invoice.get("fee", 0) or 0)
        except KeyError, TypeError, ValueError:
            return "provider_amount_invalid", False
        if amount != order.amount_minor:
            return "provider_base_amount_mismatch", False
        payable = invoice.get("payableAmount")
        unique_amount = invoice.get("uniqueAmount")
        if payable is not None or unique_amount is not None:
            try:
                payable_value = int(payable or 0)
                unique_value = int(unique_amount or 0)
            except TypeError, ValueError:
                return "provider_payable_amount_mismatch", False
            buyer_pays = fee_payer in {"buyer", "user"}
            expected_payable = amount + (fee if buyer_pays else 0) + unique_value
            if payable_value != expected_payable or unique_value < 0:
                return "provider_payable_amount_mismatch", False
        if invoice.get("isPaid") is not True:
            provider_status = (
                str(invoice.get("paymentStatus") or invoice.get("providerStatus") or "")
                .strip()
                .lower()
            )
            if invoice.get("isExpired") is True or provider_status == "expired":
                return "provider_expired", False
            cancelled = (
                invoice.get("isCancelled") is True
                or invoice.get("isCanceled") is True
                or provider_status in {"cancelled", "canceled"}
            )
            if cancelled:
                return "provider_cancelled", False
            if provider_status in {"failed", "rejected"}:
                return "provider_failed", False
            return "provider_not_paid", False
        return None, True

    def _resolve(
        self, order_id: PaymentOrderId, state: PaymentCreationState, error_code: str | None
    ) -> None:
        if self._payments is None:
            raise CheckoutUnavailableError("UniquePay creation store is not configured")
        self._payments.resolve_creation_attempt(
            order_id=order_id,
            state=state,
            error_code=error_code,
            resolved_at=datetime.now(UTC),
        )


__all__ = ["UNIQUEPAY", "UniquePayGateway", "uniquepay_hash_id"]
