"""Accepted Telegram download-submission mirroring policy (T030)."""

from __future__ import annotations

from datetime import datetime

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEventType,
    AuditSeverity,
    TelegramSourceReference,
)


class AcceptedSubmissionAuditService:
    """Create one durable mirror intent only after application acceptance."""

    def __init__(
        self,
        audit: AuditService,
        *,
        enabled: bool,
        privacy_notice_version: str | None = None,
    ) -> None:
        self._audit = audit
        self._enabled = enabled
        self._privacy_notice_version = privacy_notice_version

    def record_accepted(
        self,
        *,
        source: TelegramSourceReference,
        telegram_user_id: int,
        update_id: int | None,
        job_id: str,
        content_type: str,
        provider: str | None,
        occurred_at: datetime,
    ) -> int:
        if (
            not self._enabled
            or not self._audit.has_usable_destination()
            or (
                self._privacy_notice_version is not None
                and not self._audit.has_privacy_acknowledgement(
                    telegram_user_id, self._privacy_notice_version
                )
            )
        ):
            return 0
        identity = _submission_identity(source, update_id)
        return self._audit.emit(
            event_type=AuditEventType.USER_SUBMISSION_RECEIVED,
            category=AuditCategory.USER_SUBMISSION,
            severity=AuditSeverity.INFO,
            correlation_id=identity,
            message="Accepted Telegram download submission",
            telegram_user_id=telegram_user_id,
            update_id=update_id,
            job_id=job_id,
            content_type=content_type,
            provider=provider,
            source=source,
            occurred_at=occurred_at,
            idempotency_key=identity,
        )

    def observe_media_group_member(self, source: TelegramSourceReference) -> int:
        """Extend an already-accepted logical album; never creates acceptance itself."""
        if not self._enabled or not self._audit.has_usable_destination():
            return 0
        return self._audit.extend_submission_source(source)


def _submission_identity(source: TelegramSourceReference, update_id: int | None) -> str:
    if source.media_group_id is not None:
        return f"submission:{source.chat_id}:group:{source.media_group_id}"
    if update_id is not None:
        return f"submission:update:{update_id}"
    return f"submission:{source.chat_id}:message:{source.message_ids[0]}"


__all__ = ["AcceptedSubmissionAuditService"]
