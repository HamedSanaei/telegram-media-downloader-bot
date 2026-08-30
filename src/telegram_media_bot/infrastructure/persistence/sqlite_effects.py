"""WAL-backed SQLite store for the durable Telegram side-effect ledger.

Shares the runtime state SQLite file (and its file-permission model) with the job store. Stores
only opaque identifiers — effect key, update id, chat id, message id, state, timestamps — never
message text, cookies, or payloads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.ports.effect_ledger import EffectLedger
from telegram_media_bot.domain.effects import EffectRecord, EffectState
from telegram_media_bot.domain.errors import PersistenceError

_TERMINAL_STATES = (EffectState.COMPLETED.value, EffectState.UNCERTAIN.value)


class SqliteEffectLedger(EffectLedger):
    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("Side-effect ledger operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_effects (
                    effect_key TEXT PRIMARY KEY,
                    update_id INTEGER,
                    effect_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS telegram_effects_retention_idx
                    ON telegram_effects(state, created_at);
                CREATE INDEX IF NOT EXISTS telegram_effects_update_idx
                    ON telegram_effects(update_id);
                """
            )

    def reserve(
        self,
        effect_key: str,
        *,
        update_id: int | None,
        effect_type: str,
        chat_id: int,
    ) -> EffectRecord:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO telegram_effects (
                    effect_key, update_id, effect_type, state, chat_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    effect_key,
                    update_id,
                    effect_type,
                    EffectState.PENDING.value,
                    chat_id,
                    _now_text(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM telegram_effects WHERE effect_key = ?", (effect_key,)
            ).fetchone()
        assert row is not None
        return _from_row(row)

    def complete(self, effect_key: str, message_id: int, chat_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telegram_effects SET
                    state = ?, message_id = ?, chat_id = ?, completed_at = ?
                WHERE effect_key = ?
                """,
                (EffectState.COMPLETED.value, message_id, chat_id, _now_text(), effect_key),
            )

    def mark_uncertain(self, effect_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telegram_effects SET state = ?, completed_at = ?
                WHERE effect_key = ?
                """,
                (EffectState.UNCERTAIN.value, _now_text(), effect_key),
            )

    def get(self, effect_key: str) -> EffectRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_effects WHERE effect_key = ?", (effect_key,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def reconcile_stale_pending(
        self, now: datetime, *, stale_after_minutes: int, batch_size: int
    ) -> int:
        cutoff = _dump_datetime(now - timedelta(minutes=stale_after_minutes))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT effect_key FROM telegram_effects
                WHERE state = ? AND created_at < ?
                ORDER BY created_at ASC LIMIT ?
                """,
                (EffectState.PENDING.value, cutoff, batch_size),
            ).fetchall()
            timestamp = _now_text()
            for row in rows:
                connection.execute(
                    """
                    UPDATE telegram_effects SET state = ?, completed_at = ?
                    WHERE effect_key = ? AND state = ?
                    """,
                    (
                        EffectState.UNCERTAIN.value,
                        timestamp,
                        row["effect_key"],
                        EffectState.PENDING.value,
                    ),
                )
            connection.execute("COMMIT")
        return len(rows)

    def state_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM telegram_effects GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def purge_retention(self, now: datetime, *, retention_days: int, batch_size: int) -> int:
        cutoff = _dump_datetime(now - timedelta(days=retention_days))
        placeholders = ",".join("?" for _ in _TERMINAL_STATES)
        deleted = 0
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT effect_key FROM telegram_effects
                WHERE state IN ({placeholders}) AND created_at < ?
                ORDER BY created_at ASC LIMIT ?
                """,
                (*_TERMINAL_STATES, cutoff, batch_size),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM telegram_effects WHERE effect_key = ?", (row["effect_key"],)
                )
            deleted = len(rows)
        return deleted


def _from_row(row: sqlite3.Row) -> EffectRecord:
    completed = row["completed_at"]
    return EffectRecord(
        effect_key=str(row["effect_key"]),
        update_id=int(row["update_id"]) if row["update_id"] is not None else None,
        effect_type=str(row["effect_type"]),
        state=EffectState(str(row["state"])),
        chat_id=int(row["chat_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        created_at=_load_datetime(str(row["created_at"])),
        completed_at=_load_datetime(completed) if completed else None,
    )


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _now_text() -> str:
    return _dump_datetime(datetime.now(UTC))
