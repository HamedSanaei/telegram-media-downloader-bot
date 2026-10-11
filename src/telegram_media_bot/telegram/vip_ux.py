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
import secrets
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
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
    CredentialError,
    PersistenceError,
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
from telegram_media_bot.telegram.user_menu import BUY_VIP_BUTTON, SUBSCRIPTION_BUTTON

_PROVIDER_NAMES: dict[str, str] = {
    "uniquepay": "uniq",
    "tetraminator": "tetr",
    "hooshpay": "hoosh",
}

_STATUS_LABELS: dict[SubscriptionStatus, str] = {
    SubscriptionStatus.ACTIVE: "فعال",
    SubscriptionStatus.SUSPENDED: "تعلیق‌شده",
    SubscriptionStatus.EXPIRED: "منقضی‌شده",
    SubscriptionStatus.INACTIVE: "غیرفعال",
    SubscriptionStatus.CANCELLED: "لغوشده",
}

_PURCHASE_UNAVAILABLE = (
    "خرید VIP در حال حاضر در دسترس نیست؛ برای فعال‌سازی خرید با مدیر ربات تماس بگیرید "
    "یا بعداً از «خرید Premium Downloader / VIP» دوباره تلاش کنید."
)
_OFFER_EXPIRED = (
    "انتخاب نامعتبر یا منقضی است؛ دوباره از «خرید Premium Downloader / VIP» استفاده کنید."
)


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
    status = subscription_status(subscription, now)
    label = _STATUS_LABELS[status]
    account_type = (
        "Premium Downloader / VIP" if status is SubscriptionStatus.ACTIVE else "Free (رایگان)"
    )
    lines = [f"نوع اشتراک: {account_type}", f"وضعیت VIP: {label}"]
    if subscription is not None and subscription.authorized_until is not None:
        lines.append("اعتبار تا (UTC): " + subscription.authorized_until.isoformat())
    lines.extend(
        [
            "دانلود رسانهٔ عمومی از سرویس‌های پشتیبانی‌شده برای کاربران رایگان در دسترس است.",
            "دانلود خصوصی فقط با VIP فعال و اتصال شخصی اینستاگرام، برای محتوایی که حساب شما "
            "از قبل می‌بیند انجام می‌شود؛ دورزدن دسترسی انجام نمی‌شود.",
            "Telegram Premium اشتراک VIP این ربات نیست.",
            "اتصال شخصی اینستاگرام:",
            credential_text,
        ]
    )
    enabled = [plan for plan in plans if plan.enabled]
    if pending_order is not None:
        lines.append(
            "پرداخت در انتظار: "
            + _provider_label(pending_order.provider_id)
            + " "
            + str(pending_order.amount_minor)
            + " "
            + pending_order.currency
        )
    if enabled and providers:
        lines.append(
            "پلن‌های فعال: " + "، ".join(f"{p.name} ({p.price_minor} {p.currency})" for p in enabled)
        )
    else:
        lines.append(_PURCHASE_UNAVAILABLE)
    return "\n".join(lines)


