from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.bootstrap.config import PaymentsSection
from telegram_media_bot.bootstrap.payments import UNIQUEPAY_ID, build_payment_runtime
from telegram_media_bot.domain.payments import PaymentCreationState, PaymentStatus
from telegram_media_bot.domain.subscriptions import Capability, PlanId, SubscriptionPlan
from telegram_media_bot.infrastructure.payments.base import ProviderHttpResponse
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)


class InvoiceTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        json_body: object | None = None,
        timeout_seconds: float,
    ) -> ProviderHttpResponse:
        if method != "POST" or not url.endswith("/api/ddbot/create-invoice") or form is None:
            raise AssertionError("Unexpected provider operation")
        return ProviderHttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "status": True,
                    "code": 200,
                    "hashId": form["hashId"],
                    "refId": "configured-checkout",
                    "paymentLink": "https://pay.example.test/configured-checkout",
                }
            ).encode("utf-8"),
        )


def test_configured_runtime_opens_checkout_without_granting_vip(tmp_path: Path) -> None:
    config = PaymentsSection.model_validate(
        {
            "enabled": True,
            "uniquepay": {
                "enabled": True,
                "business_token": "isolated-test-token",
                "callback_url": "https://connect.example.test/payment/callback/uniquepay",
                "return_url": "https://connect.example.test/return",
            },
        }
    )
    database = tmp_path / "billing.sqlite3"
    subscriptions = SqliteSubscriptionRepository(database)
    subscriptions.initialize()
    plan = SubscriptionPlan(
        plan_id=PlanId("configured-vip"),
        name="Configured VIP",
        duration_months=1,
        price_minor=250_000,
        currency="IRT",
        enabled=True,
        capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
    )
    subscriptions.save_plan(plan)
    runtime = build_payment_runtime(
        payments=config, database_path=database, requester=InvoiceTransport()
    )
    assert runtime is not None
    order = runtime.billing.create_order(
        7, plan, provider_id=UNIQUEPAY_ID, expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )
    checkout = runtime.billing.start_checkout(order.order_id, provider_id=UNIQUEPAY_ID)
    loaded = runtime.repository.get_order(order.order_id)
    reservation = runtime.repository.get_creation_reservation(order.order_id)
    assert checkout.checkout_url == "https://pay.example.test/configured-checkout"
    assert loaded is not None and loaded.status is PaymentStatus.PENDING
    assert loaded.user_id == 7 and loaded.checkout_url == checkout.checkout_url
    assert reservation is not None and reservation.state is PaymentCreationState.CREATED
    assert subscriptions.get_grants(7) == ()
    assert subscriptions.get_subscription(7) is None
