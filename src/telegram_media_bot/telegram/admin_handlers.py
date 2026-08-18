from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Buffer, Callable
from contextlib import asynccontextmanager
from io import BytesIO

import structlog
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardMarkup

from telegram_media_bot.application.ports.cookie_management import CookieManager
from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.analytics import UsageReport, UsageReportPeriod
from telegram_media_bot.domain.cookies import MAX_COOKIE_UPLOAD_BYTES, CookieService
from telegram_media_bot.domain.errors import (
    CookieFileTooLargeError,
    CookieStoreUnavailableError,
    CookieStoreWriteError,
    EmptyCookieFileError,
    InvalidCookieFileError,
    UnsupportedCookieDomainsError,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_BACK_TO_MENU_BUTTON,
    ADMIN_CANCEL_DOWNLOAD_BUTTON,
    ADMIN_COOKIE_DOWNLOAD_BUTTON,
    ADMIN_COOKIE_HEALTH_BUTTON,
    ADMIN_COOKIE_HEALTH_CHECK_BUTTON,
    ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
    ADMIN_COOKIE_MANAGEMENT_BUTTON,
    ADMIN_COOKIE_UPLOAD_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    ADMIN_FULL_REPORT_BUTTON,
    ADMIN_MANAGEMENT_BUTTONS,
    ADMIN_MONTHLY_REPORT_BUTTON,
    ADMIN_REFRESH_MENU_BUTTON,
    ADMIN_WEEKLY_REPORT_BUTTON,
    AdminCookieState,
    AdminDownloadState,
    build_admin_cookie_health_inline_keyboard,
    build_admin_cookie_health_keyboard,
    build_admin_cookie_keyboard,
    build_admin_download_prompt_keyboard,
    build_admin_main_keyboard,
    build_admin_report_inline_keyboard,
)
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT, START_TEXT
from telegram_media_bot.telegram.ui import cookie_health_status_text

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
COOKIE_MENU_TEXT = "🍪 مدیریت امن فایل کوکی‌ها"  # noqa: RUF001
COOKIE_UPLOAD_PROMPT = (
    "فایل معتبر Netscape cookies.txt را به‌صورت سند ارسال کنید. "
    "سرویس‌ها از دامنهٔ رکوردها تشخیص داده می‌شوند."  # noqa: RUF001
)
COOKIE_INVALID_TEXT = "❌ فایل کوکی نامعتبر است؛ یک فایل Netscape cookies.txt سالم ارسال کنید."
COOKIE_EMPTY_TEXT = "❌ فایل کوکی خالی است یا هیچ رکورد کوکی ندارد."
COOKIE_UNSUPPORTED_TEXT = "❌ دامنهٔ موجود در فایل برای سرویس‌های پشتیبانی‌شده نیست."
COOKIE_TOO_LARGE_TEXT = "❌ حجم فایل کوکی از سقف امن ۲ مگابایت بیشتر است."
COOKIE_STORE_UNAVAILABLE_TEXT = "❌ فایل اصلی کوکی روی سرور در دسترس نیست."
COOKIE_UPDATE_FAILED_TEXT = "❌ به‌روزرسانی امن فایل کوکی انجام نشد؛ فایل قبلی حفظ شده است."
COOKIE_DOWNLOAD_FAILED_TEXT = "❌ دریافت فایل کامل کوکی از سرور ممکن نشد."
COOKIE_UPLOAD_DOCUMENT_REQUIRED_TEXT = "لطفاً فایل cookies.txt را به‌صورت سند تلگرام ارسال کنید."
COOKIE_PRIVATE_CHAT_REQUIRED_TEXT = "مدیریت کوکی فقط در گفت‌وگوی خصوصی با ربات مجاز است."
COOKIE_HEALTH_MENU_TEXT = "🍪 سلامت کوکی‌ها"  # noqa: RUF001
COOKIE_HEALTH_UNAVAILABLE_TEXT = "سرویس سلامت کوکی در دسترس نیست."
COOKIE_HEALTH_CHECK_PROGRESS_TEXT = "🔍 در حال بررسی سلامت همه کوکی‌ها…"  # noqa: RUF001
COOKIE_HEALTH_CHECK_FAILED_TEXT = "❌ بررسی سلامت کوکی‌ها با خطا مواجه شد؛ دوباره تلاش کنید."  # noqa: RUF001

