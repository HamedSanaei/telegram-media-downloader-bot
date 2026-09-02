from __future__ import annotations

from telegram_media_bot.domain.analytics import UsageReportPeriod
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
    ADMIN_LOGGER_BUTTON,
    ADMIN_MONTHLY_REPORT_BUTTON,
    ADMIN_REFRESH_MENU_BUTTON,
    ADMIN_VIP_BUTTON,
    ADMIN_WEEKLY_REPORT_BUTTON,
    build_admin_cookie_health_inline_keyboard,
    build_admin_cookie_health_keyboard,
    build_admin_cookie_keyboard,
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
        ADMIN_COOKIE_HEALTH_BUTTON,
        ADMIN_COOKIE_MANAGEMENT_BUTTON,
        ADMIN_LOGGER_BUTTON,
        ADMIN_VIP_BUTTON,
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


def test_admin_cookie_management_keyboard_is_persistent_and_complete() -> None:
    keyboard = build_admin_cookie_keyboard()

    assert [button.text for row in keyboard.keyboard for button in row] == [
        ADMIN_COOKIE_HEALTH_BUTTON,
        ADMIN_COOKIE_UPLOAD_BUTTON,
        ADMIN_COOKIE_DOWNLOAD_BUTTON,
        ADMIN_BACK_TO_MENU_BUTTON,
    ]
    assert keyboard.is_persistent is True
    assert keyboard.selective is True


def test_admin_cookie_health_keyboards_are_complete() -> None:
    health = build_admin_cookie_health_keyboard()
    assert [button.text for row in health.keyboard for button in row] == [
        ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
        ADMIN_COOKIE_UPLOAD_BUTTON,
        ADMIN_COOKIE_DOWNLOAD_BUTTON,
        ADMIN_BACK_TO_MENU_BUTTON,
    ]
    inline = build_admin_cookie_health_inline_keyboard()
    assert [button.callback_data for row in inline.inline_keyboard for button in row] == [
        "adm:ch:refresh",
        "adm:menu",
    ]
