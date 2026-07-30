from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from telegram_media_bot.domain.analytics import UsageReportPeriod

ADMIN_DOWNLOAD_BUTTON = "⬇️ دانلود مدیا"
ADMIN_WEEKLY_REPORT_BUTTON = "📊 گزارش هفتگی"
ADMIN_MONTHLY_REPORT_BUTTON = "📈 گزارش ماهانه"
ADMIN_FULL_REPORT_BUTTON = "📋 گزارش کامل استفاده"
ADMIN_REFRESH_MENU_BUTTON = "🔄 تازه‌سازی منو"
ADMIN_CANCEL_DOWNLOAD_BUTTON = "❌ لغو"
ADMIN_BACK_TO_MENU_BUTTON = "🏠 منوی مدیریت"
ADMIN_REFRESH_REPORT_BUTTON = "🔄 تازه‌سازی گزارش"

ADMIN_MANAGEMENT_BUTTONS = frozenset(
    {
        ADMIN_DOWNLOAD_BUTTON,
        ADMIN_WEEKLY_REPORT_BUTTON,
        ADMIN_MONTHLY_REPORT_BUTTON,
        ADMIN_FULL_REPORT_BUTTON,
        ADMIN_REFRESH_MENU_BUTTON,
        ADMIN_CANCEL_DOWNLOAD_BUTTON,
        ADMIN_BACK_TO_MENU_BUTTON,
    }
)


class AdminDownloadState(StatesGroup):
    awaiting_url = State()


def build_admin_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_DOWNLOAD_BUTTON)],
            [
                KeyboardButton(text=ADMIN_WEEKLY_REPORT_BUTTON),
                KeyboardButton(text=ADMIN_MONTHLY_REPORT_BUTTON),
            ],
            [KeyboardButton(text=ADMIN_FULL_REPORT_BUTTON)],
            [KeyboardButton(text=ADMIN_REFRESH_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
        one_time_keyboard=False,
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


def _period_code(report_type: UsageReportPeriod) -> str:
    return {
        UsageReportPeriod.WEEKLY: "w",
        UsageReportPeriod.MONTHLY: "m",
        UsageReportPeriod.FULL: "full",
    }[report_type]
