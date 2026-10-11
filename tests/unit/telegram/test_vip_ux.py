from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from pydantic import SecretStr

from telegram_media_bot.application.services.billing import BillingService, UtcClock
from telegram_media_bot.application.services.payment_reconciliation import (
    PaymentReconciliationService,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.bootstrap.payments import UNIQUEPAY_ID, PaymentRuntime
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentCreationState,
    PaymentOrder,
    PaymentStatus,
    ProviderTransactionReference,
    VerifiedPaymentResult,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    GrantId,
    PlanId,
    Subscription,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)
from telegram_media_bot.telegram.user_menu import (
    BUY_VIP_BUTTON,
    SUBSCRIPTION_BUTTON,
    UserDownloadState,
)
from telegram_media_bot.telegram.vip_ux import build_vip_router

OWNER = 21
OTHER = 22
PLAN = SubscriptionPlan(
    plan_id=PlanId("configured-vip"),
    name="Configured VIP",
    duration_months=2,
    price_minor=250_000,
    currency="IRT",
    enabled=True,
    capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
)


class DeterministicGateway:
    provider_id = UNIQUEPAY_ID

    def __init__(self, repository: SqlitePaymentRepository) -> None:
        self.repository = repository
        self.creations: list[PaymentOrder] = []
        self.queries = 0

    def available_for_new_checkout(self) -> bool:
        return True

    def create_payment(self, order: PaymentOrder) -> CheckoutResult:
        now = datetime.now(UTC)
        self.repository.begin_creation_attempt(
            order_id=order.order_id,
            provider_id=self.provider_id,
            merchant_reference=str(order.order_id),
            attempted_at=now,
        )
        self.creations.append(order)
        self.repository.attach_checkout(
            order_id=order.order_id,
            provider_id=self.provider_id,
            external_checkout_reference="checkout-1",
            checkout_url="https://pay.example.test/checkout/1",
            now=now,
        )
        self.repository.resolve_creation_attempt(
            order_id=order.order_id,
            state=PaymentCreationState.CREATED,
            error_code=None,
            resolved_at=now,
        )
        return CheckoutResult(
            provider_id=self.provider_id,
            order_id=order.order_id,
            external_checkout_reference="checkout-1",
            created_at=now,
            expires_at=order.expires_at,
            checkout_url="https://pay.example.test/checkout/1",
        )

    def query_payment(
        self,
        order: PaymentOrder,
        provider_transaction_reference: ProviderTransactionReference | None,
    ) -> VerifiedPaymentResult:
        self.queries += 1
        return VerifiedPaymentResult(
            provider_id=self.provider_id,
            provider_transaction_reference=ProviderTransactionReference("settlement-1"),
            order_reference=str(order.order_id),
            amount_minor=order.amount_minor,
            currency=order.currency,
            status=PaymentStatus.PAID,
            failure_code=None,
        )