_COOKIE_SERVICE_LABELS = {
    CookieService.YOUTUBE: "YouTube",
    CookieService.INSTAGRAM: "Instagram",
    CookieService.TIKTOK: "TikTok",
    CookieService.TWITTER: "X/Twitter",
    CookieService.PINTEREST: "Pinterest",
    CookieService.SOUNDCLOUD: "SoundCloud",
}


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
    cookie_manager: CookieManager | None = None,
    cookie_health_service: CookieHealthService | None = None,
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

    @router.message(F.text == ADMIN_COOKIE_MANAGEMENT_BUTTON)
    async def open_cookie_management(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_cookie_message(message, state, settings):
            return
        await state.clear()
        await message.answer(COOKIE_MENU_TEXT, reply_markup=build_admin_cookie_keyboard())

    @router.message(F.text == ADMIN_COOKIE_HEALTH_BUTTON)
    async def open_cookie_health(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        await state.clear()
        text = COOKIE_HEALTH_MENU_TEXT
        if cookie_health_service is not None:
            text = cookie_health_status_text(cookie_health_service.all_health())
        await message.answer(text, reply_markup=build_admin_cookie_health_keyboard())

    @router.message(F.text == ADMIN_COOKIE_HEALTH_REFRESH_BUTTON)
    async def refresh_cookie_health(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        if cookie_health_service is None:
            await message.answer(
                COOKIE_HEALTH_UNAVAILABLE_TEXT,
                reply_markup=build_admin_cookie_health_keyboard(),
            )
            return
        status = await message.answer(COOKIE_HEALTH_CHECK_PROGRESS_TEXT)
        try:
            updated, _alerts = await asyncio.to_thread(cookie_health_service.refresh_static)
        except Exception as exc:
            await status.edit_text(COOKIE_HEALTH_CHECK_FAILED_TEXT)
            await logger.aerror("admin_cookie_health_refresh_failed", error_type=type(exc).__name__)
            return
        await status.edit_text(
            cookie_health_status_text(updated),
            reply_markup=build_admin_cookie_health_inline_keyboard(),
        )

    @router.message(F.text == ADMIN_COOKIE_HEALTH_CHECK_BUTTON)
    async def check_cookie_health(message: Message, state: FSMContext) -> None:
        if not await _authorize_message(message, state, settings):
            return
        if cookie_health_service is None:
            await message.answer(
                COOKIE_HEALTH_UNAVAILABLE_TEXT,
                reply_markup=build_admin_cookie_health_keyboard(),
            )
            return
        status = await message.answer(COOKIE_HEALTH_CHECK_PROGRESS_TEXT)
        try:
            updated, _alerts = await asyncio.to_thread(cookie_health_service.refresh_static)
            results = await cookie_health_service.run_active_probes()
            updated, _alerts = cookie_health_service.apply_probe_results(results)
        except Exception as exc:
            await status.edit_text(COOKIE_HEALTH_CHECK_FAILED_TEXT)
            await logger.aerror("admin_cookie_health_check_failed", error_type=type(exc).__name__)
            return
        await status.edit_text(
            cookie_health_status_text(updated),
            reply_markup=build_admin_cookie_health_inline_keyboard(),
        )

    @router.callback_query(F.data.startswith("adm:ch:"))
    async def cookie_health_callback(callback: CallbackQuery) -> None:
        if not _is_admin_user(callback.from_user.id if callback.from_user else None, settings):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if (
            cookie_health_service is None
            or callback.data is None
            or not isinstance(callback.message, Message)
        ):
            await callback.answer(COOKIE_HEALTH_UNAVAILABLE_TEXT, show_alert=True)
            return
        action = callback.data.removeprefix("adm:ch:")
        if action == "open":
            await callback.message.edit_text(
                cookie_health_status_text(cookie_health_service.all_health()),
                reply_markup=build_admin_cookie_health_inline_keyboard(),
            )
            await callback.answer()
            return
        if action not in {"check", "refresh"}:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        await callback.answer(COOKIE_HEALTH_CHECK_PROGRESS_TEXT)
        try:
            updated, _alerts = await asyncio.to_thread(cookie_health_service.refresh_static)
            if action == "check":
                results = await cookie_health_service.run_active_probes()
                updated, _alerts = cookie_health_service.apply_probe_results(results)
        except Exception as exc:
            await callback.message.edit_text(COOKIE_HEALTH_CHECK_FAILED_TEXT)
            await logger.aerror(
                "admin_cookie_health_callback_failed",
                action=action,
                error_type=type(exc).__name__,
            )
            return
        await callback.message.edit_text(
            cookie_health_status_text(updated),
            reply_markup=build_admin_cookie_health_inline_keyboard(),
        )

    @router.message(F.text == ADMIN_COOKIE_UPLOAD_BUTTON)
    async def begin_cookie_upload(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_cookie_message(message, state, settings):
            return
        if cookie_manager is None:
            await message.answer(
                COOKIE_STORE_UNAVAILABLE_TEXT,
                reply_markup=build_admin_cookie_keyboard(),
            )
            return
        await state.set_state(AdminCookieState.awaiting_upload)
        await message.answer(COOKIE_UPLOAD_PROMPT, reply_markup=build_admin_cookie_keyboard())

    @router.message(F.text == ADMIN_COOKIE_DOWNLOAD_BUTTON)
    async def download_combined_cookies(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_cookie_message(message, state, settings):
            return
        if cookie_manager is None:
            await message.answer(
                COOKIE_STORE_UNAVAILABLE_TEXT,
                reply_markup=build_admin_cookie_keyboard(),
            )
            return
        try:
            content = await asyncio.to_thread(cookie_manager.export_combined)
            await message.answer_document(
                BufferedInputFile(content, filename="cookies.txt"),
                caption="فایل کامل و فعلی cookies.txt سرور",
                reply_markup=build_admin_cookie_keyboard(),
            )
        except Exception as exc:
            await message.answer(
                COOKIE_DOWNLOAD_FAILED_TEXT,
                reply_markup=build_admin_cookie_keyboard(),
            )
            await logger.aerror(
                "admin_cookie_download_failed",
                error_type=type(exc).__name__,
            )

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

    @router.message(StateFilter(AdminCookieState.awaiting_upload), F.document)
    async def receive_cookie_upload(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_cookie_message(message, state, settings):
            return
        if cookie_manager is None or message.document is None:
            await message.answer(
                COOKIE_STORE_UNAVAILABLE_TEXT,
                reply_markup=build_admin_cookie_keyboard(),
            )
            return
        try:
            if (
                message.document.file_size is not None
                and message.document.file_size > MAX_COOKIE_UPLOAD_BYTES
            ):
                raise CookieFileTooLargeError("declared cookie upload is too large")
            uploaded = await _download_cookie_document(message)
            summary = await asyncio.to_thread(cookie_manager.merge, uploaded)
        except CookieFileTooLargeError:
            text = COOKIE_TOO_LARGE_TEXT
        except EmptyCookieFileError:
            text = COOKIE_EMPTY_TEXT
        except UnsupportedCookieDomainsError:
            text = COOKIE_UNSUPPORTED_TEXT
        except InvalidCookieFileError:
            text = COOKIE_INVALID_TEXT
        except CookieStoreUnavailableError:
            text = COOKIE_STORE_UNAVAILABLE_TEXT
        except CookieStoreWriteError as exc:
            text = COOKIE_UPDATE_FAILED_TEXT
            await logger.aerror("admin_cookie_update_failed", error_type=type(exc).__name__)
        except Exception as exc:
            text = COOKIE_UPDATE_FAILED_TEXT
            await logger.aerror("admin_cookie_update_failed", error_type=type(exc).__name__)
        else:
            await state.clear()
            text = _render_cookie_update_summary(summary.services, summary.replaced, summary.added)
        await message.answer(text, reply_markup=build_admin_cookie_keyboard())

    @router.message(StateFilter(AdminCookieState.awaiting_upload))
    async def reject_non_document_cookie_upload(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_cookie_message(message, state, settings):
            return
        await message.answer(
            COOKIE_UPLOAD_DOCUMENT_REQUIRED_TEXT,
            reply_markup=build_admin_cookie_keyboard(),
        )

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


async def _authorize_private_cookie_message(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> bool:
    if not await _authorize_message(message, state, settings):
        return False
    if message.chat.type == "private":
        return True
    await state.clear()
    await message.answer(COOKIE_PRIVATE_CHAT_REQUIRED_TEXT)
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


class _BoundedBytesIO(BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self._written = 0

    def write(self, data: Buffer) -> int:
        size = memoryview(data).nbytes
        if self._written + size > self._limit:
            raise CookieFileTooLargeError("downloaded cookie upload is too large")
        written = super().write(data)
        self._written += written
        return written


async def _download_cookie_document(message: Message) -> bytes:
    assert message.document is not None
    bot = message.bot
    if bot is None:
        raise RuntimeError("Telegram bot context is unavailable")
    with _BoundedBytesIO(MAX_COOKIE_UPLOAD_BYTES) as destination:
        await bot.download(message.document, destination=destination)
        return destination.getvalue()


def _render_cookie_update_summary(
    services: tuple[CookieService, ...], replaced: int, added: int
) -> str:
    labels = "، ".join(_COOKIE_SERVICE_LABELS[service] for service in services)
    return (
        "✅ فایل کوکی با موفقیت به‌روزرسانی شد.\n"
        f"سرویس‌های تشخیص‌داده‌شده: {labels}\n"
        f"رکوردهای جایگزین‌شده: {replaced}\n"
        f"رکوردهای افزوده‌شده: {added}"
    )
