from __future__ import annotations

from typing import TypedDict

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from telegram_media_bot.application.services.private_media_policy import (
    PrivateMediaRequestAuthorization,
)
from telegram_media_bot.domain.credential_resolution import ContentAccessScope, CredentialPolicy
from telegram_media_bot.domain.subscriptions import EntitlementSnapshot

PUBLIC_DOWNLOAD_BUTTON = "دانلود عمومی"
SUBSCRIPTION_BUTTON = "نوع و وضعیت اشتراک"
BUY_VIP_BUTTON = "خرید Premium Downloader / VIP"
PRIVATE_DOWNLOAD_BUTTON = "دانلود خصوصی VIP"
INSTAGRAM_CONNECT_BUTTON = "اتصال شخصی اینستاگرام"
INSTAGRAM_STATUS_BUTTON = "وضعیت اتصال اینستاگرام"
INSTAGRAM_DISCONNECT_BUTTON = "قطع اتصال اینستاگرام"
USER_MENU_BUTTON = "بازگشت به منوی اصلی"
USER_REFRESH_BUTTON = "تازه‌سازی منوی کاربر"

PUBLIC_DOWNLOAD_PROMPT = "لینک رسانهٔ عمومی را از هر سرویس پشتیبانی‌شده ارسال کنید."
PRIVATE_DOWNLOAD_PROMPT = (
    "لینک اینستاگرام را ارسال کنید. دانلود خصوصی فقط با اشتراک VIP فعال و اتصال شخصی شما "
    "انجام می‌شود؛ محتوا باید برای حساب شما از قبل قابل مشاهده باشد. "
    "ربات درخواست دنبال‌کردن یا دورزدن دسترسی انجام نمی‌دهد."
)
PRIVATE_INSTAGRAM_ONLY_TEXT = "دانلود خصوصی فقط برای لینک‌های پشتیبانی‌شدهٔ اینستاگرام در دسترس است."


class UserDownloadState(StatesGroup):
    public_url = State()
    private_url = State()


class PrivateJobFields(TypedDict, total=False):
    entitlement_snapshot: EntitlementSnapshot
    credential_policy: CredentialPolicy
    content_access_scope: ContentAccessScope
    user_credential_generation: int


def private_job_fields(authorization: PrivateMediaRequestAuthorization | None) -> PrivateJobFields:
    if authorization is None:
        return {}
    return {
        "entitlement_snapshot": authorization.entitlement_snapshot,
        "credential_policy": CredentialPolicy.USER_ONLY,
        "content_access_scope": ContentAccessScope.USER_RESTRICTED,
        "user_credential_generation": authorization.user_credential_generation,
    }


def build_user_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=PUBLIC_DOWNLOAD_BUTTON),
                KeyboardButton(text=PRIVATE_DOWNLOAD_BUTTON),
            ],
            [KeyboardButton(text=SUBSCRIPTION_BUTTON), KeyboardButton(text=BUY_VIP_BUTTON)],
            [
                KeyboardButton(text=INSTAGRAM_CONNECT_BUTTON),
                KeyboardButton(text=INSTAGRAM_STATUS_BUTTON),
            ],
            [KeyboardButton(text=INSTAGRAM_DISCONNECT_BUTTON)],
            [KeyboardButton(text=USER_MENU_BUTTON), KeyboardButton(text=USER_REFRESH_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
