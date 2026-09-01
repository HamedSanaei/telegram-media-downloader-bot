"""RC2: privacy disclosure is informational and never blocks downloads."""

from __future__ import annotations

from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.logger_privacy import (
    LOGGER_PRIVACY_DISCLOSURE_FA,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository

EXPECTED_DISCLOSURE = (
    "برخی درخواست‌های دانلود ممکن است برای امنیت، پشتیبانی و بررسی خطا در کانال "
    "خصوصی عملیاتی ثبت شوند. اطلاعات ورود، رمز عبور، کد دو مرحله‌ای و داده‌های "
    "حساس در لاگر ثبت نمی‌شوند."
)


def test_disclosure_matches_the_approved_persian_text_exactly() -> None:
    assert LOGGER_PRIVACY_DISCLOSURE_FA == EXPECTED_DISCLOSURE


def test_disclosure_is_informational_and_mentions_that_credentials_are_not_logged() -> None:
    text = LOGGER_PRIVACY_DISCLOSURE_FA
    # It describes the mirror and the credential exclusion; it must not demand
    # any action or contain any acknowledgement wording.
    assert "عملیاتی" in text
    assert "رمز عبور" in text
    assert "ثبت نمی‌شوند" in text
    assert "موافقم" not in text


def test_legacy_acknowledgement_persistence_remains_compatible(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    audit = AuditService(repository, enabled=True)

    # The deprecated acknowledgement API still works and stores rows durably,
    # even though the runtime acceptance path never consults it.
    assert audit.acknowledge_privacy(42, "logger-v1")
    assert audit.has_privacy_acknowledgement(42, "logger-v1")

    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    assert restarted.has_privacy_acknowledgement(42, "logger-v1")
    assert restarted.acknowledge_privacy(42, "logger-v1") is False
