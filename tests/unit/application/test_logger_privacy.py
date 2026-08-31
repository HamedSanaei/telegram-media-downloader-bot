# ruff: noqa: RUF001
from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.logger_privacy import (
    LOGGER_PRIVACY_NOTICE_FA,
    LoggerPrivacyService,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository

EXPECTED_NOTICE = (
    "برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود می‌فرستید "
    "ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ "
    "با ادامهٔ استفاده موافقت می‌کنید."
)


def test_notice_matches_the_approved_persian_policy_exactly() -> None:
    assert LOGGER_PRIVACY_NOTICE_FA == EXPECTED_NOTICE


def test_acknowledgement_is_versioned_durable_and_not_requested_repeatedly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    audit = AuditService(repository, enabled=True)
    version_one = LoggerPrivacyService(audit, enabled=True, policy_version="logger-v1")

    assert version_one.requires_acknowledgement(42)
    assert version_one.acknowledge(42)
    assert not version_one.acknowledge(42)
    assert not version_one.requires_acknowledgement(42)

    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    restarted_audit = AuditService(restarted, enabled=True)
    assert not LoggerPrivacyService(
        restarted_audit, enabled=True, policy_version="logger-v1"
    ).requires_acknowledgement(42)
    assert LoggerPrivacyService(
        restarted_audit, enabled=True, policy_version="logger-v2"
    ).requires_acknowledgement(42)


def test_privacy_gate_is_inactive_without_mirror_or_destination(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    audit = AuditService(repository, enabled=True)

    assert not LoggerPrivacyService(
        audit, enabled=True, policy_version="logger-v1"
    ).requires_acknowledgement(42)
    repository.reconcile_config((-1001234567890,))
    assert not LoggerPrivacyService(
        audit, enabled=False, policy_version="logger-v1"
    ).requires_acknowledgement(42)