class Dashboard:
    def __init__(
        self,
        settings: Settings,
        path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        payments_present: bool = True,
        has_plan: bool = True,
    ) -> None:
        self.subscriptions = SqliteSubscriptionRepository(path)
        self.subscriptions.initialize()
        if has_plan:
            self.subscriptions.save_plan(PLAN)
        self.repository = SqlitePaymentRepository(path)
        self.repository.initialize()
        self.gateway = DeterministicGateway(self.repository)
        billing = BillingService(payments=self.repository, clock=UtcClock())
        billing.register_gateway(self.gateway)
        reconciliation = PaymentReconciliationService(
            billing=billing,
            payments=self.repository,
            gateways={UNIQUEPAY_ID: self.gateway},
            max_query_attempts=3,
        )
        self.runtime = PaymentRuntime(
            repository=self.repository,
            billing=billing,
            gateways={UNIQUEPAY_ID: self.gateway},
            reconciliation=reconciliation,
            logger=None,
        )
        self.bot = Bot(settings.telegram.bot_token.get_secret_value())
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(
            build_vip_router(
                settings=settings,
                payments=self.runtime if payments_present else None,
                subscriptions=self.subscriptions,
                connection=None,
            )
        )
        self.calls: list[TelegramMethod[Any]] = []
        self.update_id = 0

        async def telegram_request(bot: Bot, method: TelegramMethod[Any], **kwargs: Any) -> Any:
            self.calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            return Message(
                message_id=100,
                date=datetime.now(UTC),
                chat=Chat(id=OWNER, type="private"),
                text=getattr(method, "text", ""),
            )

        monkeypatch.setattr(Bot, "__call__", telegram_request)

    def state(self, owner: int = OWNER) -> FSMContext:
        return self.dispatcher.fsm.get_context(bot=self.bot, chat_id=owner, user_id=owner)

    async def message(self, text: str, owner: int = OWNER) -> SendMessage:
        self.update_id += 1
        update = Update(
            update_id=self.update_id,
            message=Message(
                message_id=self.update_id,
                date=datetime.now(UTC),
                chat=Chat(id=owner, type="private"),
                from_user=User(id=owner, is_bot=False, first_name="User", is_premium=True),
                text=text,
            ),
        )
        await self.dispatcher.feed_update(self.bot, update)
        result = self.calls[-1]
        assert isinstance(result, SendMessage)
        return result

    async def callback(self, data: str, owner: int = OWNER) -> AnswerCallbackQuery:
        self.update_id += 1
        update = Update(
            update_id=self.update_id,
            callback_query=CallbackQuery(
                id=str(self.update_id),
                from_user=User(id=owner, is_bot=False, first_name="User"),
                chat_instance="test-chat",
                data=data,
                message=Message(
                    message_id=100,
                    date=datetime.now(UTC),
                    chat=Chat(id=owner, type="private"),
                    text="Dashboard",
                ),
            ),
        )
        await self.dispatcher.feed_update(self.bot, update)
        result = self.calls[-1]
        assert isinstance(result, AnswerCallbackQuery)
        return result

    def callback_data(self, prefix: str) -> str:
        for method in reversed(self.calls):
            if isinstance(method, (SendMessage, EditMessageText)):
                keyboard = method.reply_markup
                if isinstance(keyboard, InlineKeyboardMarkup):
                    for row in keyboard.inline_keyboard:
                        for button in row:
                            if button.callback_data and button.callback_data.startswith(prefix):
                                return button.callback_data
        raise AssertionError(f"No callback with prefix {prefix}")


def enabled_settings(settings: Settings) -> Settings:
    provider = settings.payments.uniquepay.model_copy(
        update={
            "enabled": True,
            "business_token": SecretStr("configured-test-credential"),
            "callback_url": "https://companion.example.test/payments/uniquepay",
            "return_url": "https://companion.example.test/return",
        }
    )
    return settings.model_copy(
        update={
            "payments": settings.payments.model_copy(
                update={"enabled": True, "uniquepay": provider}
            )
        }
    )


@pytest.mark.parametrize("button", [SUBSCRIPTION_BUTTON, BUY_VIP_BUTTON, "/vip"])
@pytest.mark.parametrize(
    ("status", "expected"),
    [("free", "غیرفعال"), ("active", "فعال"), ("expired", "منقضی‌شده"), ("cancelled", "لغوشده")],
)
async def test_menu_dashboard_reports_status_without_billing_and_clears_private_mode(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    button: str,
    status: str,
    expected: str,
) -> None:
    dashboard = Dashboard(settings, tmp_path / "state.sqlite3", monkeypatch, payments_present=False)
    now = datetime.now(UTC)
    if status != "free":
        dashboard.subscriptions.create_grant_with_subscription(
            EntitlementGrant(
                grant_id=GrantId("grant-1"),
                user_id=OWNER,
                plan_id=PLAN.plan_id,
                duration_months=2,
                confirmed_at=now - timedelta(days=70),
                source_type="test",
                source_reference="paid-order-1",
                created_at=now,
            ),
            Subscription(
                user_id=OWNER,
                authorized_until=now + timedelta(days=3)
                if status == "active"
                else now - timedelta(days=3),
                cancelled_at=now if status == "cancelled" else None,
                updated_at=now,
            ),
        )
    state = dashboard.state()
    await state.set_state(UserDownloadState.private_url)
    await state.set_data({"old_private_authorization": "stale"})

    answer = await dashboard.message(button)

    assert f"وضعیت VIP: {expected}" in answer.text
    if status == "active":
        assert "نوع اشتراک: Premium Downloader / VIP" in answer.text
    else:
        assert "نوع اشتراک: Free" in answer.text
    assert "Telegram Premium اشتراک VIP این ربات نیست" in answer.text
    assert "رسانهٔ عمومی" in answer.text
    assert "اتصال شخصی" in answer.text
    assert "مدیر ربات" in answer.text
    assert answer.reply_markup is None
    assert await state.get_state() is None
    assert "old_private_authorization" not in await state.get_data()
    assert dashboard.repository.list_orders_by_user(OWNER) == ()


