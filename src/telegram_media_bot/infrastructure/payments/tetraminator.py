"""Tetraminator invoice adapter (T024).

Contract source: repository ``api_docs/tetraminator`` plus the hardened 3xui-bot verifier.
- Create: ``POST /invoice/create`` (JSON ``{price, callback_url}``, header ``X-API-KEY``).
  Amount is whole toman with a documented minimum of 50,000. Success returns
  ``{status: true, pay_id, payment_link}``. Creation is NEVER retried (no idempotency key).
- Webhook: Tetraminator sends an unsigned GET with NO body to ``callback_url`` after payment.
  It is a wake-up trigger only — payment proof requires the authoritative inquiry
  ``GET /payment/inquiry/{pay_id}``.
- Authoritative paid verdict: status==true, ``pay_id`` equals the saved provider pay ID, and
  ``amount`` equals the exact expected order amount in toman, with ``payment_status == "paid"``.
"""

from __future__ import annotations

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

TETRAMINATOR = PaymentProviderId("tetraminator")

_CREATE_PATH = "/invoice/create"
_CREATE_JSON = True
_MIN_AMOUNT_TOMAN = 50_000


class TetraminatorGateway(PaymentGateway):
    """Tetraminator adapter. ``payments`` carries the durable creation-reservation store."""

    provider_id = TETRAMINATOR

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        callback_url: str,
        timeout_seconds: float,
        inquiry_retry_count: int,
        payments: PaymentRepository | None = None,
        requester: ProviderHttpRequester | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._callback_url = callback_url
        self._timeout = timeout_seconds
        self._retries = max(0, inquiry_retry_count)
        self._payments = payments
        self._requester = requester or StdlibHttpRequester()

    def available_for_new_checkout(self) -> bool:
        return bool(self._api_key and self._callback_url)

    # -- create (exactly once, never retried) ----------------------------------

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        if not self.available_for_new_checkout():
            raise CheckoutUnavailableError("Tetraminator is not available for new checkouts")
        if self._payments is None:
            raise CheckoutUnavailableError("Tetraminator creation store is not configured")
        if order.amount_minor < _MIN_AMOUNT_TOMAN:
            raise CheckoutUnavailableError("Tetraminator amount is below the provider minimum")
        local_order = str(order.order_id)
        callback = (
            self._callback_url
            + ("&" if "?" in self._callback_url else "?")
            + ("order=" + local_order)
        )
        # Local order id only: an unsigned wake-up callback can locate the order but never
        # confirm payment.
        merchant_reference = f"tmb-tetra-{local_order}"
        self._payments.begin_creation_attempt(
            order_id=order.order_id,
            provider_id=self.provider_id,
            merchant_reference=merchant_reference,
            attempted_at=datetime.now(UTC),
        )
        try:
            response = self._requester.request(
                "POST",
                self._base_url + _CREATE_PATH,
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json_body={"price": order.amount_minor, "callback_url": callback},
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError(
                "Tetraminator could not create the invoice (ambiguous)"
            ) from None
        parsed = self._parse_create(order, response)
        if parsed is None:
            self._resolve(order.order_id, PaymentCreationState.FAILED, "create_failed")
            raise CheckoutUnavailableError("Tetraminator rejected the invoice creation")
        pay_id, payment_link = parsed
        self._resolve(order.order_id, PaymentCreationState.CREATED, None)
        self._payments.attach_checkout(
            order_id=order.order_id,
            provider_id=self.provider_id,
            external_checkout_reference=pay_id,
            checkout_url=payment_link,
            now=datetime.now(UTC),
        )
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,
            external_checkout_reference=pay_id,
            created_at=datetime.now(UTC),
            expires_at=order.expires_at,
            checkout_url=payment_link,
        )

    def _parse_create(
        self, order: PaymentOrder, response: ProviderHttpResponse
    ) -> tuple[str, str] | None:
        if response.status >= 400 and response.status < 500:
            return None
        if is_transient_status(response.status):
            self._resolve(order.order_id, PaymentCreationState.AMBIGUOUS, "create_ambiguous")
            raise CheckoutUnavailableError("Tetraminator could not create the invoice (ambiguous)")
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get("status") is not True:
            return None
        pay_id = body.get("pay_id")
        payment_link = body.get("payment_link")
        if not isinstance(pay_id, str) or not pay_id.strip():
            return None
        if not isinstance(payment_link, str) or not payment_link.strip():
            return None
        return pay_id.strip(), payment_link.strip()

    # -- authoritative inquiry (read-only, bounded retries) ---------------------

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        pay_id = (
            provider_transaction_reference
            if provider_transaction_reference is not None
            else order.external_checkout_reference
        )
        if not pay_id:
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
                "GET",
                self._base_url + "/payment/inquiry/" + str(pay_id),
                headers={"X-API-KEY": self._api_key},
                timeout_seconds=self._timeout,
                retry_count=self._retries,
            )
        except ProviderHttpRequestError:
            return VerifiedPaymentResult(
                provider_id=self.provider_id,
                provider_transaction_reference=ProviderTransactionReference(str(pay_id)),
                order_reference=str(order.order_id),
                amount_minor=order.amount_minor,
                currency="IRT",
                status=PaymentStatus.PENDING,
                failure_code="inquiry_unavailable",
            )
        error, paid = self._verify_inquiry(order, str(pay_id), response)
        status = PaymentStatus.PAID if paid else PaymentStatus.PENDING
        if error in {"provider_expired", "provider_cancelled", "provider_failed"}:
            status = PaymentStatus(error[9:])
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference(str(pay_id)),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency="IRT",
            status=status,
            failure_code=error,
        )

    def _verify_inquiry(
        self, order: PaymentOrder, pay_id: str, response: ProviderHttpResponse
    ) -> tuple[str | None, bool]:
        """Fail-closed: paid only when status, pay-id identity, exact toman amount, and paid
        status all match the local order snapshot. Callback query values never participate."""
        if response.status >= 400 or is_transient_status(response.status):
            return "provider_inquiry_unsuccessful", False
        try:
            body = response.json()
        except ValueError:
            return "provider_inquiry_unsuccessful", False
        if not isinstance(body, dict) or body.get("status") is not True:
            return "provider_inquiry_unsuccessful", False
        returned_pay_id = body.get("pay_id")
        if not isinstance(returned_pay_id, str) or returned_pay_id.strip() != pay_id:
            return "provider_pay_id_mismatch", False
        try:
            amount = int(body["amount"])
        except KeyError, TypeError, ValueError:
            return "provider_amount_invalid", False
        if amount != order.amount_minor:
            return "provider_amount_mismatch", False
        payment_status = str(body.get("payment_status", "")).strip().lower()
        if payment_status not in {
            "paid",
            "pending",
            "waiting",
            "unpaid",
            "expired",
            "cancelled",
            "failed",
            "rejected",
        }:
            return "provider_status_unknown", False
        if payment_status == "paid":
            return None, True
        if payment_status == "expired":
            return "provider_expired", False
        if payment_status in {"cancelled", "canceled"}:
            return "provider_cancelled", False
        if payment_status in {"failed", "rejected"}:
            return "provider_failed", False
        return "provider_not_paid", False

    def _resolve(
        self, order_id: PaymentOrderId, state: PaymentCreationState, error_code: str | None
    ) -> None:
        if self._payments is None:
            raise CheckoutUnavailableError("Tetraminator creation store is not configured")
        self._payments.resolve_creation_attempt(
            order_id=order_id,
            state=state,
            error_code=error_code,
            resolved_at=datetime.now(UTC),
        )


__all__ = ["TETRAMINATOR", "TetraminatorGateway"]
