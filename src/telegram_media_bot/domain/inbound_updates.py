"""Durable inbound Telegram update inbox domain model.

A Telegram long-poll delivers each ``update_id`` at-least-once while the process is alive, but an
offset advanced past an update before its handler finishes is effectively acknowledged to Telegram.
Every replayable inbound update is durably recorded *before* the polling offset advances. The inbox
state machine drives replay/idempotency across crash, Docker, and host restarts; a bounded terminal
serialization tombstone records only the update ID and failure and deliberately abandons handler
processing when no replayable payload can be produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UpdateProcessingState(StrEnum):
    #: Durably recorded, not yet handed to a handler.
    RECEIVED = "received"
    #: A handler is (or was) running for this update; a crash leaves it here for recovery.
    PROCESSING = "processing"
    #: The handler finished successfully; never replayed.
    COMPLETED = "completed"
    #: The update handler itself is permanently broken after bounded retries; not replayed.
    TERMINAL_FAILURE = "terminal_failure"


#: States that are still eligible for (re)processing.
PENDING_STATES = frozenset({UpdateProcessingState.RECEIVED, UpdateProcessingState.PROCESSING})


@dataclass(frozen=True, slots=True)
class InboundUpdate:
    """One durably recorded Telegram update, replayable after a crash."""

    update_id: int
    received_at: datetime
    update_type: str
    #: Replayable JSON representation of the Telegram update (aiogram's
    #: ``deserialize_telegram_object_to_python`` round-trip, never raw ``Update.model_dump_json``).
    payload_json: str
    state: UpdateProcessingState = UpdateProcessingState.RECEIVED
    processing_attempts: int = 0
    last_error_category: str | None = None
    completed_at: datetime | None = None

    @property
    def pending(self) -> bool:
        return self.state in PENDING_STATES