@pytest.mark.parametrize("unavailable", ["disabled", "no-provider", "no-plan", "disabled-plan"])
async def test_unavailable_purchase_never_creates_an_order(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    if unavailable == "disabled":
        configured = settings
    elif unavailable == "no-provider":
        configured = settings.model_copy(
            update={"payments": settings.payments.model_copy(update={"enabled": True})}
        )
    else:
        configured = enabled_settings(settings)
    dashboard = Dashboard(
        configured,
        tmp_path / "state.sqlite3",
        monkeypatch,
        has_plan=unavailable != "no-plan",
    )
    if unavailable == "disabled-plan":
        dashboard.subscriptions.save_plan(replace(PLAN, enabled=False))

    answer = await dashboard.message(BUY_VIP_BUTTON)
    denial = await dashboard.callback("vip:plan:configured-vip")

    assert "خرید VIP در حال حاضر در دسترس نیست" in answer.text
    assert answer.reply_markup is None
    assert denial.show_alert is True
    assert dashboard.repository.list_orders_by_user(OWNER) == ()
    assert dashboard.gateway.creations == []


async def test_configured_checkout_is_owner_bound_and_replay_reuses_one_payment(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = Dashboard(enabled_settings(settings), tmp_path / "state.sqlite3", monkeypatch)
    answer = await dashboard.message(BUY_VIP_BUTTON)
    assert PLAN.name in answer.text
    assert str(PLAN.price_minor) in answer.text
    plan_callback = dashboard.callback_data("vip:plan:")
    assert len(plan_callback.encode()) <= 64
    denial = await dashboard.callback(plan_callback, OTHER)
    assert denial.show_alert is True
    assert dashboard.repository.list_orders_by_user(OTHER) == ()
    assert dashboard.repository.list_orders_by_user(OWNER) == ()

    await asyncio.gather(dashboard.callback(plan_callback), dashboard.callback(plan_callback))
    orders = dashboard.repository.list_orders_by_user(OWNER)
    assert len(orders) == 1
    assert orders[0].user_id == OWNER
    assert orders[0].amount_minor == PLAN.price_minor
    gateway_callback = dashboard.callback_data("vip:gw:")
    original = dashboard.repository.get_order(orders[0].order_id)

    denial = await dashboard.callback(gateway_callback, OTHER)
    assert denial.show_alert is True
    assert dashboard.repository.get_order(orders[0].order_id) == original
    assert dashboard.gateway.creations == []
    await dashboard.callback(gateway_callback)
    await dashboard.callback(gateway_callback)

    assert len(dashboard.gateway.creations) == 1
    assert dashboard.gateway.creations[0].user_id == OWNER
    checkout_edits = [method for method in dashboard.calls if isinstance(method, EditMessageText)]
    assert checkout_edits[-1].reply_markup is not None
    assert (
        checkout_edits[-1].reply_markup.inline_keyboard[0][0].url
        == "https://pay.example.test/checkout/1"
    )
    assert dashboard.subscriptions.get_grants(OWNER) == ()
    check_callback = dashboard.callback_data("vip:check:")
    denial = await dashboard.callback(check_callback, OTHER)
    assert denial.show_alert is True
    assert dashboard.gateway.queries == 0
    assert dashboard.subscriptions.get_grants(OWNER) == ()

    await dashboard.callback(check_callback)
    await dashboard.callback(check_callback)
    assert len(dashboard.subscriptions.get_grants(OWNER)) == 1
    assert dashboard.gateway.queries == 1
    assert len(dashboard.repository.list_orders_by_user(OWNER)) == 1


async def test_expired_offer_and_order_cannot_open_checkout(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = Dashboard(enabled_settings(settings), tmp_path / "state.sqlite3", monkeypatch)
    await dashboard.message(BUY_VIP_BUTTON)
    plan_callback = dashboard.callback_data("vip:plan:")
    state = dashboard.state()
    offers = cast(dict[str, dict[str, str | float]], (await state.get_data())["vip_offers"])
    offers[plan_callback.removeprefix("vip:plan:")]["expires_at"] = 0.0
    await state.update_data(vip_offers=offers)
    denial = await dashboard.callback(plan_callback)
    assert denial.show_alert is True
    assert dashboard.repository.list_orders_by_user(OWNER) == ()

    await dashboard.message(BUY_VIP_BUTTON)
    await dashboard.callback(dashboard.callback_data("vip:plan:"))
    gateway_callback = dashboard.callback_data("vip:gw:")
    order = dashboard.repository.list_orders_by_user(OWNER)[0]
    expired = replace(order, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    dashboard.repository.save_order(expired)
    denial = await dashboard.callback(gateway_callback)
    assert denial.show_alert is True
    assert "منقضی" in (denial.text or "")
    assert dashboard.repository.get_order(order.order_id) == expired
    assert dashboard.gateway.creations == []


async def test_dashboard_recovers_pending_link_when_new_purchases_disabled(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = enabled_settings(settings)
    dashboard = Dashboard(configured, tmp_path / "state.sqlite3", monkeypatch)
    await dashboard.message(BUY_VIP_BUTTON)
    await dashboard.callback(dashboard.callback_data("vip:plan:"))
    await dashboard.callback(dashboard.callback_data("vip:gw:"))
    disabled_settings = configured.model_copy(
        update={"payments": configured.payments.model_copy(update={"enabled": False})}
    )
    disabled = Dashboard(disabled_settings, tmp_path / "state.sqlite3", monkeypatch)

    answer = await disabled.message(SUBSCRIPTION_BUTTON)

    assert "خرید VIP در حال حاضر در دسترس نیست" in answer.text
    assert "پرداخت در انتظار" in answer.text
    assert isinstance(answer.reply_markup, InlineKeyboardMarkup)
    assert answer.reply_markup.inline_keyboard[0][0].url == "https://pay.example.test/checkout/1"
    assert len(dashboard.gateway.creations) == 1
    assert len(dashboard.repository.list_orders_by_user(OWNER)) == 1
    assert disabled.gateway.creations == []


async def test_cancelled_checkout_and_consumed_plan_cannot_be_replayed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = Dashboard(enabled_settings(settings), tmp_path / "state.sqlite3", monkeypatch)
    await dashboard.message(BUY_VIP_BUTTON)
    plan_callback = dashboard.callback_data("vip:plan:")
    await dashboard.callback(plan_callback)
    gateway_callback = dashboard.callback_data("vip:gw:")
    order = dashboard.repository.list_orders_by_user(OWNER)[0]
    cancelled = dashboard.runtime.billing.cancel_order(order.order_id)

    denial = await dashboard.callback(gateway_callback)
    assert denial.show_alert is True
    denial = await dashboard.callback(plan_callback)
    assert denial.show_alert is True
    assert dashboard.repository.get_order(order.order_id) == cancelled
    assert len(dashboard.repository.list_orders_by_user(OWNER)) == 1
    assert dashboard.gateway.creations == []
