"""Tests for the durable Telegram side-effect ledger (Hardening 3).

The ledger makes replay-sensitive handler-side status effects (initial status message, Story
delivery-mode prompt) idempotent across inbound-update replays: a replayed update reuses or skips
the earlier effect instead of emitting a duplicate Telegram message.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.effect_ledger import EffectLedgerService
from telegram_media_bot.domain.effects import EffectState
from telegram_media_bot.infrastructure.persistence.sqlite_effects import SqliteEffectLedger


def _ledger(tmp_path: Path) -> SqliteEffectLedger:
    result = SqliteEffectLedger(tmp_path / "state" / "jobs.sqlite3")
    result.initialize()
    return result


async def _run(coro: object) -> object:
    return await coro  # type: ignore[misc]


def test_status_message_sent_once_across_replay(tmp_path: Path) -> None:
    """First attempt sends; a replayed update reuses the same message instead of sending again."""
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)
    sent: list[int] = []
    edited: list[int] = []

    async def send() -> int:
        sent.append(111)
        return 111

    async def edit(message_id: int) -> None:
        edited.append(message_id)

    async def first() -> None:
        outcome = await service.send_or_reuse(
            effect_key="update:42:initial_status",
            effect_type="initial_status",
            update_id=42,
            chat_id=7,
            send=send,
            edit=edit,
        )
        assert outcome.sent is True
        assert outcome.message_id == 111

    async def replay() -> None:
        outcome = await service.send_or_reuse(
            effect_key="update:42:initial_status",
            effect_type="initial_status",
            update_id=42,
            chat_id=7,
            send=send,
            edit=edit,
        )
        assert outcome.sent is False
        assert outcome.message_id == 111

    asyncio.run(first())
    asyncio.run(replay())
    assert sent == [111]
    assert edited == [111]


def test_completed_effect_never_fires_send_again(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)
    sends = 0

    async def send() -> int:
        nonlocal sends
        sends += 1
        return 999

    async def run_effect() -> None:
        await service.send_or_reuse(
            effect_key="update:9:download_queued",
            effect_type="download_queued",
            update_id=9,
            chat_id=7,
            send=send,
        )

    asyncio.run(run_effect())
    asyncio.run(run_effect())
    asyncio.run(run_effect())
    assert sends == 1
    record = ledger.get("update:9:download_queued")
    assert record is not None and record.state is EffectState.COMPLETED


def test_uncertain_effect_is_never_resent(tmp_path: Path) -> None:
    """A crashed mid-call effect must not blindly fire again on replay."""
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)
    sends = 0

    async def send() -> int:
        nonlocal sends
        sends += 1
        raise RuntimeError("telegram call crashed mid-flight")

    async def attempt() -> None:
        with suppress(RuntimeError):
            await service.send_or_reuse(
                effect_key="update:5:story_delivery_mode_prompt",
                effect_type="story_delivery_mode_prompt",
                update_id=5,
                chat_id=7,
                send=send,
            )

    async def replay() -> None:
        outcome = await service.send_or_reuse(
            effect_key="update:5:story_delivery_mode_prompt",
            effect_type="story_delivery_mode_prompt",
            update_id=5,
            chat_id=7,
            send=send,
        )
        assert outcome.state is EffectState.UNCERTAIN

    asyncio.run(attempt())
    asyncio.run(replay())
    assert sends == 1
    record = ledger.get("update:5:story_delivery_mode_prompt")
    assert record is not None and record.state is EffectState.UNCERTAIN


def test_pending_effect_with_known_message_id_is_reused(tmp_path: Path) -> None:
    """Crash after send but before ledger completion: reuse the known message via edit."""
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)
    # Simulate: effect reserved, Telegram send landed, message_id recorded, crash before COMPLETED.
    ledger.reserve("update:3:initial_status", update_id=3, effect_type="initial_status", chat_id=7)
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute(
            "UPDATE telegram_effects SET message_id = 555 WHERE effect_key = 'update:3:initial_status'"
        )
        connection.commit()
    sends = 0
    edited: list[int] = []

    async def send() -> int:
        nonlocal sends
        sends += 1
        return 555

    async def edit(message_id: int) -> None:
        edited.append(message_id)

    async def replay() -> None:
        outcome = await service.send_or_reuse(
            effect_key="update:3:initial_status",
            effect_type="initial_status",
            update_id=3,
            chat_id=7,
            send=send,
            edit=edit,
        )
        assert outcome.sent is False
        assert outcome.message_id == 555

    asyncio.run(replay())
    assert sends == 0
    assert edited == [555]


def test_recovery_notice_sent_once_per_attempt(tmp_path: Path) -> None:
    """One recovery notice per recovery attempt even if reconciliation runs repeatedly."""
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)
    notices = 0

    async def notify() -> int:
        nonlocal notices
        notices += 1
        return 777

    async def send_notice(attempt: int) -> None:
        await service.send_or_reuse(
            effect_key=f"job:job-1:recovery_notice:{attempt}",
            effect_type="recovery_notice",
            update_id=None,
            chat_id=7,
            send=notify,
        )

    asyncio.run(send_notice(1))
    # Worker restart / repeated reconciliation for the same attempt: no second notice.
    asyncio.run(send_notice(1))
    asyncio.run(send_notice(1))
    assert notices == 1
    # A genuinely new recovery attempt (new version) is a different effect key.
    asyncio.run(send_notice(2))
    assert notices == 2


def test_fresh_pending_effect_remains_pending(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    record = ledger.reserve("fresh", update_id=1, effect_type="status", chat_id=1)
    assert (
        ledger.reconcile_stale_pending(datetime.now(UTC), stale_after_minutes=10, batch_size=500)
        == 0
    )
    current = ledger.get("fresh")
    assert current is not None and current.state is EffectState.PENDING
    assert record.state is EffectState.PENDING


def test_stale_pending_effect_becomes_uncertain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.reserve("stale", update_id=1, effect_type="status", chat_id=1)
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute("UPDATE telegram_effects SET created_at = ?", (old,))
        connection.commit()
    assert (
        ledger.reconcile_stale_pending(datetime.now(UTC), stale_after_minutes=10, batch_size=500)
        == 1
    )
    current = ledger.get("stale")
    assert current is not None and current.state is EffectState.UNCERTAIN
    assert (
        ledger.reconcile_stale_pending(datetime.now(UTC), stale_after_minutes=10, batch_size=500)
        == 0
    )


def test_completed_and_uncertain_are_unchanged_by_stale_reconciliation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.reserve("completed", update_id=1, effect_type="status", chat_id=1)
    ledger.complete("completed", 10, 1)
    ledger.reserve("uncertain", update_id=2, effect_type="status", chat_id=1)
    ledger.mark_uncertain("uncertain")
    assert (
        ledger.reconcile_stale_pending(datetime.now(UTC), stale_after_minutes=0, batch_size=500)
        == 0
    )
    assert ledger.get("completed").state is EffectState.COMPLETED  # type: ignore[union-attr]
    assert ledger.get("uncertain").state is EffectState.UNCERTAIN  # type: ignore[union-attr]


def test_stale_reconciliation_is_batched(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(1000):
        ledger.reserve(f"stale-{index}", update_id=index, effect_type="status", chat_id=1)
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute("UPDATE telegram_effects SET created_at = ?", (old,))
        connection.commit()
    now = datetime.now(UTC)
    assert ledger.reconcile_stale_pending(now, stale_after_minutes=10, batch_size=500) == 500
    assert ledger.reconcile_stale_pending(now, stale_after_minutes=10, batch_size=500) == 500
    assert ledger.reconcile_stale_pending(now, stale_after_minutes=10, batch_size=500) == 0


def test_stale_effect_can_later_be_purged(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.reserve("old-pending", update_id=1, effect_type="status", chat_id=1)
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute("UPDATE telegram_effects SET created_at = ?", (old,))
        connection.commit()
    now = datetime.now(UTC)
    assert ledger.reconcile_stale_pending(now, stale_after_minutes=10, batch_size=500) == 1
    assert ledger.purge_retention(now, retention_days=30, batch_size=500) == 1
    assert ledger.get("old-pending") is None


def test_effect_cleanup_purges_old_completed_but_keeps_pending(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)

    async def send() -> int:
        await asyncio.sleep(0)
        return 1

    async def send_effect(key: str, update_id: int | None) -> None:
        await service.send_or_reuse(
            effect_key=key,
            effect_type="status",
            update_id=update_id,
            chat_id=7,
            send=send,
        )

    asyncio.run(send_effect("update:1:status", 1))
    asyncio.run(send_effect("update:2:status", 2))
    ledger.reserve("update:3:status", update_id=3, effect_type="status", chat_id=7)
    # Backdate the completed effects; PENDING stays fresh.
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute(
            "UPDATE telegram_effects SET created_at = ?, completed_at = ? "
            "WHERE effect_key IN ('update:1:status', 'update:2:status')",
            (old, old),
        )
        connection.commit()
    purged = ledger.purge_retention(datetime.now(UTC), retention_days=30, batch_size=100)
    assert purged == 2
    assert ledger.get("update:1:status") is None
    assert ledger.get("update:2:status") is None
    # PENDING effects represent in-flight work and are never age-purged.
    assert ledger.get("update:3:status") is not None
    assert ledger.get("update:3:status").state is EffectState.PENDING  # type: ignore[union-attr]


def test_effect_cleanup_is_batched_and_idempotent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    service = EffectLedgerService(ledger)

    async def send() -> int:
        await asyncio.sleep(0)
        return 1

    async def send_effect(key: str) -> None:
        await service.send_or_reuse(
            effect_key=key,
            effect_type="status",
            update_id=None,
            chat_id=7,
            send=send,
        )

    for index in range(25):
        asyncio.run(send_effect(f"update:{index}:status"))
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute(
            "UPDATE telegram_effects SET created_at = ?, completed_at = ?",
            (old, old),
        )
        connection.commit()
    now = datetime.now(UTC)
    first = ledger.purge_retention(now, retention_days=30, batch_size=10)
    second = ledger.purge_retention(now, retention_days=30, batch_size=10)
    third = ledger.purge_retention(now, retention_days=30, batch_size=10)
    assert first == 10
    assert second == 10
    assert third == 5
    # Re-running when nothing is left is safe and deletes nothing.
    assert ledger.purge_retention(now, retention_days=30, batch_size=10) == 0
