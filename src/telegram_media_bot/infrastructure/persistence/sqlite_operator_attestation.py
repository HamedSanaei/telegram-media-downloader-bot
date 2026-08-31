"""WAL-backed operator public-only attestation store (ADR-034, T019).

Additive/idempotent: one current attestation row per operator generation, bound to the keyed
verifier of the canonical cookie file's Instagram records. The verifier is secret-adjacent and
kept out of logs/metrics; changing or replacing Instagram records invalidates it by mismatch.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.domain.credential_resolution import PublicOnlyAttestation
from telegram_media_bot.domain.errors import PersistenceError


class SqliteOperatorAttestationRepository:
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
            raise PersistenceError("Operator attestation store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operator_public_attestations (
                    operator_generation INTEGER PRIMARY KEY,
                    attested_at TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    keyed_verifier TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_attestation(self, attestation: PublicOnlyAttestation) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO operator_public_attestations (
                    operator_generation, attested_at, actor_role, keyed_verifier, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attestation.operator_generation,
                    _dump(attestation.attested_at),
                    attestation.actor_role,
                    attestation.keyed_verifier,
                    _dump(datetime.now(UTC)),
                ),
            )
            connection.execute("COMMIT")

    def get_current(self) -> PublicOnlyAttestation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_public_attestations "
                "ORDER BY operator_generation DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return PublicOnlyAttestation(
            operator_generation=int(row["operator_generation"]),
            attested_at=_load(str(row["attested_at"])),
            actor_role=str(row["actor_role"]),
            keyed_verifier=str(row["keyed_verifier"]),
        )


def _dump(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
