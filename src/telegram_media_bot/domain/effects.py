"""Durable idempotency for replay-sensitive Telegram side effects.

The durable inbound-update inbox provides at-least-once update processing, so a handler-side
Telegram status message (inspection queued, Story delivery-mode prompt, …) could be emitted twice if
the process crashed between the Telegram call and marking the inbound update completed. This ledger
records one deterministic ``effect_key`` per intended side effect so a replayed update reuses or
skips the earlier effect instead of duplicating it.

Telegram API calls are external side effects that cannot be committed atomically with SQLite, so an
effect can be left in a genuinely ambiguous state. The states below make that explicit:

* ``PENDING`` — reserved, but the Telegram call may or may not have landed yet.
* ``COMPLETED`` — the effect is durably done and carries a known ``message_id`` when applicable.
* ``UNCERTAIN`` — the Telegram call was attempted but its outcome is unknown (crash mid-call);
  a later replay must not blindly fire it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EffectState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """One durable side-effect ledger entry."""

    effect_key: str
    update_id: int | None
    effect_type: str
    state: EffectState
    chat_id: int
    message_id: int | None
    created_at: datetime
    completed_at: datetime | None
