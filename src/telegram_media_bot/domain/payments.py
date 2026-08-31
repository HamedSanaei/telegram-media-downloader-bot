"""Provider-neutral payment/order domain (T015).

T015 introduces the durable payment foundation that a future real gateway (T024) and the Telegram
purchasing UX (T023) will call. It deliberately and only models orders, attempts, statuses, and the
verified-result boundary. It contains no real provider, no HTTP/callback route, no card/credential
storage, and no browser-redirect confirmation. A browser redirect may display a status; it can
never activate an entitlement.

Billing owns economic/payment facts here. Entitlement access grants remain authoritative in the
T014 subscription domain (``domain/subscriptions.py``); T015 reuses those grants and the
calendar-month recomputation rules rather than duplicating them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

from telegram_media_bot.domain.errors import InvalidPaymentTransitionError
from telegram_media_bot.domain.subscriptions import Capability, PlanId

PaymentOrderId = NewType("PaymentOrderId", str)
PaymentAttemptId = NewType("PaymentAttemptId", str)
PaymentProviderId = NewType("PaymentProviderId", str)
ProviderTransactionReference = NewType("ProviderTransactionReference", str)


class PaymentStatus(StrEnum):
    """Stable, typed payment/order lifecycle status."""

    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


#: Explicit allowed transitions. Terminal states are fixed financial history and must never be
#: silently rewritten: PAID -> PENDING, REFUNDED -> PAID, EXPIRED -> PAID and CANCELLED -> PAID
#: are all forbidden unless a future ADR explicitly changes the business rule.
_ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(
        {
            PaymentStatus.PENDING,
            PaymentStatus.PAID,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.PENDING: frozenset(
        {PaymentStatus.PAID, PaymentStatus.FAILED, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED}
    ),
    PaymentStatus.PAID: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}


def payment_status_transition(current: PaymentStatus, target: PaymentStatus) -> PaymentStatus:
    """Validate an order status transition, returning the target or raising a typed denial."""
    if current is target:
        return target
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidPaymentTransitionError(
            f"Cannot transition payment order from {current.value} to {target.value}"
        )
    return target


@dataclass(frozen=True, slots=True)
class PaymentOrder:
    """An immutable commercial-facts snapshot created once when the order is opened.

    The order snapshots the plan's duration, capabilities, amount (integer minor units), and
    currency at creation time. Payment confirmation compares the provider-verified result against
    this SNAPSHOT, never against the mutable plan catalog. A later operator price change cannot
    alter an already-created order.
    """

    order_id: PaymentOrderId
    user_id: int
    plan_id: PlanId
    duration_months: int
    capabilities: frozenset[Capability]
    amount_minor: int
    currency: str
    created_at: datetime
    expires_at: datetime
    status: PaymentStatus
    provider_id: PaymentProviderId | None = None

    def __post_init__(self) -> None:
        if isinstance(self.duration_months, bool) or not isinstance(self.duration_months, int):
            raise TypeError("duration_months must be a positive integer")
        if self.duration_months <= 0:
            raise ValueError("duration_months must be a positive integer")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer in minor units")
        if self.amount_minor < 0:
            raise ValueError("amount_minor cannot be negative")
        currency = str(self.currency).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter alphabetic code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True, slots=True)
class PaymentAttempt:
    """A durable audit row for one provider interaction.

    Persists attempt identity, order identity, provider, normalized status, timestamps, and a safe
    failure category/code only. It NEVER persists raw callback bodies, headers, signatures,
    redirect queries, card/CVV data, or provider secrets.
    """

    attempt_id: PaymentAttemptId
    order_id: PaymentOrderId
    provider_id: PaymentProviderId | None
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Safe, provider-neutral result of opening an external checkout.

    Contains provider identity, an opaque external reference/token, and the order identity. It must
    not be treated as payment proof and may carry no provider secret. A redirect/return URL is
    presentation state only; it never confirms payment.
    """

    provider_id: PaymentProviderId
    order_id: PaymentOrderId
    external_checkout_reference: str
    created_at: datetime
    expires_at: datetime

    @property
    def redirect_only(self) -> bool:
        """Checkout creation alone is a redirect, never a confirmation of payment."""
        return True


@dataclass(frozen=True, slots=True)
class VerifiedPaymentResult:
    """The ONLY currency accepted by ``BillingService`` from a provider.

    Produced by a gateway adapter after it verifies signature/authenticity and freshness. It is a
    normalized, project-owned value containing the provider reference, merchant/order reference,
    verified amount/currency, and normalized status. ``BillingService`` rejects any raw provider
    callback message; it accepts only this verified result.
    """

    provider_id: PaymentProviderId
    provider_transaction_reference: ProviderTransactionReference
    order_reference: str
    amount_minor: int
    currency: str
    status: PaymentStatus
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_transaction_reference, str):
            raise TypeError("provider_transaction_reference must be a string")
        if not self.provider_transaction_reference:
            raise ValueError("provider_transaction_reference cannot be empty")
        if not self.order_reference:
            raise ValueError("order_reference cannot be empty")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer in minor units")
        if self.amount_minor < 0:
            raise ValueError("amount_minor cannot be negative")
        currency = str(self.currency).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter alphabetic code")
        object.__setattr__(self, "currency", currency)
