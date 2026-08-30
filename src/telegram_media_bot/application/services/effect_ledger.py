"""Replay-safe sending of handler-side Telegram status messages.

Given a deterministic effect key, the service decides whether a replay-sensitive Telegram effect
should be executed, reused, or skipped:

* ``PENDING`` + no message id — nothing was (observably) sent yet: perform the effect.
* ``PENDING`` + message id — a previous attempt recorded the message id but crashed before
  completing the ledger row: the Telegram call very likely landed, so edit/reuse that message
  instead of sending a new one.
* ``COMPLETED`` — already done: reuse the recorded message id (edit) or skip entirely.
* ``UNCERTAIN`` — a previous attempt raised mid-call with unknown outcome: never fire the effect
  again automatically.

This is at-least-once for *status messages only*: a duplicate status is less harmful than losing
the UX, and the ledger removes the common double-send. Final media delivery never uses this
mechanism — ``DELIVERY_UNCERTAIN`` remains the sole owner of uncertain delivery semantics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

from telegram_media_bot.application.ports.effect_ledger import EffectLedger
from telegram_media_bot.domain.effects import EffectState

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """Result of resolving one replay-sensitive effect."""

    state: EffectState
    message_id: int | None
    #: True when a Telegram send was actually executed by this call.
    sent: bool = False


class EffectLedgerService:
    def __init__(self, repository: EffectLedger) -> None:
        self._repository = repository

    def reconcile_stale_pending(
        self, now: datetime, *, stale_after_minutes: int, batch_size: int
    ) -> int:
        """Quarantine stale reservations; callers schedule no automatic Telegram resend."""
        return self._repository.reconcile_stale_pending(
            now, stale_after_minutes=stale_after_minutes, batch_size=batch_size
        )

    async def send_or_reuse(
        self,
        *,
        effect_key: str,
        effect_type: str,
        update_id: int | None,
        chat_id: int,
        send: Callable[[], Awaitable[int]],
        edit: Callable[[int], Awaitable[None]] | None = None,
    ) -> EffectOutcome:
        """Perform or reuse one replay-sensitive status effect.

        ``send`` executes the Telegram call and returns the resulting message id. ``edit``, when
        provided, edits an existing message instead of sending a duplicate when a message id is
        already known from a previous attempt.
        """
        record = self._repository.reserve(
            effect_key,
            update_id=update_id,
            effect_type=effect_type,
            chat_id=chat_id,
        )
        if record.state is EffectState.COMPLETED:
            if record.message_id is not None and edit is not None:
                await edit(record.message_id)
            return EffectOutcome(state=EffectState.COMPLETED, message_id=record.message_id)
        if record.state is EffectState.UNCERTAIN:
            # Ambiguous previous attempt: never fire the effect again automatically.
            return EffectOutcome(state=EffectState.UNCERTAIN, message_id=record.message_id)
        if record.message_id is not None:
            # PENDING but a previous attempt recorded the message id: reuse it via edit.
            if edit is not None:
                await edit(record.message_id)
            return EffectOutcome(state=EffectState.COMPLETED, message_id=record.message_id)
        try:
            message_id = await send()
        except Exception:
            self._repository.mark_uncertain(effect_key)
            raise
        self._repository.complete(effect_key, message_id, chat_id)
        return EffectOutcome(state=EffectState.COMPLETED, message_id=message_id, sent=True)
