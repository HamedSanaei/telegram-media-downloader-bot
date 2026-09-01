"""Durable mirroring of concrete Telegram download outputs."""

from __future__ import annotations

from typing import cast

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEventType,
    AuditSeverity,
    TelegramSourceReference,
)
from telegram_media_bot.domain.models import DeliveryItemStatus, JobId, JobStatus


class DeliveredOutputAuditService:
    """Turn durable delivery receipts into one replay-safe logger copy intent."""

    def __init__(
        self,
        audit: AuditService,
        jobs: JobRepository,
        *,
        enabled: bool,
    ) -> None:
        self._audit = audit
        self._jobs = jobs
        self._enabled = enabled

    def prepare(self, job_id: JobId) -> bool:
        """Persist recovery intent before the first user-facing Telegram send."""
        if not self._enabled or not self._audit.has_usable_destination():
            return False
        return self._audit.prepare_delivery_output(str(job_id))

    def finalize(self, job_id: JobId) -> bool:
        """Enqueue the actual delivered messages, never the original submission."""
        if not self._enabled or not self._audit.delivery_output_pending(str(job_id)):
            return False
        record = self._jobs.get_job(job_id)
        if record is None or not record.status.terminal:
            return False
        if record.status is not JobStatus.SUCCEEDED:
            return self._audit.complete_delivery_output(str(job_id))

        delivered = tuple(
            item
            for item in self._jobs.delivery_items(job_id)
            if item.status is DeliveryItemStatus.DELIVERED and item.recipient_message_id is not None
        )
        if not delivered:
            # No durable Telegram identity means recovery cannot safely guess or re-upload.
            return self._audit.complete_delivery_output(str(job_id))

        identity = f"delivery-output:{job_id}"
        providers = {item.provider.value for item in delivered}
        provider = next(iter(providers)) if len(providers) == 1 else "mixed"
        message_ids = tuple(
            dict.fromkeys(cast(int, item.recipient_message_id) for item in delivered)
        )
        self._audit.emit(
            event_type=AuditEventType.DOWNLOAD_OUTPUT_DELIVERED,
            category=AuditCategory.USER_SUBMISSION,
            severity=AuditSeverity.INFO,
            correlation_id=identity,
            message="Delivered Telegram download output",
            telegram_user_id=record.user_id,
            job_id=str(job_id),
            content_type="download_output",
            provider=provider,
            source=TelegramSourceReference(
                chat_id=record.chat_id,
                message_ids=message_ids,
            ),
            occurred_at=record.updated_at,
            idempotency_key=identity,
        )
        return self._audit.complete_delivery_output(str(job_id))

    def reconcile_pending(self, *, limit: int = 50) -> int:
        """Recover the post-completion/pre-enqueue crash window in bounded passes."""
        completed = 0
        for raw_job_id in self._audit.pending_delivery_outputs(limit=limit):
            completed += int(self.finalize(JobId(raw_job_id)))
        return completed


__all__ = ["DeliveredOutputAuditService"]
