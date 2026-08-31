from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.domain.audit import AuditCategory, AuditEventType, AuditSeverity
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository


def test_disabled_service_creates_no_durable_event(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    service = AuditService(repository, enabled=False)

    assert (
        service.emit(
            event_type=AuditEventType.SYSTEM_HEALTH,
            category=AuditCategory.SYSTEM,
            severity=AuditSeverity.INFO,
            correlation_id="system-1",
            message="healthy",
        )
        == 0
    )
    assert repository.health_snapshot().pending_effects == 0


def test_service_sanitizes_before_persistence_without_removing_user_id(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    service = AuditService(repository, enabled=True)

    assert (
        service.emit(
            event_type=AuditEventType.TERMINAL_OPERATIONAL_ERROR,
            category=AuditCategory.ERROR,
            severity=AuditSeverity.ERROR,
            correlation_id="request-1",
            message="Authorization: Bearer sensitive-value",
            telegram_user_id=42,
            provider="instagram",
        )
        == 1
    )
    item = repository.claim_pending()[0]
    assert item.event.telegram_user_id == 42
    assert "sensitive-value" not in item.event.message
    assert "redacted" in item.event.message
