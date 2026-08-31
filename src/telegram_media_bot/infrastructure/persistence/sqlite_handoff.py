"""WAL-backed single-use handoff nonce store for the companion boundary (T016).

Additive and idempotent: existing databases gain a ``handoff_nonce_consumptions`` table without
rewrites or deletions. Only the SHA-256 digest of each nonce is durable; raw nonce values never
touch the database, logs, or metrics. Rows are purged shortly after expiry and are never read by
media, payment, or credential code.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.domain.errors import PersistenceError
from telegram_media_bot.domain.web_companion import HandoffPurpose


class SqliteHandoffNonceRepository:
    """Durable exactly-once nonce consumption sharing the bot/worker WAL database."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("Handoff nonce store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS handoff_nonce_consumptions (
                    nonce_hash TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoff_nonce_expires_idx
                    ON handoff_nonce_consumptions(expires_at);
                """
            )

    def reserve_once(
        self,
        *,
        nonce_hash: str,
        purpose: HandoffPurpose,
        owner_user_id: int,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM handoff_nonce_consumptions WHERE nonce_hash = ?",
                (nonce_hash,),
            ).fetchone()
            if row is not None:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                """
                INSERT INTO handoff_nonce_consumptions (
                    nonce_hash, purpose, owner_user_id, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    nonce_hash,
                    purpose.value,
                    owner_user_id,
                    _dump_datetime(expires_at),
                    _dump_datetime(now),
                ),
            )
            connection.execute("COMMIT")
            return True

    def purge_expired(self, *, now: datetime, before: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM handoff_nonce_consumptions WHERE expires_at < ?",
                (_dump_datetime(before),),
            )
        _ = now
        return int(cursor.rowcount)


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")
