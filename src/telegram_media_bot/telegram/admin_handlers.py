from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardMarkup

from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.analytics import UsageReport, UsageReportPeriod
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_BACK_TO_MENU_BUTTON,
    ADMIN_CANCEL_DOWNLOAD_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    ADMIN_FULL_REPORT_BUTTON,
    ADMIN_MANAGEMENT_BUTTONS,
    ADMIN_MONTHLY_REPORT_BUTTON,
    ADMIN_REFRESH_MENU_BUTTON,
    ADMIN_WEEKLY_REPORT_BUTTON,
    AdminDownloadState,
    build_admin_download_prompt_keyboard,
    build_admin_main_keyboard,
    build_admin_report_inline_keyboard,
)
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT, START_TEXT

logger = structlog.get_logger(__name__)

SubmitUrl = Callable[[Message, ReplyKeyboardMarkup | None], Awaitable[bool]]

ADMIN_MENU_TEXT = "منوی مدیریت آماده است."
ADMIN_DOWNLOAD_PROMPT = (
    "🔗 لینک رسانه را ارسال کنید.\n\n"
    "می‌توانید لینک YouTube، Instagram، TikTok، X/Twitter، Pinterest یا SoundCloud را بفرستید."
)
ADMIN_DOWNLOAD_CANCELLED = "دریافت لینک لغو شد."
REPORT_PREPARING_TEXT = "⏳ در حال آماده‌سازی گزارش..."
REPORT_BUSY_TEXT = "گزارش دیگری برای شما در حال آماده‌سازی است."
REPORT_FAILED_TEXT = "❌ تهیه گزارش با خطا مواجه شد. لطفاً دوباره تلاش کنید."


class AdminReportCoordinator:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: set[int] = set()

    @asynccontextmanager
    async def single_flight(self, user_id: int) -> AsyncIterator[bool]:
        async with self._lock:
            acquired = user_id not in self._active
            if acquired:
                self._active.add(user_id)
        try:
            yield acquired
        finally:
            if acquired:
                async with self._lock:
                    self._active.discard(user_id)


