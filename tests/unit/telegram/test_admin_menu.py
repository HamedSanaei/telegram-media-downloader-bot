from __future__ import annotations

from telegram_media_bot.domain.analytics import UsageReportPeriod
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_BACK_TO_MENU_BUTTON,
    ADMIN_CANCEL_DOWNLOAD_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    ADMIN_FULL_REPORT_BUTTON,
    ADMIN_MONTHLY_REPORT_BUTTON,
    ADMIN_REFRESH_MENU_BUTTON,
    ADMIN_WEEKLY_REPORT_BUTTON,
    build_admin_download_prompt_keyboard,
    build_admin_main_keyboard,
    build_admin_report_inline_keyboard,
)


def test_admin_main_keyboard_is_persistent_selective_and_complete() -> None:
    keyboard = build_admin_main_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == [
        ADMIN_DOWNLOAD_BUTTON,
        ADMIN_WEEKLY_REPORT_BUTTON,
        ADMIN_MONTHLY_REPORT_BUTTON,
        ADMIN_FULL_REPORT_BUTTON,
        ADMIN_REFRESH_MENU_BUTTON,
    ]
    assert keyboard.resize_keyboard is True
    assert keyboard.is_persistent is True
    assert keyboard.selective is True
    assert keyboard.one_time_keyboard is False


def test_admin_download_prompt_and_report_refresh_keyboards() -> None:
    prompt = build_admin_download_prompt_keyboard()
    report = build_admin_report_inline_keyboard(UsageReportPeriod.WEEKLY)

    assert [button.text for button in prompt.keyboard[0]] == [
        ADMIN_CANCEL_DOWNLOAD_BUTTON,
        ADMIN_BACK_TO_MENU_BUTTON,
    ]
    assert report.inline_keyboard[0][0].callback_data == "adm:rpt:w:refresh"
    assert report.inline_keyboard[1][0].callback_data == "adm:menu"
