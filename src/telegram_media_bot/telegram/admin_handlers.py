from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Buffer, Callable
from contextlib import asynccontextmanager
from io import BytesIO

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

from telegram_media_bot.application.ports.cookie_management import CookieManager
from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.application.services.audit_destination_admin import (
    ConfigOwnedLoggerChannelError,
    InvalidLoggerChannelError,
    LoggerDestinationAdminService,
)
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.job_recovery_service import JobRecoveryService
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.analytics import UsageReport, UsageReportPeriod
from telegram_media_bot.domain.audit import (
    DestinationProbeOutcome,
    LoggerDestination,
    LoggerDestinationHealth,
)
from telegram_media_bot.domain.cookie_health import CookieHealthState, ProviderCookieHealth
from telegram_media_bot.domain.cookies import (
    MAX_COOKIE_UPLOAD_BYTES,
    CookieService,
    CookieUpdateSummary,
)
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
    ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
    ADMIN_COOKIE_MANAGEMENT_BUTTON,
    ADMIN_COOKIE_UPLOAD_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    ADMIN_FULL_REPORT_BUTTON,
    ADMIN_LOGGER_ADD_BUTTON,
    ADMIN_LOGGER_BUTTON,
    ADMIN_MANAGEMENT_BUTTONS,
    ADMIN_MONTHLY_REPORT_BUTTON,
    ADMIN_REFRESH_MENU_BUTTON,
    ADMIN_WEEKLY_REPORT_BUTTON,
    AdminCookieState,
    AdminDownloadState,
    AdminLoggerState,
    build_admin_cookie_health_inline_keyboard,
    build_admin_cookie_health_keyboard,
    build_admin_cookie_keyboard,
    build_admin_download_prompt_keyboard,
    build_admin_logger_inline_keyboard,
    build_admin_logger_keyboard,
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
LOGGER_MENU_TEXT = "🧾 مدیریت کانال‌های لاگر"
LOGGER_UNAVAILABLE_TEXT = "سرویس لاگر در دسترس نیست."
LOGGER_DISABLED_TEXT = (
    "⚠️ لاگر در پیکربندی غیرفعال است؛ کانال‌ها فقط مدیریت می‌شوند و ارسال انجام نمی‌شود."  # noqa: RUF001
)
LOGGER_ADD_PROMPT_TEXT = "شناسه عددی کانال را به شکل -100... ارسال کنید."
LOGGER_INVALID_ID_TEXT = "❌ شناسه نامعتبر است؛ شناسه عددی کانال باید با -100... شروع شود."
LOGGER_ADDED_TEXT = "✅ کانال لاگر افزوده شد و وضعیت آن بررسی می‌شود."
LOGGER_REMOVED_TEXT = "🗑️ کانال لاگر حذف شد."
LOGGER_CONFIG_OWNED_TEXT = "کانال پیکربندی‌شده را نمی‌توان از اینجا حذف کرد."
LOGGER_NOT_FOUND_TEXT = "کانال لاگر یافت نشد."
LOGGER_EMPTY_TEXT = "هیچ کانال لاگری ثبت نشده است."
LOGGER_PRIVATE_CHAT_REQUIRED_TEXT = "مدیریت کانال‌های لاگر فقط در گفت‌وگوی خصوصی با ربات مجاز است."
LOGGER_TEST_PROGRESS_TEXT = "🔍 در حال آزمایش کانال…"
COOKIE_HEALTH_CHECK_PROGRESS_TEXT = "🔍 در حال بررسی محلی فایل کوکی‌ها…"  # noqa: RUF001
COOKIE_HEALTH_CHECK_FAILED_TEXT = "❌ بررسی محلی کوکی‌ها با خطا مواجه شد؛ دوباره تلاش کنید."  # noqa: RUF001

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
    recovery_service: JobRecoveryService | None = None,
    audit_admin: LoggerDestinationAdminService | None = None,
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

    @router.message(F.text == ADMIN_LOGGER_BUTTON)
    async def open_logger_menu(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_admin_message(
            message, state, settings, LOGGER_PRIVATE_CHAT_REQUIRED_TEXT
        ):
            return
        await state.clear()
        if audit_admin is None:
            await message.answer(LOGGER_UNAVAILABLE_TEXT, reply_markup=build_admin_main_keyboard())
            return
        destinations = await asyncio.to_thread(audit_admin.list)
        await message.answer(
            _render_logger_destinations(
                destinations, logger_enabled=settings.telegram.logger.enabled
            ),
            reply_markup=build_admin_logger_inline_keyboard(destinations),
        )

    @router.message(F.text == ADMIN_LOGGER_ADD_BUTTON)
    async def begin_logger_add(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_admin_message(
            message, state, settings, LOGGER_PRIVATE_CHAT_REQUIRED_TEXT
        ):
            return
        if audit_admin is None:
            await message.answer(LOGGER_UNAVAILABLE_TEXT, reply_markup=build_admin_main_keyboard())
            return
        await state.set_state(AdminLoggerState.awaiting_add_chat_id)
        await message.answer(LOGGER_ADD_PROMPT_TEXT, reply_markup=build_admin_logger_keyboard())

    @router.message(StateFilter(AdminLoggerState.awaiting_add_chat_id))
    async def receive_logger_chat_id(message: Message, state: FSMContext) -> None:
        if not await _authorize_private_admin_message(
            message, state, settings, LOGGER_PRIVATE_CHAT_REQUIRED_TEXT
        ):
            return
        if audit_admin is None:
            await message.answer(LOGGER_UNAVAILABLE_TEXT, reply_markup=build_admin_main_keyboard())
            return
        raw = (message.text or "").strip()
        try:
            chat_id = int(raw)
        except ValueError:
            await message.answer(LOGGER_INVALID_ID_TEXT, reply_markup=build_admin_logger_keyboard())
            return
        try:
            await asyncio.to_thread(audit_admin.add, chat_id)
        except InvalidLoggerChannelError:
            await message.answer(LOGGER_INVALID_ID_TEXT, reply_markup=build_admin_logger_keyboard())
            return
        await state.clear()
        await message.answer(LOGGER_ADDED_TEXT, reply_markup=build_admin_logger_keyboard())
        await _send_logger_list(
            message,
            audit_admin,
            logger_enabled=settings.telegram.logger.enabled,
            probe_chat_id=chat_id,
        )

    @router.callback_query(F.data.startswith("adm:lg:"))
    async def logger_callback(callback: CallbackQuery) -> None:
        if not _is_admin_user(callback.from_user.id if callback.from_user else None, settings):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if (
            audit_admin is None
            or callback.data is None
            or not isinstance(callback.message, Message)
        ):
            await callback.answer(LOGGER_UNAVAILABLE_TEXT, show_alert=True)
            return
        parsed = _parse_logger_callback(callback.data)
        if parsed is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        action, chat_id = parsed
        if action == "noop":
            await callback.answer()
            return
        if action == "add":
            await callback.answer(LOGGER_ADD_PROMPT_TEXT)
            await callback.message.answer(
                LOGGER_ADD_PROMPT_TEXT, reply_markup=build_admin_logger_keyboard()
            )
            return
        if action in {"test", "enable", "disable", "remove", "confirm", "cancel"}:
            assert chat_id is not None
            await _handle_logger_channel_action(
                callback,
                audit_admin,
                logger_enabled=settings.telegram.logger.enabled,
                action=action,
                chat_id=chat_id,
            )
            return
        await _send_logger_list(
            callback.message,
            audit_admin,
            logger_enabled=settings.telegram.logger.enabled,
        )
        await callback.answer()

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
            try:
                updated, _alerts = await asyncio.to_thread(cookie_health_service.refresh_static)
                text = cookie_health_status_text(updated)
            except Exception as exc:
                text = COOKIE_HEALTH_CHECK_FAILED_TEXT
                await logger.aerror(
                    "admin_cookie_health_refresh_failed", error_type=type(exc).__name__
                )
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
            await callback.answer()
        elif action == "refresh":
            await callback.answer(COOKIE_HEALTH_CHECK_PROGRESS_TEXT)
        else:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        try:
            updated, _alerts = await asyncio.to_thread(cookie_health_service.refresh_static)
        except Exception as exc:
            await _edit_cookie_health_message(
                callback.message,
                COOKIE_HEALTH_CHECK_FAILED_TEXT,
            )
            await logger.aerror(
                "admin_cookie_health_callback_failed",
                action=action,
                error_type=type(exc).__name__,
            )
            return
        await _edit_cookie_health_message(
            callback.message,
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
        logger.info("cookie_upload_started")
        try:
            if (
                message.document.file_size is not None
                and message.document.file_size > MAX_COOKIE_UPLOAD_BYTES
            ):
                raise CookieFileTooLargeError("declared cookie upload is too large")
            uploaded = await _download_cookie_document(message)
            summary = await asyncio.to_thread(cookie_manager.merge, uploaded)
            for provider in summary.services:
                logger.info(
                    "cookie_upload_provider_detected",
                    provider=provider.value,
                    uploaded_record_count=summary.uploaded_record_count,
                    matched_record_count=summary.record_count(provider),
                )
            logger.info(
                "cookie_upload_merge_completed",
                uploaded_record_count=summary.uploaded_record_count,
                previous_canonical_record_count=summary.previous_canonical_record_count,
                new_canonical_record_count=summary.new_canonical_record_count,
                preserved_other_provider_count=summary.preserved_other_provider_count,
            )
            health_by_provider: dict[CookieService, ProviderCookieHealth] = {}
            if cookie_health_service is not None:
                health_by_provider, _alerts = await asyncio.to_thread(
                    cookie_health_service.refresh_static,
                    summary.services,
                    clear_runtime_auth_failure=True,
                )
                for provider in summary.services:
                    health = health_by_provider[provider]
                    logger.info(
                        "cookie_health_provider_refreshed",
                        provider=provider.value,
                        health_after=health.status.value,
                        matched_record_count=health.static.record_count,
                    )
                    if health.status is CookieHealthState.MISSING:
                        raise CookieStoreWriteError(
                            "canonical provider verification returned missing"
                        )
        except CookieFileTooLargeError:
            text = COOKIE_TOO_LARGE_TEXT
            logger.warning("cookie_upload_failed", error_type="CookieFileTooLargeError")
        except EmptyCookieFileError:
            text = COOKIE_EMPTY_TEXT
            logger.warning("cookie_upload_failed", error_type="EmptyCookieFileError")
        except UnsupportedCookieDomainsError:
            text = COOKIE_UNSUPPORTED_TEXT
            logger.warning("cookie_upload_failed", error_type="UnsupportedCookieDomainsError")
        except InvalidCookieFileError:
            text = COOKIE_INVALID_TEXT
            logger.warning("cookie_upload_failed", error_type="InvalidCookieFileError")
        except CookieStoreUnavailableError:
            text = COOKIE_STORE_UNAVAILABLE_TEXT
            logger.error("cookie_upload_failed", error_type="CookieStoreUnavailableError")
        except CookieStoreWriteError as exc:
            text = COOKIE_UPDATE_FAILED_TEXT
            logger.error("cookie_upload_failed", error_type=type(exc).__name__)
        except Exception as exc:
            text = COOKIE_UPDATE_FAILED_TEXT
            logger.error("cookie_upload_failed", error_type=type(exc).__name__)
        else:
            await state.clear()
            text = _render_cookie_update_summary(summary, health_by_provider)
            logger.info(
                "cookie_upload_verified",
                uploaded_record_count=summary.uploaded_record_count,
                new_canonical_record_count=summary.new_canonical_record_count,
            )
            if recovery_service is not None:
                total_requeued = 0
                for provider in summary.services:
                    try:
                        remediated = await recovery_service.remediate_cookies(provider)
                    except Exception as exc:
                        await logger.aerror(
                            "cookie_remediation_failed",
                            provider=provider.value,
                            error_type=type(exc).__name__,
                        )
                        continue
                    total_requeued += remediated.requeued
                    await logger.ainfo(
                        "cookie_remediation_completed",
                        provider=provider.value,
                        discovered=remediated.discovered,
                        requeued=remediated.requeued,
                    )
                if total_requeued:
                    await message.answer(
                        f"🔁 {total_requeued} درخواست قبلی مرتبط دوباره در صف قرار گرفت."
                    )
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
    return await _authorize_private_admin_message(
        message,
        state,
        settings,
        COOKIE_PRIVATE_CHAT_REQUIRED_TEXT,
    )


async def _authorize_private_admin_message(
    message: Message,
    state: FSMContext,
    settings: Settings,
    private_required_text: str,
) -> bool:
    if not await _authorize_message(message, state, settings):
        return False
    if message.chat.type == "private":
        return True
    await state.clear()
    await message.answer(private_required_text)
    return False


def _is_admin_user(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.telegram.admin_ids


_LOGGER_PROBE_LABELS = {
    DestinationProbeOutcome.OK: "✅ آزمایش موفق بود",
    DestinationProbeOutcome.NOT_CHANNEL: "❌ این شناسه کانال نیست",
    DestinationProbeOutcome.BOT_NOT_MEMBER: "❌ ربات عضو کانال نیست",
    DestinationProbeOutcome.FORBIDDEN: "❌ ربات اجازه ارسال در کانال را ندارد",
    DestinationProbeOutcome.UNREACHABLE: "⏳ کانال در دسترس نبود",
    DestinationProbeOutcome.AMBIGUOUS: "⚠️ نتیجه آزمایش نامشخص است",
}


def _render_logger_destinations(
    destinations: tuple[LoggerDestination, ...],
    *,
    logger_enabled: bool,
) -> str:
    header = LOGGER_MENU_TEXT
    if not logger_enabled:
        header += f"\n{LOGGER_DISABLED_TEXT}"
    if not destinations:
        return f"{header}\n\n{LOGGER_EMPTY_TEXT}"
    lines = [header, ""]
    for index, destination in enumerate(destinations, 1):
        ownership = _logger_ownership_label(destination)
        state = "فعال" if destination.enabled else "غیرفعال"
        health = _logger_health_label(destination.health)
        lines.append(
            f"{index}. {destination.chat_id}\n"
            f"   مالکیت: {ownership} · وضعیت: {state} · سلامت: {health}"
        )
    return "\n".join(lines)


def _logger_ownership_label(destination: LoggerDestination) -> str:
    from telegram_media_bot.domain.audit import LoggerDestinationSource

    config = LoggerDestinationSource.CONFIG in destination.ownership
    runtime = LoggerDestinationSource.RUNTIME in destination.ownership
    if config and runtime:
        return "پیکربندی + مدیر"
    if config:
        return "از پیکربندی"
    return "مدیر"


def _logger_health_label(health: LoggerDestinationHealth) -> str:
    labels = {
        LoggerDestinationHealth.ACTIVE: "فعال",
        LoggerDestinationHealth.UNREACHABLE: "دسترس‌ناپذیر",
        LoggerDestinationHealth.FORBIDDEN: "ممنوع",
        LoggerDestinationHealth.DISABLED: "غیرفعال",
    }
    return labels[health]


def _parse_logger_callback(data: str) -> tuple[str, int | None] | None:
    prefix = "adm:lg:"
    if not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    if rest in {"noop", "refresh"}:
        return rest, None
    if ":" not in rest:
        return None
    action, raw_id = rest.rsplit(":", 1)
    if action not in {"test", "enable", "disable", "remove", "confirm", "cancel"}:
        return None
    try:
        return action, int(raw_id)
    except ValueError:
        return None


async def _handle_logger_channel_action(
    callback: CallbackQuery,
    audit_admin: LoggerDestinationAdminService,
    *,
    logger_enabled: bool,
    action: str,
    chat_id: int,
) -> None:
    message = callback.message
    assert isinstance(message, Message)
    if action == "test":
        await callback.answer(LOGGER_TEST_PROGRESS_TEXT)
        await _edit_logger_message(message, LOGGER_TEST_PROGRESS_TEXT)
        destination, result = await audit_admin.probe(chat_id)
        del destination
        await _send_logger_list(
            message,
            audit_admin,
            logger_enabled=logger_enabled,
            status_line=f"{chat_id}: {_LOGGER_PROBE_LABELS[result.outcome]}",
        )
        return
    if action == "enable":
        await asyncio.to_thread(audit_admin.set_enabled, chat_id, True)
        await callback.answer()
    elif action == "disable":
        await asyncio.to_thread(audit_admin.set_enabled, chat_id, False)
        await callback.answer()
    elif action == "confirm":
        try:
            removed = await asyncio.to_thread(audit_admin.remove, chat_id)
        except ConfigOwnedLoggerChannelError:
            await callback.answer(LOGGER_CONFIG_OWNED_TEXT, show_alert=True)
            await _send_logger_list(message, audit_admin, logger_enabled=logger_enabled)
            return
        await callback.answer(LOGGER_REMOVED_TEXT if removed else LOGGER_NOT_FOUND_TEXT)
    elif action == "remove":
        await callback.answer()
        destinations = await asyncio.to_thread(audit_admin.list)
        await _edit_logger_message(
            message,
            _render_logger_destinations(destinations, logger_enabled=logger_enabled),
            reply_markup=build_admin_logger_inline_keyboard(destinations, confirm_chat_id=chat_id),
        )
        return
    elif action == "cancel":
        await callback.answer()
    else:
        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        return
    await _send_logger_list(message, audit_admin, logger_enabled=logger_enabled)


async def _send_logger_list(
    message: Message,
    audit_admin: LoggerDestinationAdminService,
    *,
    logger_enabled: bool,
    status_line: str | None = None,
    probe_chat_id: int | None = None,
) -> None:
    destinations = await asyncio.to_thread(audit_admin.list)
    text = _render_logger_destinations(destinations, logger_enabled=logger_enabled)
    if status_line is not None:
        text = f"{status_line}\n\n{text}"
    if probe_chat_id is not None:
        destination, result = await audit_admin.probe(probe_chat_id)
        del destination
        text = f"{probe_chat_id}: {_LOGGER_PROBE_LABELS[result.outcome]}\n\n{text}"
    await _edit_logger_message(
        message,
        text,
        reply_markup=build_admin_logger_inline_keyboard(destinations),
    )


async def _edit_logger_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return


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
    summary: CookieUpdateSummary,
    health_by_provider: dict[CookieService, ProviderCookieHealth],
) -> str:
    labels = "، ".join(_COOKIE_SERVICE_LABELS[service] for service in summary.services)
    provider_lines = [
        (
            f"{_COOKIE_SERVICE_LABELS[service]}: "
            f"{summary.record_count(service)} رکورد، "
            f"{health_by_provider[service].status.value.upper() if service in health_by_provider else 'UNVERIFIED'}"
        )
        for service in summary.services
    ]
    return (
        "✅ فایل کوکی با موفقیت به‌روزرسانی شد.\n"
        f"سرویس‌های تشخیص‌داده‌شده: {labels}\n"
        f"رکوردهای جایگزین‌شده: {summary.replaced}\n"
        f"رکوردهای افزوده‌شده: {summary.added}\n" + "\n".join(provider_lines)
    )


async def _edit_cookie_health_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
        raise
