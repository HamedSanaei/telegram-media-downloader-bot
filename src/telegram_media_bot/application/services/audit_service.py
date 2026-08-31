"""Application construction boundary for typed, sanitized audit events (T026)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from telegram_media_bot.application.ports.audit import AuditRepository
from telegram_media_bot.application.services.audit_sanitizer import sanitize_audit_message
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    LoggerDestinationHealth,
    TelegramSourceReference,
)


class AuditService:
    def __init__(self, repository: AuditRepository, *, enabled: bool) -> None:
        self._repository = repository
        self._enabled = enabled

    def has_usable_destination(self) -> bool:
        if not self._enabled:
            return False
        return any(
            destination.enabled
            and destination.health
            in {LoggerDestinationHealth.ACTIVE, LoggerDestinationHealth.UNREACHABLE}
            for destination in self._repository.list_destinations()
        )

    def has_privacy_acknowledgement(self, user_id: int, policy_version: str) -> bool:
        return self._repository.has_privacy_acknowledgement(user_id, policy_version)

    def acknowledge_privacy(self, user_id: int, policy_version: str) -> bool:
        return self._repository.acknowledge_privacy(user_id, policy_version)

    def emit(
        self,
        *,
        event_type: AuditEventType,
        category: AuditCategory,
        severity: AuditSeverity,
        correlation_id: str,
        message: object,
        telegram_user_id: int | None = None,
        update_id: int | None = None,
        job_id: str | None = None,
        content_type: str | None = None,
        provider: str | None = None,
        source: TelegramSourceReference | None = None,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        if not self._enabled:
            return 0
        identity = (
            "\0".join((event_type.value, idempotency_key))
            if idempotency_key is not None
            else "\0".join(
                (
                    event_type.value,
                    correlation_id,
                    str(update_id or ""),
                    str(job_id or ""),
                    ",".join(str(item) for item in source.message_ids) if source else "",
                )
            )
        )
        event = AuditEvent(
            event_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            event_type=event_type,
            category=category,
            severity=severity,
            occurred_at=occurred_at or datetime.now(UTC),
            correlation_id=correlation_id,
            message=sanitize_audit_message(message),
            telegram_user_id=telegram_user_id,
            update_id=update_id,
            job_id=job_id,
            content_type=content_type,
            provider=provider,
            source=source,
        )
        return self._repository.enqueue(event)

    def extend_submission_source(self, source: TelegramSourceReference) -> int:
        if not self._enabled or source.media_group_id is None:
            return 0
        return self._repository.extend_submission_source(source)


__all__ = ["AuditService"]
