"""WAL-backed owner-bound encrypted credential store (T017).

Additive and idempotent: existing databases gain credential/event/lease tables without rewrites or
deletions. Only the AEAD envelope (as a bounded base64 JSON string, never searched or logged) and
sanitized metadata are stored; raw cookie bytes, passwords, 2FA codes, nonces, and key IDs are
never durable. Events follow ADR-033's 90-day retention; leases are purged shortly after expiry.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.domain.errors import (
    CredentialChallengeRequiredError,
    CredentialDecryptError,
    CredentialDisconnectedError,
    CredentialExpiredError,
    CredentialGenerationMismatchError,
    CredentialLeaseBusyError,
    CredentialNotFoundError,
    CredentialRevokedError,
    PersistenceError,
)
from telegram_media_bot.domain.instagram_credentials import (
    CREDENTIAL_PROVIDER,
    CredentialEnvelope,
    CredentialEvent,
    CredentialEventKind,
    CredentialLease,
    InstagramCredential,
    InstagramCredentialState,
    LeaseState,
    new_lease_id,
)


class SqliteInstagramCredentialRepository:
    """Sharing the bot/worker WAL database; plaintext never enters these queries."""

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
            raise PersistenceError("Credential store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS instagram_credentials (
                    credential_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    envelope_json TEXT,
                    last_verified_at TEXT,
                    last_success_at TEXT,
                    last_failure_category TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (owner_user_id, provider)
                );
                CREATE TABLE IF NOT EXISTS instagram_credential_events (
                    event_id TEXT PRIMARY KEY,
                    credential_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    actor_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (credential_id) REFERENCES instagram_credentials(credential_id)
                );
                CREATE INDEX IF NOT EXISTS credential_events_owner_time_idx
                    ON instagram_credential_events(owner_user_id, created_at);
                CREATE TABLE IF NOT EXISTS instagram_credential_leases (
                    lease_id TEXT PRIMARY KEY,
                    credential_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (credential_id) REFERENCES instagram_credentials(credential_id)
                );
                CREATE INDEX IF NOT EXISTS credential_leases_active_idx
                    ON instagram_credential_leases(credential_id, state);
                CREATE INDEX IF NOT EXISTS credential_leases_expires_idx
                    ON instagram_credential_leases(expires_at);
                """
            )

    def save_credential(self, credential: InstagramCredential) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO instagram_credentials (
                    credential_id, provider, owner_user_id, state, generation, envelope_json,
                    last_verified_at, last_success_at, last_failure_category, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (credential_id) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    envelope_json = excluded.envelope_json,
                    last_verified_at = excluded.last_verified_at,
                    last_success_at = excluded.last_success_at,
                    last_failure_category = excluded.last_failure_category,
                    updated_at = excluded.updated_at
                """,
                (
                    credential.credential_id,
                    credential.provider,
                    credential.owner_user_id,
                    credential.state.value,
                    credential.generation,
                    credential.envelope.serialized() if credential.envelope else None,
                    _dump_datetime(credential.last_verified_at)
                    if credential.last_verified_at
                    else None,
                    _dump_datetime(credential.last_success_at)
                    if credential.last_success_at
                    else None,
                    credential.last_failure_category,
                    _dump_datetime(credential.created_at),
                    _dump_datetime(credential.updated_at),
                ),
            )
            connection.execute("COMMIT")

    def get_credential_for_owner(self, owner_user_id: int) -> InstagramCredential | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM instagram_credentials WHERE owner_user_id = ? AND provider = ?",
                (owner_user_id, CREDENTIAL_PROVIDER),
            ).fetchone()
        return _credential_from_row(row) if row is not None else None

    def append_event(self, event: CredentialEvent) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO instagram_credential_events (
                    event_id, credential_id, owner_user_id, kind, generation, actor_role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.credential_id,
                    event.owner_user_id,
                    event.kind.value,
                    event.generation,
                    event.actor_role,
                    _dump_datetime(event.created_at),
                ),
            )
            connection.execute("COMMIT")

    def list_events_for_owner(
        self, owner_user_id: int, *, limit: int
    ) -> tuple[CredentialEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instagram_credential_events WHERE owner_user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (owner_user_id, limit),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def purge_events(self, *, before: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM instagram_credential_events WHERE created_at < ?",
                (_dump_datetime(before),),
            )
        return int(cursor.rowcount)

    def acquire_lease(
        self,
        *,
        owner_user_id: int,
        generation: int,
        expires_at: datetime,
        now: datetime,
    ) -> CredentialLease:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM instagram_credentials WHERE owner_user_id = ? AND provider = ?",
                    (owner_user_id, CREDENTIAL_PROVIDER),
                ).fetchone()
                if row is None:
                    raise CredentialNotFoundError("no credential exists for the owner")
                credential = _credential_from_row(row)
                if credential.generation != generation:
                    raise CredentialGenerationMismatchError(
                        "credential generation does not match the request"
                    )
                if credential.state is InstagramCredentialState.REVOKED:
                    raise CredentialRevokedError("credential is revoked")
                if credential.state is InstagramCredentialState.DISCONNECTED:
                    raise CredentialDisconnectedError("credential is disconnected")
                if credential.state is InstagramCredentialState.EXPIRED:
                    raise CredentialExpiredError("credential session expired")
                if credential.state is InstagramCredentialState.CHALLENGE_REQUIRED:
                    raise CredentialChallengeRequiredError("credential requires a challenge")
                if credential.envelope is None:
                    raise CredentialDisconnectedError("credential holds no ciphertext")
                live = connection.execute(
                    "SELECT lease_id FROM instagram_credential_leases "
                    "WHERE credential_id = ? AND state = ? AND expires_at > ?",
                    (credential.credential_id, LeaseState.ACTIVE.value, _dump_datetime(now)),
                ).fetchone()
                if live is not None:
                    raise CredentialLeaseBusyError("another job holds this credential lease")
                lease_id = new_lease_id()
                connection.execute(
                    """
                    INSERT INTO instagram_credential_leases (
                        lease_id, credential_id, owner_user_id, generation, state,
                        acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        credential.credential_id,
                        owner_user_id,
                        generation,
                        LeaseState.ACTIVE.value,
                        _dump_datetime(now),
                        _dump_datetime(expires_at),
                    ),
                )
                connection.execute("COMMIT")
                return CredentialLease(
                    lease_id=lease_id,
                    credential_id=credential.credential_id,
                    owner_user_id=owner_user_id,
                    generation=generation,
                    acquired_at=now,
                    expires_at=expires_at,
                    state=LeaseState.ACTIVE,
                )
            except (
                CredentialNotFoundError,
                CredentialGenerationMismatchError,
                CredentialRevokedError,
                CredentialDisconnectedError,
                CredentialExpiredError,
                CredentialChallengeRequiredError,
                CredentialLeaseBusyError,
            ) as exc:
                connection.execute("ROLLBACK")
                raise exc
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise PersistenceError("Credential lease operation failed") from exc

    def release_lease(self, lease_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE instagram_credential_leases SET state = ? WHERE lease_id = ?",
                (LeaseState.RELEASED.value, lease_id),
            )
        return cursor.rowcount > 0

    def purge_leases(self, *, before: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM instagram_credential_leases WHERE expires_at < ?",
                (_dump_datetime(before),),
            )
        return int(cursor.rowcount)


