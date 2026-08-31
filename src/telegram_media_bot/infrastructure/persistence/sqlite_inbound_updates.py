"""WAL-backed SQLite store for the durable Telegram inbound-update inbox.

The same database permissions model as the job store applies: this file lives below the state
directory that the runtime owns at ``0600`` modes, and it may hold replayable Telegram update
payloads so a crash never loses an unanswered user request.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.ports.inbound_update_repository import (
    InboundUpdateRepository,
)
from telegram_media_bot.domain.errors import PersistenceError
from telegram_media_bot.domain.inbound_updates import (
    InboundUpdate,
    UpdateProcessingState,
)

_PENDING_STATES = (
    UpdateProcessingState.RECEIVED.value,
    UpdateProcessingState.PROCESSING.value,
)
_ACTIVE_STATES = (
    UpdateProcessingState.RECEIVED.value,
    UpdateProcessingState.PROCESSING.value,
    UpdateProcessingState.COMPLETED.value,
    UpdateProcessingState.TERMINAL_FAILURE.value,
)
_MEDIA_GROUP_SCAN_LIMIT = 100
_TELEGRAM_MEDIA_GROUP_MAX_ITEMS = 10


class SqliteInboundUpdateRepository(InboundUpdateRepository):
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
            raise PersistenceError("Inbound-update store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_updates (
                    update_id INTEGER PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    update_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processing_state TEXT NOT NULL,
                    processing_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_category TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS inbound_updates_pending_idx
                    ON inbound_updates(processing_state, received_at);
                CREATE INDEX IF NOT EXISTS inbound_updates_retention_idx
                    ON inbound_updates(processing_state, completed_at);
                """
            )

    def persist(
        self,
        update_id: int,
        update_type: str,
        payload_json: str,
    ) -> tuple[InboundUpdate, bool]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_updates (
                    update_id, received_at, update_type, payload_json,
                    processing_state, processing_attempts
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    update_id,
                    _now_text(),
                    update_type,
                    payload_json,
                    UpdateProcessingState.RECEIVED.value,
                ),
            )
            newly_inserted = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM inbound_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
        assert row is not None
        return _from_row(row), newly_inserted

    def persist_terminal(self, update_id: int, update_type: str, error_category: str) -> bool:
        """Atomically insert an unserializable update directly in TERMINAL_FAILURE state.

        Inserting directly as terminal (never passing through RECEIVED) means a crash cannot leave
        a tombstone row pending that replay would try to parse. Uses a safe marker payload with no
        user/message content. Idempotent via ``INSERT OR IGNORE``.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_updates (
                    update_id, received_at, update_type, payload_json,
                    processing_state, processing_attempts, last_error_category, completed_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    update_id,
                    _now_text(),
                    update_type,
                    _quarantine_payload(update_id),
                    UpdateProcessingState.TERMINAL_FAILURE.value,
                    error_category,
                    _now_text(),
                ),
            )
        return cursor.rowcount == 1

    def transition(
        self,
        update_id: int,
        state: UpdateProcessingState,
        *,
        last_error_category: str | None = None,
        increment_attempts: bool = False,
    ) -> InboundUpdate | None:
        # COMPLETED and TERMINAL_FAILURE are both terminal: record a terminal timestamp so
        # retention cleanup can purge them by age.
        completed_column = (
            ", completed_at = ?"
            if state in {UpdateProcessingState.COMPLETED, UpdateProcessingState.TERMINAL_FAILURE}
            else ""
        )
        binds: list[object] = [state.value, last_error_category]
        if state in {UpdateProcessingState.COMPLETED, UpdateProcessingState.TERMINAL_FAILURE}:
            binds.append(_now_text())
        attempts_clause = (
            "processing_attempts = processing_attempts + 1" if increment_attempts else ""
        )
        columns = (
            attempts_clause if attempts_clause else "processing_attempts = processing_attempts"
        )
        binds.append(update_id)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE inbound_updates SET
                    processing_state = ?,
                    last_error_category = ?,
                    {columns}{completed_column}
                WHERE update_id = ?
                """,
                binds,
            )
        return self.get(update_id)

    def get(self, update_id: int) -> InboundUpdate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def pending_updates(self, limit: int = 500) -> tuple[InboundUpdate, ...]:
        placeholders = ",".join("?" for _ in _PENDING_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM inbound_updates
                WHERE processing_state IN ({placeholders})
                ORDER BY received_at ASC, update_id ASC LIMIT ?
                """,
                (*_PENDING_STATES, limit),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def pending_count(self) -> int:
        placeholders = ",".join("?" for _ in _PENDING_STATES)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM inbound_updates WHERE processing_state IN ({placeholders})",
                _PENDING_STATES,
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def state_counts(self) -> dict[UpdateProcessingState, int]:
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT processing_state, COUNT(*) AS count FROM inbound_updates
                WHERE processing_state IN ({placeholders})
                GROUP BY processing_state
                """,
                _ACTIVE_STATES,
            ).fetchall()
        return {
            UpdateProcessingState(str(row["processing_state"])): int(row["count"]) for row in rows
        }

    def purge_retention(
        self,
        now: datetime,
        *,
        completed_retention_days: int,
        terminal_failure_retention_days: int,
        batch_size: int,
    ) -> int:
        """Boundedly delete terminal inbox history by age (COMPLETED / TERMINAL_FAILURE).

        RECEIVED and PROCESSING rows are never touched, no matter how old — they are potentially
        unfinished user work and must surface as stuck instead. Batching keeps each pass bounded;
        a crash mid-pass simply leaves the rest for the next maintenance run.
        """
        completed_cutoff = _dump_datetime(now - timedelta(days=completed_retention_days))
        terminal_cutoff = _dump_datetime(now - timedelta(days=terminal_failure_retention_days))
        deleted = 0
        with self._connect() as connection:
            for state, cutoff in (
                (UpdateProcessingState.COMPLETED.value, completed_cutoff),
                (UpdateProcessingState.TERMINAL_FAILURE.value, terminal_cutoff),
            ):
                rows = connection.execute(
                    """
                    SELECT update_id FROM inbound_updates
                    WHERE processing_state = ? AND completed_at IS NOT NULL AND completed_at < ?
                    ORDER BY completed_at ASC LIMIT ?
                    """,
                    (state, cutoff, batch_size),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "DELETE FROM inbound_updates WHERE update_id = ?", (row["update_id"],)
                    )
                deleted += len(rows)
        return deleted

    def stuck_count(self, older_than: datetime) -> int:
        """Unfinished updates older than ``older_than`` (stuck work, never auto-deleted)."""
        cutoff = _dump_datetime(older_than)
        placeholders = ",".join("?" for _ in _PENDING_STATES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) FROM inbound_updates
                WHERE processing_state IN ({placeholders}) AND received_at < ?
                """,
                (*_PENDING_STATES, cutoff),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def media_group_message_ids(self, chat_id: int, media_group_id: str) -> tuple[int, ...]:
        """Resolve one bounded album from already-durable inbound snapshots."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM inbound_updates
                ORDER BY update_id DESC LIMIT ?""",
                (_MEDIA_GROUP_SCAN_LIMIT,),
            ).fetchall()
        message_ids: set[int] = set()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError, TypeError, ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            message = next(
                (
                    payload.get(key)
                    for key in ("message", "edited_message", "business_message")
                    if isinstance(payload.get(key), dict)
                ),
                None,
            )
            if not isinstance(message, dict) or message.get("media_group_id") != media_group_id:
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict) or chat.get("id") != chat_id:
                continue
            message_id = message.get("message_id")
            if isinstance(message_id, int) and message_id > 0:
                message_ids.add(message_id)
        return tuple(sorted(message_ids))[:_TELEGRAM_MEDIA_GROUP_MAX_ITEMS]


def _from_row(row: sqlite3.Row) -> InboundUpdate:
    completed = row["completed_at"]
    return InboundUpdate(
        update_id=int(row["update_id"]),
        received_at=_load_datetime(str(row["received_at"])),
        update_type=str(row["update_type"]),
        payload_json=str(row["payload_json"]),
        state=UpdateProcessingState(str(row["processing_state"])),
        processing_attempts=int(row["processing_attempts"]),
        last_error_category=(
            str(row["last_error_category"]) if row["last_error_category"] else None
        ),
        completed_at=_load_datetime(completed) if completed else None,
    )


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _now_text() -> str:
    return _dump_datetime(datetime.now(UTC))


def _quarantine_payload(update_id: int) -> str:
    return json.dumps({"update_id": update_id, "_unserializable": True}, ensure_ascii=False)