def build_admin_router(
    *,
    settings: Settings,
    submit_url: SubmitUrl,
    analytics: UsageAnalyticsService | None,
    chart_renderer: UsageChartRenderer | None,
) -> Router:
    router = Router(name="admin")
    reports = AdminReportCoordinator()

    @router.message(Command("menu"))
    async def menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        if _is_admin_user(message.from_user.id if message.from_user else None, settings):
            await message.answer(ADMIN_MENU_TEXT, reply_markup=build_admin_main_keyboard())
        else:
            await message.answer(START_TEXT)

    @router.message(Command("panel"))
    async def panel(message: Message, state: FSMContext) -> None:
        if not _is_admin_user(message.from_user.id if message.from_user else None, settings):
            await state.clear()
            await message.answer(ACCESS_DENIED_TEXT)
            return
        await state.clear()
        await message.answer(ADMIN_MENU_TEXT, reply_markup=build_admin_main_keyboard())

    @router.message(F.text == ADMIN_DOWNLOAD_BUTTON)
    async def begin_admin_download(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        await state.set_state(AdminDownloadState.awaiting_url)
        await message.answer(
            ADMIN_DOWNLOAD_PROMPT,
            reply_markup=build_admin_download_prompt_keyboard(),
        )

    @router.message(F.text == ADMIN_REFRESH_MENU_BUTTON)
    @router.message(F.text == ADMIN_BACK_TO_MENU_BUTTON)
    async def refresh_admin_menu(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        await state.clear()
        await message.answer(ADMIN_MENU_TEXT, reply_markup=build_admin_main_keyboard())

    @router.message(
        StateFilter(AdminDownloadState.awaiting_url),
        F.text == ADMIN_CANCEL_DOWNLOAD_BUTTON,
    )
    async def cancel_admin_download(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        await state.clear()
        await message.answer(
            ADMIN_DOWNLOAD_CANCELLED,
            reply_markup=build_admin_main_keyboard(),
        )

    @router.message(F.text == ADMIN_WEEKLY_REPORT_BUTTON)
    async def weekly_report(message: Message, state: FSMContext) -> None:
        await _report_from_message(
            message,
            state,
            settings=settings,
            analytics=analytics,
            chart_renderer=chart_renderer,
            coordinator=reports,
            period=UsageReportPeriod.WEEKLY,
        )

    @router.message(F.text == ADMIN_MONTHLY_REPORT_BUTTON)
    async def monthly_report(message: Message, state: FSMContext) -> None:
        await _report_from_message(
            message,
            state,
            settings=settings,
            analytics=analytics,
            chart_renderer=chart_renderer,
            coordinator=reports,
            period=UsageReportPeriod.MONTHLY,
        )

    @router.message(F.text == ADMIN_FULL_REPORT_BUTTON)
    async def full_report(message: Message, state: FSMContext) -> None:
        await _report_from_message(
            message,
            state,
            settings=settings,
            analytics=analytics,
            chart_renderer=chart_renderer,
            coordinator=reports,
            period=UsageReportPeriod.FULL,
        )

    @router.callback_query(F.data == "adm:menu")
    async def callback_admin_menu(callback: CallbackQuery) -> None:
        if not _is_admin_user(callback.from_user.id if callback.from_user else None, settings):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if isinstance(callback.message, Message):
            await callback.message.answer(
                ADMIN_MENU_TEXT,
                reply_markup=build_admin_main_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("adm:rpt:"))
    async def refresh_report(callback: CallbackQuery) -> None:
        if not _is_admin_user(callback.from_user.id if callback.from_user else None, settings):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        period = _callback_period(callback.data)
        if period is None or not isinstance(callback.message, Message):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        await callback.answer(REPORT_PREPARING_TEXT)
        await _send_report(
            callback.message,
            callback.from_user.id,
            analytics=analytics,
            chart_renderer=chart_renderer,
            coordinator=reports,
            period=period,
        )

    @router.message(StateFilter(AdminDownloadState.awaiting_url))
    async def receive_admin_url(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        accepted = await submit_url(message, build_admin_download_prompt_keyboard())
        if accepted:
            await state.clear()

    @router.message(F.text.in_(ADMIN_MANAGEMENT_BUTTONS))
    async def reject_forged_admin_button(message: Message, state: FSMContext) -> None:
        if _is_admin_user(message.from_user.id if message.from_user else None, settings):
            await message.answer(ADMIN_MENU_TEXT, reply_markup=build_admin_main_keyboard())
            return
        await state.clear()
        await message.answer(ACCESS_DENIED_TEXT)

    return router


async def _report_from_message(
    message: Message,
    state: FSMContext,
    *,
    settings: Settings,
    analytics: UsageAnalyticsService | None,
    chart_renderer: UsageChartRenderer | None,
    coordinator: AdminReportCoordinator,
    period: UsageReportPeriod,
) -> None:
    if not await _authorize_message(message, state, settings):
        return
    await state.clear()
    assert message.from_user is not None
    await _send_report(
        message,
        message.from_user.id,
        analytics=analytics,
        chart_renderer=chart_renderer,
        coordinator=coordinator,
        period=period,
    )


async def _send_report(
    message: Message,
    user_id: int,
    *,
    analytics: UsageAnalyticsService | None,
    chart_renderer: UsageChartRenderer | None,
    coordinator: AdminReportCoordinator,
    period: UsageReportPeriod,
) -> None:
    async with coordinator.single_flight(user_id) as acquired:
        if not acquired:
            await message.answer(REPORT_BUSY_TEXT, reply_markup=build_admin_main_keyboard())
            return
        status = await message.answer(
            REPORT_PREPARING_TEXT,
            reply_markup=build_admin_main_keyboard(),
        )
        try:
            if analytics is None:
                raise RuntimeError("Usage analytics is unavailable")
            report = await asyncio.to_thread(analytics.build, period)
            if period is UsageReportPeriod.FULL:
                await message.answer(
                    render_full_usage_report(report),
                    reply_markup=build_admin_report_inline_keyboard(period),
                )
            else:
                if chart_renderer is None:
                    raise RuntimeError("Usage chart renderer is unavailable")
                image = await asyncio.to_thread(chart_renderer.render, report)
                await message.answer_photo(
                    BufferedInputFile(image, filename=f"usage-{period.value}.png"),
                    caption=render_report_caption(report),
                    reply_markup=build_admin_report_inline_keyboard(period),
                )
            await status.edit_text("✅ گزارش آماده شد.")
        except Exception as exc:
            await status.edit_text(REPORT_FAILED_TEXT)
            await logger.aexception(
                "admin_usage_report_failed",
                report_period=period.value,
                error_type=type(exc).__name__,
            )


async def _authorize_message(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> bool:
    if _is_admin_user(message.from_user.id if message.from_user else None, settings):
        return True
    await state.clear()
    await message.answer(ACCESS_DENIED_TEXT)
    return False


def _is_admin_user(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.telegram.admin_ids


def _callback_period(data: str | None) -> UsageReportPeriod | None:
    return {
        "adm:rpt:w:refresh": UsageReportPeriod.WEEKLY,
        "adm:rpt:m:refresh": UsageReportPeriod.MONTHLY,
        "adm:rpt:full:refresh": UsageReportPeriod.FULL,
    }.get(data or "")


def render_report_caption(report: UsageReport) -> str:
    title = "گزارش هفتگی" if report.period is UsageReportPeriod.WEEKLY else "گزارش ماهانه"
    return (
        f"📊 {title}\n"
        f"کاربران یکتا: {report.unique_users}\n"
        f"تعامل‌ها: {report.interactions}\n"  # noqa: RUF001
        f"دانلودها: {report.downloads}\n"
        f"موفق / ناموفق / لغوشده: {report.succeeded} / {report.failed} / {report.cancelled}\n"
        f"حجم تحویل‌شده: {_format_bytes(report.delivered_bytes)}"
    )


def render_full_usage_report(report: UsageReport) -> str:
    sources = "\n".join(f"• {item.label}: {item.count}" for item in report.sources) or "• بدون داده"
    formats = "\n".join(f"• {item.label}: {item.count}" for item in report.formats) or "• بدون داده"
    daily = "\n".join(
        f"• {item.day.isoformat()}: تعامل {item.interactions}، دانلود {item.downloads}، "
        f"موفق {item.succeeded}، ناموفق {item.failed}، لغو {item.cancelled}"
        for item in report.daily
    )
    return (
        "📋 گزارش کامل استفاده\n\n"
        f"کاربران یکتا: {report.unique_users}\n"
        f"تعامل‌ها: {report.interactions}\n"  # noqa: RUF001
        f"دانلودها: {report.downloads}\n"
        f"موفق: {report.succeeded}\n"
        f"ناموفق: {report.failed}\n"
        f"لغوشده: {report.cancelled}\n"
        f"حجم تحویل‌شده: {_format_bytes(report.delivered_bytes)}\n\n"
        f"منابع:\n{sources}\n\n"
        f"فرمت‌ها:\n{formats}\n\n"  # noqa: RUF001
        f"جزئیات روزانه ۱۴ روز اخیر:\n{daily}"
    )


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
