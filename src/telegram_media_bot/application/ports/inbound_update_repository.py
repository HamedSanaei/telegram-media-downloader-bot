from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.inbound_updates import InboundUpdate, UpdateProcessingState


class InboundUpdateRepository(Protocol):
    def initialize(self) -> None: ...

    def persist(
        self,
        update_id: int,
        update_type: str,
        payload_json: str,
    ) -> tuple[InboundUpdate, bool]:
        """Durably record/return one Telegram update.

        Inserting a duplicate ``update_id`` is idempotent: the existing row is returned with
        ``newly_inserted=False`` instead of creating a second record.
        """

    def transition(
        self,
        update_id: int,
        state: UpdateProcessingState,
        *,
        last_error_category: str | None = None,
        increment_attempts: bool = False,
    ) -> InboundUpdate | None:
        """Move one update to ``state``, optionally bumping its processing-attempt counter."""

    def get(self, update_id: int) -> InboundUpdate | None: ...

    def pending_updates(self, limit: int = 500) -> tuple[InboundUpdate, ...]:
        """RECEIVED/PROCESSING updates in receipt order for startup reconciliation."""

    def pending_count(self) -> int:
        """Number of currently pending (uncompleted) updates."""

    def state_counts(self) -> dict[UpdateProcessingState, int]:
        """Number of updates per state (for operations visibility)."""

    def purge_retention(
        self,
        now: datetime,
        *,
        completed_retention_days: int,
        terminal_failure_retention_days: int,
        batch_size: int,
    ) -> int:
        """Boundedly delete COMPLETED / TERMINAL_FAILURE history older than retention."""

    def stuck_count(self, older_than: datetime) -> int:
        """Unfinished (RECEIVED/PROCESSING) updates older than ``older_than``."""
