"""Companion-side payment callback processing (T025).

Implements the ``PaymentCallbackProcessor`` port: a normalized trigger is followed by a
point-in-time authoritative provider query and settlement ONLY through
``PaymentReconciliationService`` (itself BillingService-mediated). The trigger itself never
confirms anything; unsigned wake-ups and replaying signed IPNs are equally harmless because
exactly-once economic identity lives in the provider-transaction claim inside the atomic
confirmation.

No provider secret, callback body, or signature is ever logged here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from telegram_media_bot.application.ports.companion import (
    PaymentCallbackProcessor,
    PaymentCallbackTrigger,
)
from telegram_media_bot.application.ports.payments import PaymentRepository
from telegram_media_bot.application.services.payment_reconciliation import (
    CheckOutcome,
    PaymentReconciliationService,
)
from telegram_media_bot.domain.payments import PaymentOrderId
from telegram_media_bot.domain.web_companion import PaymentCallbackOutcome


class CompanionPaymentCallbackProcessor(PaymentCallbackProcessor):
    """Trigger -> authoritative query -> atomic settle (bounded, owner-checking at the web layer
    cannot apply here; the order itself binds provider identity)."""

    def __init__(
        self,
        *,
        reconciliation: PaymentReconciliationService,
        payments: PaymentRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Purchase auditing is owned by PaymentReconciliationService (emit-after-commit, idempotent);
        # this processor never touches the Logger.
        self._reconciliation = reconciliation
        self._payments = payments
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(self, *, trigger: PaymentCallbackTrigger) -> PaymentCallbackOutcome:
        if not trigger.authentic:
            return PaymentCallbackOutcome.REJECTED
        if trigger.order_reference is None:
            # Generic 404: never reveal whether an arbitrary order id exists.
            return PaymentCallbackOutcome.NOT_AVAILABLE
        order = await asyncio.to_thread(
            self._payments.get_order, PaymentOrderId(trigger.order_reference)
        )
        if order is None:
            return PaymentCallbackOutcome.NOT_AVAILABLE
        if str(order.provider_id) != trigger.provider_id:
            return PaymentCallbackOutcome.REJECTED
        outcome = await asyncio.to_thread(self._reconciliation.check_order, order.order_id)
        if outcome in {
            CheckOutcome.PAID,
            CheckOutcome.PENDING,
            CheckOutcome.EXPIRED,
            CheckOutcome.CANCELLED,
            CheckOutcome.FAILED,
        }:
            return PaymentCallbackOutcome.ACCEPTED
        if outcome is CheckOutcome.REJECTED:
            return PaymentCallbackOutcome.REJECTED
        return PaymentCallbackOutcome.NOT_AVAILABLE


__all__ = ["CompanionPaymentCallbackProcessor"]
