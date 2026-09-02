"""Payment runtime composition (T024/T025).

One factory builds the billing graph (repository + BillingService + three registered rial gateway
adapters + reconciliation service + audit logger) shared by the bot process, the worker process,
and the least-privilege companion process. Provider selection lives HERE (a registry), never in
domain/application code.

Availability rule (T025 section 13): a provider is available for NEW checkout only when
``payments.enabled`` AND ``provider.enabled`` AND the adapter has all required credentials/URLs.
Provider adapters stay REGISTERED while credentials exist so pending orders remain queryable and
confirmable even with new checkout disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telegram_media_bot.application.ports.payments import PaymentGateway
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.billing import BillingService, UtcClock
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.application.services.payment_reconciliation import (
    PaymentReconciliationService,
)
from telegram_media_bot.bootstrap.config import (
    HooshPaySection,
    PaymentsSection,
    TetraminatorSection,
    UniquePaySection,
)
from telegram_media_bot.domain.payments import PaymentProviderId
from telegram_media_bot.infrastructure.payments.base import ProviderHttpRequester
from telegram_media_bot.infrastructure.payments.hooshpay import HooshPayGateway
from telegram_media_bot.infrastructure.payments.tetraminator import TetraminatorGateway
from telegram_media_bot.infrastructure.payments.uniquepay import UniquePayGateway
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository

DEFAULT_UNIQUEPAY_BASE_URL = "https://uniquepay.top"
DEFAULT_TETRAMINATOR_BASE_URL = "https://api.tetraminator.com/v1"
DEFAULT_HOOSHPAY_BASE_URL = "https://pay.hooshnet.com"

#: Stable bounded provider identities (row of the provider registry).
UNIQUEPAY_ID = PaymentProviderId("uniquepay")
TETRAMINATOR_ID = PaymentProviderId("tetraminator")
HOOSHPAY_ID = PaymentProviderId("hooshpay")


@dataclass(frozen=True, slots=True)
class PaymentRuntime:
    repository: SqlitePaymentRepository
    billing: BillingService
    gateways: dict[PaymentProviderId, PaymentGateway]
    reconciliation: PaymentReconciliationService
    logger: PaymentAuditLogger | None


def build_payment_runtime(
    *,
    payments: PaymentsSection,
    database_path: Path,
    audit: AuditService | None = None,
    payment_events_enabled: bool = True,
    requester: ProviderHttpRequester | None = None,
) -> PaymentRuntime | None:
    """Compose the payment graph; returns ``None`` when no provider credential exists.

    ``payment_events_enabled`` mirrors ``telegram.logger.payment_events_enabled``; it is the
    independent VAT/operator switch for purchase/admin payment events and is never coupled to
    submission mirroring. ``logger.enabled`` (the caller decides whether ``audit`` exists)
    remains the master kill switch.
    """
    repository = SqlitePaymentRepository(database_path)
    repository.initialize()
    billing = BillingService(payments=repository, clock=UtcClock())
    gateways: dict[PaymentProviderId, PaymentGateway] = {}
    _register_uniquepay(payments.uniquepay, repository, gateways, requester)
    _register_tetraminator(payments.tetraminator, repository, gateways, requester)
    _register_hooshpay(payments.hooshpay, repository, gateways, requester)
    if not gateways:
        return None
    logger = (
        PaymentAuditLogger(audit, payment_events_enabled=payment_events_enabled)
        if audit is not None
        else None
    )
    reconciliation = PaymentReconciliationService(
        billing=billing,
        payments=repository,
        gateways=gateways,
        max_query_attempts=payments.reconciliation.max_query_attempts,
        payment_logger=logger,
    )
    return PaymentRuntime(
        repository=repository,
        billing=billing,
        gateways=gateways,
        reconciliation=reconciliation,
        logger=logger,
    )


def available_providers(payments: PaymentsSection) -> tuple[PaymentProviderId, ...]:
    """Providers allowed to open a NEW checkout right now (master + provider + credentials)."""
    if not payments.enabled:
        return ()
    available: list[PaymentProviderId] = []
    if payments.uniquepay.enabled and _uniquepay_ready(payments.uniquepay):
        available.append(UNIQUEPAY_ID)
    if payments.tetraminator.enabled and _tetraminator_ready(payments.tetraminator):
        available.append(TETRAMINATOR_ID)
    if payments.hooshpay.enabled and _hooshpay_ready(payments.hooshpay):
        available.append(HOOSHPAY_ID)
    return tuple(available)


def _register_uniquepay(
    section: UniquePaySection,
    repository: SqlitePaymentRepository,
    gateways: dict[PaymentProviderId, PaymentGateway],
    requester: ProviderHttpRequester | None,
) -> None:
    token = section.business_token
    callback = section.callback_url
    return_url = section.return_url
    if token is None or not token.get_secret_value() or not callback or not return_url:
        return
    gateways[UNIQUEPAY_ID] = UniquePayGateway(
        base_url=section.effective_base_url() or DEFAULT_UNIQUEPAY_BASE_URL,
        business_token=token.get_secret_value(),
        callback_url=callback,
        return_url=return_url,
        timeout_seconds=section.request_timeout_seconds,
        inquiry_retry_count=section.inquiry_retry_count,
        payments=repository,
        requester=requester,
    )


def _register_tetraminator(
    section: TetraminatorSection,
    repository: SqlitePaymentRepository,
    gateways: dict[PaymentProviderId, PaymentGateway],
    requester: ProviderHttpRequester | None,
) -> None:
    api_key = section.api_key
    callback = section.callback_url
    if api_key is None or not api_key.get_secret_value() or not callback:
        return
    gateways[TETRAMINATOR_ID] = TetraminatorGateway(
        base_url=section.effective_base_url() or DEFAULT_TETRAMINATOR_BASE_URL,
        api_key=api_key.get_secret_value(),
        callback_url=callback,
        timeout_seconds=section.request_timeout_seconds,
        inquiry_retry_count=section.inquiry_retry_count,
        payments=repository,
        requester=requester,
    )


def _register_hooshpay(
    section: HooshPaySection,
    repository: SqlitePaymentRepository,
    gateways: dict[PaymentProviderId, PaymentGateway],
    requester: ProviderHttpRequester | None,
) -> None:
    api_key = section.api_key
    callback = section.callback_url
    return_url = section.return_url
    if api_key is None or not api_key.get_secret_value() or not callback or not return_url:
        return
    gateways[HOOSHPAY_ID] = HooshPayGateway(
        base_url=section.effective_base_url() or DEFAULT_HOOSHPAY_BASE_URL,
        api_key=api_key.get_secret_value(),
        callback_url=callback,
        return_url=return_url,
        timeout_seconds=section.request_timeout_seconds,
        inquiry_retry_count=section.inquiry_retry_count,
        payments=repository,
        requester=requester,
    )


def _uniquepay_ready(section: UniquePaySection) -> bool:
    token = section.business_token
    return bool(
        section.enabled
        and token
        and token.get_secret_value()
        and section.callback_url
        and section.return_url
    )


def _tetraminator_ready(section: TetraminatorSection) -> bool:
    api_key = section.api_key
    return bool(section.enabled and api_key and api_key.get_secret_value() and section.callback_url)


def _hooshpay_ready(section: HooshPaySection) -> bool:
    api_key = section.api_key
    ipn = section.ipn_secret_key
    return bool(
        section.enabled
        and api_key
        and api_key.get_secret_value()
        and ipn
        and ipn.get_secret_value()
        and section.callback_url
        and section.return_url
    )


__all__ = [
    "HOOSHPAY_ID",
    "TETRAMINATOR_ID",
    "UNIQUEPAY_ID",
    "PaymentRuntime",
    "available_providers",
    "build_payment_runtime",
]
