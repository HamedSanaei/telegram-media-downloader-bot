# ruff: noqa: RUF001
"""Versioned privacy acknowledgement gate for submission mirroring (T031)."""

from __future__ import annotations

from telegram_media_bot.application.services.audit_service import AuditService

LOGGER_PRIVACY_NOTICE_FA = (
    "برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود می‌فرستید "
    "ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ "
    "با ادامهٔ استفاده موافقت می‌کنید."
)


class LoggerPrivacyService:
    def __init__(
        self,
        audit: AuditService,
        *,
        enabled: bool,
        policy_version: str,
    ) -> None:
        self._audit = audit
        self._enabled = enabled
        self.policy_version = policy_version

    def requires_acknowledgement(self, user_id: int) -> bool:
        return (
            self._enabled
            and self._audit.has_usable_destination()
            and not self._audit.has_privacy_acknowledgement(user_id, self.policy_version)
        )

    def acknowledge(self, user_id: int) -> bool:
        if not self._enabled:
            return False
        return self._audit.acknowledge_privacy(user_id, self.policy_version)


__all__ = ["LOGGER_PRIVACY_NOTICE_FA", "LoggerPrivacyService"]
# ruff: noqa: RUF001
