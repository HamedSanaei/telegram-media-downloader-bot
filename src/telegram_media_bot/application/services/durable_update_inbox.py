"""Durable Telegram inbound-update inbox.

Guards the invariant *"a Telegram update must be durably recorded before it is acknowledged"*.
The bot owns the polling loop that calls :meth:`record` for every fetched update *before* the
polling offset advances; this service persists the replayable payload and decides whether the
update needs handling now. Handler execution is at-least-once and side-effect-safe because it is
idempotent at the job layer (unique ids / idempotency keys already deduplicate created jobs).

Only ``update_id``, the update type, and attempt counts are ever logged or counted here. Replayable
payloads are stored in the protected runtime SQLite file and never written to logs.
"""

from __future__ import annotations

import structlog

from telegram_media_bot.application.ports.inbound_update_repository import (
    InboundUpdateRepository,
)
from telegram_media_bot.domain.inbound_updates import (
    InboundUpdate,
    UpdateProcessingState,
)

logger = structlog.get_logger(__name__)

#: Bounded handler retries for an update whose handler itself is permanently broken, so a
#: crash-looping handler cannot replay forever across every restart.
DEFAULT_MAX_PROCESSING_ATTEMPTS = 3


class DurableUpdateInbox:
    def __init__(
        self,
        repository: InboundUpdateRepository,
        *,
        max_processing_attempts: int = DEFAULT_MAX_PROCESSING_ATTEMPTS,
    ) -> None:
        self._repository = repository
        self._max_processing_attempts = max_processing_attempts

    def record(self, update_id: int, update_type: str, payload_json: str) -> InboundUpdate | None:
        """Durably record one delivered update.

        Returns the record to process now, or ``None`` when the update was already handled and
        must not be executed again.
        """
        record, newly = self._repository.persist(update_id, update_type, payload_json)
        if newly:
            logger.info(
                "telegram_update_persisted",
                update_id=update_id,
                update_type=update_type,
            )
            return record
        if record.state in {
            UpdateProcessingState.COMPLETED,
            UpdateProcessingState.TERMINAL_FAILURE,
        }:
            return None
        # A duplicate that is still pending may be reprocessed idempotently.
        return record

    def quarantine(self, update_id: int, update_type: str, error_category: str) -> bool:
        """Durably record an update that could not be serialized as a terminal, non-replayable row.

        This is the bounded fail-safe for a permanently malformed update: keeping it fresh would
        make Telegram redeliver it on every poll forever (a restart loop). Recording it here in
        TERMINAL_FAILURE state preserves the update_id for audit while marking it non-replayable,
        so the polling offset may advance and the bot keeps serving fresh traffic. Never loses or
        silently drops the update -- it is durably tracked with a structured log/status.

        Returns ``True`` when a new quarantined row was inserted, ``False`` if the update was
        already durably recorded (duplicate delivery).
        """
        newly = self._repository.persist_terminal(update_id, update_type, error_category)
        logger.warning(
            "telegram_update_serialization_failed",
            update_id=update_id,
            update_type=update_type,
            error_category=error_category,
            quarantined=True,
        )
        return newly

    def start_processing(self, record: InboundUpdate) -> InboundUpdate:
        updated = self._repository.transition(record.update_id, UpdateProcessingState.PROCESSING)
        return updated if updated is not None else record

    def mark_completed(self, record: InboundUpdate) -> None:
        logger.info("telegram_update_completed", update_id=record.update_id)
        self._repository.transition(record.update_id, UpdateProcessingState.COMPLETED)

    def handler_failed(self, record: InboundUpdate, *, error_category: str) -> InboundUpdate:
        """Handle a handler exception with bounded, non-looping replay.

        Failed updates stay pending for a later startup reconciliation until the retry bound is
        exhausted; beyond that they become terminal so a broken handler is never replayed forever.
        """
        attempt = record.processing_attempts + 1
        terminal = attempt >= self._max_processing_attempts
        if terminal:
            updated = self._repository.transition(
                record.update_id,
                UpdateProcessingState.TERMINAL_FAILURE,
                last_error_category=error_category,
                increment_attempts=True,
            )
            logger.warning(
                "telegram_update_terminal_failure",
                update_id=record.update_id,
                processing_attempts=attempt,
                error_category=error_category,
            )
            return updated if updated is not None else record
        updated = self._repository.transition(
            record.update_id,
            UpdateProcessingState.RECEIVED,
            last_error_category=error_category,
            increment_attempts=True,
        )
        logger.warning(
            "telegram_update_retry",
            update_id=record.update_id,
            processing_attempts=attempt,
            max_processing_attempts=self._max_processing_attempts,
            error_category=error_category,
        )
        return updated if updated is not None else record

    def pending(self, limit: int = 500) -> tuple[InboundUpdate, ...]:
        return self._repository.pending_updates(limit)

    def pending_count(self) -> int:
        return self._repository.pending_count()

    def recovered(self, record: InboundUpdate) -> None:
        logger.info(
            "telegram_update_recovered",
            update_id=record.update_id,
            processing_attempts=record.processing_attempts,
        )
