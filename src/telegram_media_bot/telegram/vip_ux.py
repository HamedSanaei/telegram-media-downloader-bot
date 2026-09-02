"""Owner-bound /vip user experience (T023): wired into the real bot runtime.

The /vip command shows the user's subscription + Instagram state, the enabled plan catalog, and a
pending order if one exists. The purchase path is: plan -> gateway -> durable order -> provider
checkout -> check-payment callback button. Every callback payload is an opaque order/plan/gateway
reference only; nothing secret or provider-owned is ever embedded. All provider access goes
through BillingService/PaymentReconciliationService in this module; Telegram handlers never touch
provider internals.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from telegram_media_bot.application.services.entitlements import subscription_status
from telegram_media_bot.application.services.instagram_connection import (
    InstagramConnectionService,
)
from telegram_media_bot.application.services.payment_reconciliation import (
    CheckOutcome,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.bootstrap.payments import PaymentRuntime, available_providers
from telegram_media_bot.domain.errors import (
    CheckoutAlreadyStartedError,
    CheckoutUnavailableError,
)
from telegram_media_bot.domain.payments import (
    CheckoutResult,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
)
from telegram_media_bot.domain.subscriptions import (
    PlanId,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)
from telegram_media_bot.telegram.instagram_ux import render_connection_status

_PROVIDER_NAMES: dict[str, str] = {
    "uniquepay": "uniq",
    "tetraminator": "tetr",
    "hooshpay": "hoosh",
}

_STATUS_LABELS: dict[SubscriptionStatus, str] = {
    SubscriptionStatus.ACTIVE: "active",
    SubscriptionStatus.SUSPENDED: "suspended",
    SubscriptionStatus.EXPIRED: "expired",
    SubscriptionStatus.INACTIVE: "inactive",
    SubscriptionStatus.CANCELLED: "cancelled",
}


def _provider_label(provider_id: PaymentProviderId | None) -> str:
    if provider_id is None:
        return "unknown"
    return _PROVIDER_NAMES.get(str(provider_id), str(provider_id))


def render_vip_status(
    *,
    subscription: Subscription | None,
    now: datetime,
    credential_text: str,
    plans: tuple[SubscriptionPlan, ...],
    pending_order: PaymentOrder | None,
    providers: tuple[PaymentProviderId, ...],
) -> str:
    status = (
        subscription_status(subscription, now)
        if subscription is not None
        else SubscriptionStatus.INACTIVE
    )
    label = _STATUS_LABELS.get(status, status.value)
    lines = ["VIP status: " + label]
    if subscription is not None and subscription.authorized_until is not None:
        lines.append("valid until: " + str(subscription.authorized_until))
    lines.append(credential_text)
    enabled = [plan for plan in plans if plan.enabled]
    if pending_order is not None:
        lines.append(
            "pending payment: "
            + _provider_label(pending_order.provider_id)
            + " "
            + str(pending_order.amount_minor)
            + " "
            + pending_order.currency
        )
    if enabled and providers:
        lines.append(
            "active plans: "
            + ", ".join(f"{p.name} ({p.price_minor} {p.currency})" for p in enabled)
        )
    elif not providers:
        lines.append("VIP purchase is not available right now.")
    return "\n".join(lines)


def _plan_callback(plan_id: str) -> str:
    return "vip:plan:" + plan_id


def _gateway_callback(order_id: str, gateway: str) -> str:
    return "vip:gw:" + order_id + ":" + gateway


def _check_callback(order_id: str) -> str:
    return "vip:check:" + order_id


def build_vip_router(
    *,
    settings: Settings,
    payments: PaymentRuntime | None,
    subscriptions: SqliteSubscriptionRepository | None,
    connection: InstagramConnectionService | None,
) -> Router:
    router = Router(name="vip")

    @router.message(Command("vip"))
    async def vip_command(message: Message) -> None:
        if message.from_user is None or payments is None or subscriptions is None:
            await message.answer("VIP purchase is not available right now.")
            return
        owner = message.from_user.id
        now = datetime.now()
        subscription = await asyncio.to_thread(subscriptions.get_subscription, owner)
        credential = (
            await asyncio.to_thread(connection.status, owner) if connection is not None else None
        )
        pending = await asyncio.to_thread(
            payments.repository.find_pending_order_for_user, owner, now
        )
        plans = await asyncio.to_thread(subscriptions.list_plans)
        providers = available_providers(settings.payments)
        text = render_vip_status(
            subscription=subscription,
            now=now,
            credential_text=render_connection_status(credential),
            plans=plans,
            pending_order=pending,
            providers=providers,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=plan.name,
                        callback_data=_plan_callback(str(plan.plan_id)),
                    )
                ]
                for plan in plans
                if plan.enabled and providers
            ]
        )
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("vip:plan:"))
    async def vip_plan_selected(callback: CallbackQuery) -> None:
        if callback.from_user is None or payments is None or subscriptions is None:
            await callback.answer("not available", show_alert=True)
            return
        raw = callback.data or ""
        plan_id = raw.removeprefix("vip:plan:")
        plan = await asyncio.to_thread(subscriptions.get_plan, PlanId(plan_id))
        if plan is None or not plan.enabled:
            await callback.answer("invalid plan", show_alert=True)
            return
        try:
            order = await asyncio.to_thread(
                payments.billing.create_order,
                callback.from_user.id,
                plan,
                expires_at=_now_plus(30),
            )
        except Exception:
            await callback.answer("order creation failed", show_alert=True)
            return
        providers = available_providers(settings.payments)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_provider_label(provider_id),
                        callback_data=_gateway_callback(str(order.order_id), str(provider_id)),
                    )
                ]
                for provider_id in providers
            ]
        )
        if callback.message is not None and isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Plan " + plan.name + " — choose a gateway:",
                reply_markup=keyboard,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vip:gw:"))
    async def vip_gateway_selected(callback: CallbackQuery) -> None:
        if callback.from_user is None or payments is None:
            await callback.answer("not available", show_alert=True)
            return
        raw = callback.data or ""
        _prefix, _tag, order_id, gateway = raw.split(":", 3)
        provider = PaymentProviderId(gateway)
        try:
            checkout: CheckoutResult = await asyncio.to_thread(
                payments.billing.start_checkout,
                PaymentOrderId(order_id),
                provider_id=provider,
            )
        except CheckoutAlreadyStartedError:
            existing = await asyncio.to_thread(
                payments.repository.get_order, PaymentOrderId(order_id)
            )
            recovered = _recover_checkout_from_order(existing) if existing is not None else None
            if recovered is None:
                await callback.answer("order state is unknown", show_alert=True)
                return
            checkout = recovered
        except CheckoutUnavailableError:
            await callback.answer("gateway unavailable", show_alert=True)
            return
        except Exception:
            await callback.answer("checkout failed", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Pay", url=checkout.checkout_url)],
                [
                    InlineKeyboardButton(
                        text="Check payment",
                        callback_data=_check_callback(str(checkout.order_id)),
                    )
                ],
            ]
        )
        if callback.message is not None and isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Complete your purchase using the buttons below.",
                reply_markup=keyboard,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vip:check:"))
    async def vip_check_payment(callback: CallbackQuery) -> None:
        if callback.from_user is None or payments is None:
            await callback.answer("not available", show_alert=True)
            return
        raw = callback.data or ""
        order_id = PaymentOrderId(raw.removeprefix("vip:check:"))
        order = await asyncio.to_thread(payments.repository.get_order, order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("order not found", show_alert=True)
            return
        if order.status is PaymentStatus.PAID:
            await callback.answer("payment already confirmed", show_alert=True)
            return
        outcome = await asyncio.to_thread(payments.reconciliation.manual_check, order_id)
        _outcome_messages: dict[str, str] = {
            CheckOutcome.PAID: "payment confirmed",
            CheckOutcome.PENDING: "payment is still pending",
            CheckOutcome.EXPIRED: "order expired",
            CheckOutcome.CANCELLED: "order cancelled",
            CheckOutcome.FAILED: "order failed",
            CheckOutcome.REJECTED: "confirmation rejected",
            CheckOutcome.SKIPPED: "status unknown; try again later",
        }
        message = _outcome_messages[str(outcome)]
        await callback.answer(message, show_alert=True)

    return router


def _now_plus(minutes: int) -> datetime:
    from datetime import timedelta

    return datetime.now(UTC) + timedelta(minutes=minutes)


def _recover_checkout_from_order(order: PaymentOrder) -> CheckoutResult | None:
    if (
        order.provider_id is None
        or order.external_checkout_reference is None
        or order.checkout_url is None
    ):
        return None
    return CheckoutResult(
        provider_id=order.provider_id,
        order_id=order.order_id,
        external_checkout_reference=order.external_checkout_reference,
        created_at=order.created_at,
        expires_at=order.expires_at,
        checkout_url=order.checkout_url,
    )


__all__ = [
    "build_vip_router",
    "render_vip_status",
]
