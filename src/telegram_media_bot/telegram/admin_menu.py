from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from telegram_media_bot.domain.analytics import UsageReportPeriod
from telegram_media_bot.domain.audit import LoggerDestination, LoggerDestinationHealth

ADMIN_DOWNLOAD_BUTTON = "⬇️ دانلود مدیا"
ADMIN_WEEKLY_REPORT_BUTTON = "📊 گزارش هفتگی"
ADMIN_MONTHLY_REPORT_BUTTON = "📈 گزارش ماهانه"
ADMIN_FULL_REPORT_BUTTON = "📋 گزارش کامل استفاده"
ADMIN_COOKIE_MANAGEMENT_BUTTON = "🍪 مدیریت کوکی‌ها"  # noqa: RUF001
ADMIN_COOKIE_HEALTH_BUTTON = "🍪 سلامت کوکی‌ها"  # noqa: RUF001
ADMIN_COOKIE_UPLOAD_BUTTON = "⬆️ بارگذاری cookies.txt"
ADMIN_COOKIE_DOWNLOAD_BUTTON = "⬇️ دریافت cookies.txt کامل"
ADMIN_COOKIE_HEALTH_REFRESH_BUTTON = "🔄 تازه‌سازی وضعیت"
ADMIN_REFRESH_MENU_BUTTON = "🔄 تازه‌سازی منو"
ADMIN_CANCEL_DOWNLOAD_BUTTON = "❌ لغو"
ADMIN_BACK_TO_MENU_BUTTON = "🏠 منوی مدیریت"
ADMIN_REFRESH_REPORT_BUTTON = "🔄 تازه‌سازی گزارش"
ADMIN_LOGGER_BUTTON = "🧾 کانال‌های لاگر"
ADMIN_LOGGER_ADD_BUTTON = "➕ افزودن کانال"  # noqa: RUF001
ADMIN_LOGGER_REFRESH_BUTTON = "🔄 تازه‌سازی وضعیت کانال‌ها"  # noqa: RUF001
ADMIN_LOGGER_TEST_BUTTON = "🔍 آزمایش"
ADMIN_LOGGER_ENABLE_BUTTON = "✅ فعال‌سازی"
ADMIN_LOGGER_DISABLE_BUTTON = "⏸️ غیرفعال‌سازی"
ADMIN_LOGGER_REMOVE_BUTTON = "🗑️ حذف"
ADMIN_LOGGER_REMOVE_CONFIRM_BUTTON = "🗑️ تأیید حذف"
ADMIN_LOGGER_REMOVE_CANCEL_BUTTON = "❌ انصراف از حذف"

ADMIN_MANAGEMENT_BUTTONS = frozenset(
    {
        ADMIN_DOWNLOAD_BUTTON,
        ADMIN_WEEKLY_REPORT_BUTTON,
        ADMIN_MONTHLY_REPORT_BUTTON,
        ADMIN_FULL_REPORT_BUTTON,
        ADMIN_COOKIE_MANAGEMENT_BUTTON,
        ADMIN_COOKIE_HEALTH_BUTTON,
        ADMIN_COOKIE_UPLOAD_BUTTON,
        ADMIN_COOKIE_DOWNLOAD_BUTTON,
        ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
        ADMIN_REFRESH_MENU_BUTTON,
        ADMIN_CANCEL_DOWNLOAD_BUTTON,
        ADMIN_BACK_TO_MENU_BUTTON,
        ADMIN_LOGGER_BUTTON,
        ADMIN_LOGGER_ADD_BUTTON,
    }
)


class AdminDownloadState(StatesGroup):
    awaiting_url = State()


class AdminCookieState(StatesGroup):
    awaiting_upload = State()


class AdminLoggerState(StatesGroup):
    awaiting_add_chat_id = State()


def build_admin_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_DOWNLOAD_BUTTON)],
            [
                KeyboardButton(text=ADMIN_WEEKLY_REPORT_BUTTON),
                KeyboardButton(text=ADMIN_MONTHLY_REPORT_BUTTON),
            ],
            [KeyboardButton(text=ADMIN_FULL_REPORT_BUTTON)],
            [
                KeyboardButton(text=ADMIN_COOKIE_HEALTH_BUTTON),
                KeyboardButton(text=ADMIN_COOKIE_MANAGEMENT_BUTTON),
            ],
            [KeyboardButton(text=ADMIN_LOGGER_BUTTON)],
            [KeyboardButton(text=ADMIN_REFRESH_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
    )


def build_admin_cookie_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_COOKIE_HEALTH_BUTTON)],
            [KeyboardButton(text=ADMIN_COOKIE_UPLOAD_BUTTON)],
            [KeyboardButton(text=ADMIN_COOKIE_DOWNLOAD_BUTTON)],
            [KeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
    )


