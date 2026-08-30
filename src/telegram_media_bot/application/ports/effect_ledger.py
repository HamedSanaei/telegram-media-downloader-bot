from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.effects import EffectRecord


class EffectLedger(Protocol):
    def initialize(self) -> None: ...

    def reserve(
        self,
        effect_key: str,
        *,
        update_id: int | None,
        effect_type: str,
        chat_id: int,
    ) -> EffectRecord:
        """Insert a PENDING effect row, or return the existing row for the same key.

        A deterministic ``effect_key`` identifies one intended side effect; reserving is
        idempotent, so replayed updates always see the same row.
        """

    def complete(self, effect_key: str, message_id: int, chat_id: int) -> None:
        """Mark an effect COMPLETED with its known Telegram message id."""

    def mark_uncertain(self, effect_key: str) -> None:
        """Mark an effect UNCERTAIN (Telegram call attempted, outcome unknown)."""

    def get(self, effect_key: str) -> EffectRecord | None: ...

    def reconcile_stale_pending(
        self, now: datetime, *, stale_after_minutes: int, batch_size: int
    ) -> int:
        """Boundedly quarantine stale PENDING effects as UNCERTAIN."""

    def state_counts(self) -> dict[str, int]: ...

    def purge_retention(self, now: datetime, *, retention_days: int, batch_size: int) -> int:
        """Boundedly delete COMPLETED/UNCERTAIN effects older than retention.

        PENDING rows are never purged — they may represent in-flight work.
        """