def _credential_from_row(row: sqlite3.Row) -> InstagramCredential:
    envelope_value = row["envelope_json"]
    envelope: CredentialEnvelope | None = None
    if envelope_value is not None:
        try:
            envelope = CredentialEnvelope.parse(str(envelope_value))
        except CredentialDecryptError as exc:
            raise CredentialDecryptError("stored credential envelope is malformed") from exc
    return InstagramCredential(
        credential_id=str(row["credential_id"]),
        provider=str(row["provider"]),
        owner_user_id=int(row["owner_user_id"]),
        state=InstagramCredentialState(str(row["state"])),
        generation=int(row["generation"]),
        envelope=envelope,
        last_verified_at=_load_datetime(row["last_verified_at"])
        if row["last_verified_at"]
        else None,
        last_success_at=_load_datetime(row["last_success_at"]) if row["last_success_at"] else None,
        last_failure_category=row["last_failure_category"],
        created_at=_load_datetime(str(row["created_at"])),
        updated_at=_load_datetime(str(row["updated_at"])),
    )


def _event_from_row(row: sqlite3.Row) -> CredentialEvent:
    return CredentialEvent(
        event_id=str(row["event_id"]),
        credential_id=str(row["credential_id"]),
        owner_user_id=int(row["owner_user_id"]),
        kind=CredentialEventKind(str(row["kind"])),
        generation=int(row["generation"]),
        created_at=_load_datetime(str(row["created_at"])),
        actor_role=str(row["actor_role"]),
    )


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)