def build_admin_cookie_health_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_COOKIE_HEALTH_REFRESH_BUTTON)],
            [KeyboardButton(text=ADMIN_COOKIE_UPLOAD_BUTTON)],
            [KeyboardButton(text=ADMIN_COOKIE_DOWNLOAD_BUTTON)],
            [KeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
    )


def build_admin_cookie_health_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
                    callback_data="adm:ch:refresh",
                ),
            ],
            [InlineKeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON, callback_data="adm:menu")],
        ]
    )


def build_admin_download_prompt_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=ADMIN_CANCEL_DOWNLOAD_BUTTON),
                KeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
    )


def build_admin_report_inline_keyboard(
    report_type: UsageReportPeriod,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ADMIN_REFRESH_REPORT_BUTTON,
                    callback_data=f"adm:rpt:{_period_code(report_type)}:refresh",
                )
            ],
            [InlineKeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON, callback_data="adm:menu")],
        ]
    )


def build_admin_logger_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_LOGGER_ADD_BUTTON)],
            [KeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
    )


_HEALTH_LABELS = {
    LoggerDestinationHealth.ACTIVE: "فعال",
    LoggerDestinationHealth.UNREACHABLE: "دسترس‌ناپذیر",
    LoggerDestinationHealth.FORBIDDEN: "ممنوع",
    LoggerDestinationHealth.DISABLED: "غیرفعال",
}


def build_admin_logger_inline_keyboard(
    destinations: tuple[LoggerDestination, ...],
    *,
    confirm_chat_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for destination in destinations:
        label = f"{destination.chat_id} · {_HEALTH_LABELS[destination.health]}"
        actions: list[InlineKeyboardButton] = []
        actions.append(
            InlineKeyboardButton(
                text=ADMIN_LOGGER_TEST_BUTTON,
                callback_data=f"adm:lg:test:{destination.chat_id}",
            )
        )
        if destination.runtime_owned:
            if destination.enabled:
                actions.append(
                    InlineKeyboardButton(
                        text=ADMIN_LOGGER_DISABLE_BUTTON,
                        callback_data=f"adm:lg:disable:{destination.chat_id}",
                    )
                )
            else:
                actions.append(
                    InlineKeyboardButton(
                        text=ADMIN_LOGGER_ENABLE_BUTTON,
                        callback_data=f"adm:lg:enable:{destination.chat_id}",
                    )
                )
            if destination.runtime_owned and not destination.config_owned:
                if confirm_chat_id == destination.chat_id:
                    actions.append(
                        InlineKeyboardButton(
                            text=ADMIN_LOGGER_REMOVE_CONFIRM_BUTTON,
                            callback_data=f"adm:lg:confirm:{destination.chat_id}",
                        )
                    )
                    actions.append(
                        InlineKeyboardButton(
                            text=ADMIN_LOGGER_REMOVE_CANCEL_BUTTON,
                            callback_data=f"adm:lg:cancel:{destination.chat_id}",
                        )
                    )
                else:
                    actions.append(
                        InlineKeyboardButton(
                            text=ADMIN_LOGGER_REMOVE_BUTTON,
                            callback_data=f"adm:lg:remove:{destination.chat_id}",
                        )
                    )
        rows.append([InlineKeyboardButton(text=label, callback_data="adm:lg:noop"), *actions])
    rows.append(
        [
            InlineKeyboardButton(text=ADMIN_LOGGER_REFRESH_BUTTON, callback_data="adm:lg:refresh"),
            InlineKeyboardButton(text=ADMIN_LOGGER_ADD_BUTTON, callback_data="adm:lg:add"),
        ]
    )
    rows.append([InlineKeyboardButton(text=ADMIN_BACK_TO_MENU_BUTTON, callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _period_code(report_type: UsageReportPeriod) -> str:
    return {
        UsageReportPeriod.WEEKLY: "w",
        UsageReportPeriod.MONTHLY: "m",
        UsageReportPeriod.FULL: "full",
    }[report_type]
