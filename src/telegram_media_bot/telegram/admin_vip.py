"""Role-authorized VIP administration panel (T024/T025).

Every message/callback/FSM continuation reauthorizes against ``settings.telegram.admin_ids``;
a stale screen opened by an admin is never trusted again. Gift grants use a distinct
``admin_grant`` source and never fabricate a provider payment. Revocation reverses ONLY
admin-issued grants; suspension never mutates payment history. Plan catalog operations are
restricted to this panel and write through VipAdminService (no raw SQL in handlers).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from telegram_media_bot.application.services.vip_admin import VipAdminService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.bootstrap.payments import PaymentRuntime
from telegram_media_bot.domain.subscriptions import (
    Capability,
    PlanId,
    SubscriptionPlan,
    SubscriptionStatus,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_VIP_BUTTON,
    build_admin_main_keyboard,
)
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT

_STATUS_LABELS: dict[str, str] = {
    SubscriptionStatus.ACTIVE.value: "active",
    SubscriptionStatus.EXPIRED.value: "expired",
    SubscriptionStatus.CANCELLED.value: "cancelled",
    SubscriptionStatus.INACTIVE.value: "inactive",
    SubscriptionStatus.SUSPENDED.value: "suspended",
}

_CAPABILITY_LABELS: dict[Capability, str] = {
    Capability.INSTAGRAM_PRIVATE_MEDIA: "instagram_private_media",
    Capability.INSTAGRAM_USER_SESSION_PREFERENCE: "instagram_user_session_preference",
}


class AdminVipState(StatesGroup):
    awaiting_user_id = State()
    awaiting_grant_plan = State()
    awaiting_grant_months = State()


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="inspect user", callback_data="vipadm:inspect")],
            [
                InlineKeyboardButton(text="grant gift", callback_data="vipadm:grant"),
                InlineKeyboardButton(text="revoke gifts", callback_data="vipadm:revoke"),
            ],
            [
                InlineKeyboardButton(text="suspend/unsuspend", callback_data="vipadm:suspend"),
                InlineKeyboardButton(text="plans", callback_data="vipadm:plans"),
            ],
            [InlineKeyboardButton(text="payment stats", callback_data="vipadm:stats")],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="cancel", callback_data="vipadm:menu")]]
    )


def _render_plans(plans: tuple[SubscriptionPlan, ...]) -> str:
    if not plans:
        return "no plans yet; create one first"
    lines = []
    for plan in plans:
        caps = ", ".join(
            _CAPABILITY_LABELS.get(cap, cap.value) for cap in sorted(plan.capabilities)
        )
        lines.append(
            f"{plan.plan_id}: {plan.name} / {plan.duration_months}mo / "
            f"{plan.price_minor} {plan.currency} / enabled={plan.enabled} / {caps}"
        )
    return "\n".join(lines)


def _status_text(view: dict[str, object]) -> str:
    subscription = view.get("subscription")
    grant_counts: list[int] = []
    for key in ("grants", "admin_grants", "paid_grants"):
        value = view.get(key)
        grant_counts.append(len(value) if isinstance(value, (tuple, list)) else 0)
    status = "n/a"
    authorized = ""
    if subscription is not None:
        status_value = getattr(subscription, "status", None)
        status = _STATUS_LABELS.get(str(status_value), str(status_value)) if status_value else "n/a"
        authorized_until = getattr(subscription, "authorized_until", None)
        authorized = str(authorized_until) if authorized_until else ""
    lines = [f"VIP status: {status}"]
    if authorized:
        lines.append(f"valid until: {authorized}")
    total, admin, paid = grant_counts
    lines.append(f"grants: total={total} admin={admin} paid={paid}")
    return "\n".join(lines)


def build_admin_vip_router(
    *,
    settings: Settings,
    vip_admin: VipAdminService | None,
    payments: PaymentRuntime | None,
) -> Router:
    router = Router(name="admin_vip")

    def _authorized(user_id: int | None) -> bool:
        return user_id is not None and user_id in settings.telegram.admin_ids

    @router.message(F.text == ADMIN_VIP_BUTTON)
    async def open_vip_panel(message: Message, state: FSMContext) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        if vip_admin is None:
            await message.answer(
                "VIP management is not configured.",
                reply_markup=build_admin_main_keyboard(),
            )
            return
        await state.clear()
        await message.answer("VIP admin panel", reply_markup=_menu_keyboard())

    @router.callback_query(F.data.startswith("vipadm:"))
    async def vip_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if not _authorized(callback.from_user.id if callback.from_user else None):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if vip_admin is None or callback.message is None:
            await callback.answer("not configured", show_alert=True)
            return
        if not isinstance(callback.message, Message):
            await callback.answer("stale screen", show_alert=True)
            return
        message = callback.message
        action = str(callback.data or "").removeprefix("vipadm:").split(":", 1)[0]
        if action == "menu":
            await state.clear()
            await message.edit_text("VIP admin panel", reply_markup=_menu_keyboard())
            await callback.answer()
            return
        if action in ("inspect", "grant", "suspend", "revoke"):
            await state.set_state(AdminVipState.awaiting_user_id)
            await state.update_data(vip_action=action)
            await message.edit_text(
                "enter a numeric telegram user id (or cancel):",
                reply_markup=_cancel_keyboard(),
            )
            await callback.answer()
            return
        if action == "plans":
            await message.edit_text(_render_plans(vip_admin.list_plans()))
            await callback.answer()
            return
        if action == "stats":
            if payments is None:
                await message.edit_text("payment runtime is not configured")
            else:
                counts = await asyncio.to_thread(payments.repository.count_orders_by_status)
                text = "payment status counts:\n" + "\n".join(
                    f"{key}: {value}" for key, value in counts.items()
                )
                await message.edit_text(text)
            await callback.answer()
            return
        await callback.answer("unknown action", show_alert=True)

    @router.callback_query(F.data.startswith("vipadm:plan:"))
    async def choose_grant_plan(callback: CallbackQuery, state: FSMContext) -> None:
        if not _authorized(callback.from_user.id if callback.from_user else None):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if vip_admin is None or callback.message is None:
            await callback.answer("not configured", show_alert=True)
            return
        if not isinstance(callback.message, Message):
            await callback.answer("stale screen", show_alert=True)
            return
        plan_id = str(callback.data or "").removeprefix("vipadm:plan:")
        await state.update_data(vip_plan_id=plan_id)
        await state.set_state(AdminVipState.awaiting_grant_months)
        await callback.message.edit_text(
            "enter duration in whole months (or cancel):",
            reply_markup=_cancel_keyboard(),
        )
        await callback.answer()

    @router.message(F.state == AdminVipState.awaiting_user_id)
    async def receive_user_id(message: Message, state: FSMContext) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        assert vip_admin is not None
        raw = (message.text or "").strip()
        if not raw.isdigit():
            await message.answer(
                "invalid user id; enter digits only", reply_markup=_cancel_keyboard()
            )
            return
        target = int(raw)
        data = await state.get_data()
        action = str(data.get("vip_action", "inspect"))
        now = datetime.now(UTC)
        actor = message.from_user.id if message.from_user else 0
        if action == "inspect":
            view = await asyncio.to_thread(vip_admin.inspect_user, target)
            await message.answer(_status_text(view), reply_markup=_menu_keyboard())
            await state.clear()
            return
        if action == "revoke":
            result = await asyncio.to_thread(
                vip_admin.revoke_gifts,
                actor_user_id=actor,
                target_user_id=target,
                now=now,
            )
            await message.answer(_result_text(result), reply_markup=_menu_keyboard())
            await state.clear()
            return
        if action == "suspend":
            view = await asyncio.to_thread(vip_admin.inspect_user, target)
            subscription = view.get("subscription")
            suspended = (
                getattr(subscription, "suspended_at", None) is not None
                if subscription is not None
                else False
            )
            result = await asyncio.to_thread(
                vip_admin.set_suspended,
                actor_user_id=actor,
                target_user_id=target,
                suspended=not suspended,
                reason="admin_toggle",
                now=now,
            )
            await message.answer(_result_text(result), reply_markup=_menu_keyboard())
            await state.clear()
            return
        # grant: choose an enabled plan
        plans = vip_admin.list_plans()
        enabled = [plan for plan in plans if plan.enabled]
        if not enabled:
            await message.answer(
                "no enabled plan exists; create a plan first",
                reply_markup=_menu_keyboard(),
            )
            await state.clear()
            return
        await state.set_state(AdminVipState.awaiting_grant_plan)
        await state.update_data(vip_target_id=target)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"{plan.name} ({plan.duration_months}mo)",
                        callback_data=f"vipadm:plan:{plan.plan_id}",
                    )
                ]
                for plan in enabled
            ]
            + [[InlineKeyboardButton(text="cancel", callback_data="vipadm:menu")]]
        )
        await message.answer("choose the plan for the gift:", reply_markup=keyboard)

    @router.message(F.state == AdminVipState.awaiting_grant_months)
    async def receive_grant_months(message: Message, state: FSMContext) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        assert vip_admin is not None
        raw = (message.text or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            await message.answer(
                "months must be a positive integer",
                reply_markup=_cancel_keyboard(),
            )
            return
        months = int(raw)
        data = await state.get_data()
        plan_id = str(data.get("vip_plan_id", ""))
        target = int(data.get("vip_target_id", 0))
        actor = message.from_user.id if message.from_user else 0
        result = await asyncio.to_thread(
            vip_admin.grant_gift,
            actor_user_id=actor,
            target_user_id=target,
            plan_id=PlanId(plan_id),
            duration_months=months,
            now=datetime.now(UTC),
        )
        await message.answer(_result_text(result), reply_markup=_menu_keyboard())
        await state.clear()

    return router


def _result_text(result: object) -> str:
    message = getattr(result, "message", "")
    authorized = getattr(result, "authorized_until", None)
    text = f"gift ok: {message}"
    if authorized:
        text += f"\nvalid until: {authorized}"
    return text


__all__ = ["AdminVipState", "build_admin_vip_router"]
