"""Transport-neutral durable audit outbox processor (T027)."""

from __future__ import annotations

from telegram_media_bot.application.ports.audit import AuditDeliveryPort, AuditRepository
from telegram_media_bot.application.services.audit_sanitizer import safe_failure_class
from telegram_media_bot.domain.audit import AuditDeliveryOutcome


class AuditOutboxProcessor:
    def __init__(self, repository: AuditRepository, delivery: AuditDeliveryPort) -> None:
        self._repository = repository
        self._delivery = delivery

    async def dispatch_batch(self, *, limit: int = 20) -> int:
        completed = 0
        self._repository.recover_expired_leases()
        for item in self._repository.claim_pending(limit=limit):
            if not self._repository.mark_send_started(item):
                continue
            try:
                result = await self._delivery.deliver(item)
            except Exception as exc:
                # The external-send boundary was crossed. A generic exception cannot prove that
                # Telegram created nothing, so quarantine instead of retrying.
                self._repository.mark_uncertain(item, safe_failure_class(exc))
                continue
            failure = result.failure_class or result.outcome.value
            if result.outcome is AuditDeliveryOutcome.SUCCEEDED:
                self._repository.mark_succeeded(item)
                completed += 1
            elif result.outcome is AuditDeliveryOutcome.RETRYABLE:
                self._repository.mark_retryable(item, failure)
            elif result.outcome is AuditDeliveryOutcome.FAILED_TERMINAL:
                self._repository.mark_terminal(item, failure)
            else:
                self._repository.mark_uncertain(item, failure)
        return completed


__all__ = ["AuditOutboxProcessor"]