def _plan_callback(token: str) -> str:
    return "vip:plan:" + token


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
    plan_lock = asyncio.Lock()

    def providers_for_purchase() -> tuple[PaymentProviderId, ...]:
        if payments is None:
            return ()
        return tuple(
            provider
            for provider in available_providers(settings.payments)
            if provider in payments.gateways
        )

    @router.message(F.text == SUBSCRIPTION_BUTTON)
    @router.message(F.text == BUY_VIP_BUTTON)
    @router.message(Command("vip"))
    async def vip_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        if subscriptions is None:
            await message.answer("وضعیت اشتراک در حال حاضر در دسترس نیست. " + _PURCHASE_UNAVAILABLE)
            return
        owner = message.from_user.id
        now = datetime.now(UTC)
        try:
            subscription = await asyncio.to_thread(subscriptions.get_subscription, owner)
            credential = (
                await asyncio.to_thread(connection.status, owner)
                if connection is not None
                else None
            )
            pending = (
                await asyncio.to_thread(payments.repository.find_pending_order_for_user, owner, now)
                if payments is not None
                else None
            )
            if pending is not None and pending.expires_at <= now:
                pending = None
            plans = await asyncio.to_thread(subscriptions.list_plans)
        except PersistenceError, CredentialError:
            await message.answer(
                "وضعیت اشتراک یا اتصال حساب موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید."
            )
            return
        providers = providers_for_purchase()
        text = render_vip_status(
            subscription=subscription,
            now=now,
            credential_text=render_connection_status(credential),
            plans=plans,
            pending_order=pending,
            providers=providers,
        )
        rows: list[list[InlineKeyboardButton]] = []
        offers: dict[str, dict[str, str | float]] = {}
        for plan in plans:
            if plan.enabled and providers:
                token = secrets.token_urlsafe(12)
                offers[token] = {
                    "plan_id": str(plan.plan_id),
                    "expires_at": (now + timedelta(minutes=30)).timestamp(),
                }
                rows.append(
                    [InlineKeyboardButton(text=plan.name, callback_data=_plan_callback(token))]
                )
        await state.update_data(vip_offers=offers)
        if pending is not None:
            checkout = _recover_checkout_from_order(pending)
            if checkout is not None:
                rows.extend(_checkout_keyboard(checkout).inline_keyboard)
            elif providers:
                rows.extend(_gateway_keyboard(pending, providers).inline_keyboard)
        await message.answer(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )

    @router.callback_query(F.data.startswith("vip:plan:"))
    async def vip_plan_selected(callback: CallbackQuery, state: FSMContext) -> None:
        providers = providers_for_purchase()
        if payments is None or subscriptions is None or not providers:
            await callback.answer(_PURCHASE_UNAVAILABLE, show_alert=True)
            return
        async with plan_lock:
            offers = (await state.get_data()).get("vip_offers", {})
            offer = (
                offers.get((callback.data or "").removeprefix("vip:plan:"))
                if isinstance(offers, dict)
                else None
            )
            if (
                not isinstance(offer, dict)
                or not isinstance(offer.get("plan_id"), str)
                or not isinstance(offer.get("expires_at"), (float, int))
                or offer["expires_at"] <= datetime.now(UTC).timestamp()
            ):
                await callback.answer(_OFFER_EXPIRED, show_alert=True)
                return
            plan = await asyncio.to_thread(subscriptions.get_plan, PlanId(offer["plan_id"]))
            if plan is None or not plan.enabled:
                await callback.answer(_OFFER_EXPIRED, show_alert=True)
                return
            try:
                await state.set_state(None)
                selected_order_id = offer.get("order_id")
                if isinstance(selected_order_id, str):
                    order = await asyncio.to_thread(
                        payments.repository.get_order, PaymentOrderId(selected_order_id)
                    )
                    if (
                        order is None
                        or order.user_id != callback.from_user.id
                        or order.expires_at <= datetime.now(UTC)
                        or order.status not in (PaymentStatus.CREATED, PaymentStatus.PENDING)
                    ):
                        await callback.answer(_OFFER_EXPIRED, show_alert=True)
                        return
                else:
                    order = await asyncio.to_thread(
                        payments.repository.find_pending_order_for_user,
                        callback.from_user.id,
                        datetime.now(UTC),
                    )
                    if (
                        order is None
                        or order.plan_id != plan.plan_id
                        or order.expires_at <= datetime.now(UTC)
                    ):
                        order = await asyncio.to_thread(
                            payments.billing.create_order,
                            callback.from_user.id,
                            plan,
                            expires_at=_now_plus(30),
                        )
                    offer["order_id"] = str(order.order_id)
                    await state.update_data(vip_offers=offers)
            except Exception:
                await callback.answer("ایجاد سفارش ممکن نشد؛ دوباره تلاش کنید.", show_alert=True)
                return
        checkout = _recover_checkout_from_order(order)
        keyboard = (
            _checkout_keyboard(checkout)
            if checkout is not None
            else _gateway_keyboard(order, providers)
        )
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "پلن " + plan.name + " — درگاه پرداخت را انتخاب کنید:",
                reply_markup=keyboard,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vip:gw:"))
    async def vip_gateway_selected(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if payments is None:
            await callback.answer(_PURCHASE_UNAVAILABLE, show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        order_id, gateway = parts[2:]
        order = await asyncio.to_thread(payments.repository.get_order, PaymentOrderId(order_id))
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("سفارش یافت نشد.", show_alert=True)
            return
        if order.expires_at <= datetime.now(UTC):
            await callback.answer(
                "سفارش منقضی شده؛ دوباره از منوی خرید استفاده کنید.", show_alert=True
            )
            return
        if order.status not in (PaymentStatus.CREATED, PaymentStatus.PENDING):
            await callback.answer("این سفارش دیگر قابل پرداخت نیست.", show_alert=True)
            return
        provider = PaymentProviderId(gateway)
        recovered = _recover_checkout_from_order(order)
        if recovered is not None:
            checkout = recovered
        elif provider not in providers_for_purchase():
            await callback.answer(_PURCHASE_UNAVAILABLE, show_alert=True)
            return
        else:
            try:
                checkout = await asyncio.to_thread(
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
                    await callback.answer(
                        "وضعیت پرداخت نامشخص است؛ کمی بعد دوباره بررسی کنید.", show_alert=True
                    )
                    return
                checkout = recovered
            except CheckoutUnavailableError:
                await callback.answer(
                    "درگاه در دسترس نیست؛ کمی بعد دوباره تلاش کنید.", show_alert=True
                )
                return
            except Exception:
                await callback.answer(
                    "شروع پرداخت ممکن نشد؛ کمی بعد دوباره بررسی کنید.", show_alert=True
                )
                return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "خرید را از لینک امن درگاه تکمیل کنید؛ بازگشت از درگاه به‌تنهایی VIP را فعال نمی‌کند.",
                reply_markup=_checkout_keyboard(checkout),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vip:check:"))
    async def vip_check_payment(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if payments is None:
            await callback.answer(_PURCHASE_UNAVAILABLE, show_alert=True)
            return
        order_id = PaymentOrderId((callback.data or "").removeprefix("vip:check:"))
        order = await asyncio.to_thread(payments.repository.get_order, order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("سفارش یافت نشد.", show_alert=True)
            return
        if order.status is PaymentStatus.PAID:
            await callback.answer("پرداخت قبلاً تأیید شده است.", show_alert=True)
            return
        if order.expires_at <= datetime.now(UTC):
            await callback.answer("سفارش منقضی شده است.", show_alert=True)
            return
        if order.status not in (PaymentStatus.CREATED, PaymentStatus.PENDING):
            await callback.answer("این سفارش بسته شده است.", show_alert=True)
            return
        outcome = await asyncio.to_thread(payments.reconciliation.manual_check, order_id)
        outcome_messages: dict[str, str] = {
            CheckOutcome.PAID: "پرداخت تأیید شد.",
            CheckOutcome.PENDING: "پرداخت همچنان در انتظار است.",
            CheckOutcome.EXPIRED: "سفارش منقضی شده است.",
            CheckOutcome.CANCELLED: "سفارش لغو شده است.",
            CheckOutcome.FAILED: "پرداخت ناموفق بود.",
            CheckOutcome.REJECTED: "تأیید پرداخت پذیرفته نشد.",
            CheckOutcome.SKIPPED: "وضعیت نامشخص است؛ کمی بعد دوباره بررسی کنید.",
        }
        await callback.answer(outcome_messages[str(outcome)], show_alert=True)

    return router


def _now_plus(minutes: int) -> datetime:
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


def _gateway_keyboard(
    order: PaymentOrder, providers: tuple[PaymentProviderId, ...]
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_provider_label(provider),
                    callback_data=_gateway_callback(str(order.order_id), str(provider)),
                )
            ]
            for provider in providers
        ]
    )


def _checkout_keyboard(checkout: CheckoutResult) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="پرداخت", url=checkout.checkout_url)],
            [
                InlineKeyboardButton(
                    text="بررسی پرداخت", callback_data=_check_callback(str(checkout.order_id))
                )
            ],
        ]
    )


__all__ = [
    "build_vip_router",
    "render_vip_status",
]
