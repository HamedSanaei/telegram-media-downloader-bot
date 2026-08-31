"""Owner-bound encrypted Instagram credential vault ports (T017).

Framework-free contracts: a key store and envelope-symmetric cryptor keep ``cryptography`` out of
application code; a repository keeps SQLite out of services; only sanitized views and leases cross
the boundary. No plaintext cookie bytes, ciphertext, nonces, or key IDs are ever required outside
the infrastructure layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.instagram_credentials import (
    CredentialEnvelope,
    CredentialEvent,
    CredentialLease,
    InstagramCredential,
)


class VaultKeyStore(Protocol):
    """Operator-configured key ring: one active key plus decrypt-only rotation keys."""

    def active_key(self) -> tuple[str, bytes] | None:
        """Return (key_id, raw_key_bytes) for encryption, or None when no vault is configured."""

    def key_by_id(self, key_id: str) -> bytes | None:
        """Return the raw key bytes for a given key ID (active or retained), or None."""


class EnvelopeCryptor(Protocol):
    """AEAD envelope operations used by the vault service."""

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> CredentialEnvelope:
        """Encrypt with the active key and a random nonce; binds ``aad``."""

    def decrypt(self, envelope: CredentialEnvelope, *, aad: bytes) -> bytes:
        """Authenticated-decrypt an envelope; raises typed KeyMissing/Decrypt errors."""


class InstagramCredentialRepository(Protocol):
    """Durable encrypted-credential, event, and expiring-lease persistence (WAL/SQLite)."""

    def initialize(self) -> None: ...

    def save_credential(self, credential: InstagramCredential) -> None: ...

    def get_credential_for_owner(self, owner_user_id: int) -> InstagramCredential | None: ...

    def append_event(self, event: CredentialEvent) -> None: ...

    def list_events_for_owner(
        self, owner_user_id: int, *, limit: int
    ) -> tuple[CredentialEvent, ...]: ...

    def purge_events(self, *, before: datetime) -> int: ...

    def acquire_lease(
        self,
        *,
        owner_user_id: int,
        generation: int,
        expires_at: datetime,
        now: datetime,
    ) -> CredentialLease:
        """Atomically acquire a single expiring lease; raises typed errors on any block."""

    def release_lease(self, lease_id: str) -> bool: ...

    def purge_leases(self, *, before: datetime) -> int: ...


class CredentialMaterializer(Protocol):
    """Context-managed plaintext materialization inside an exact job workspace.

    Provides ``__enter__ -> Path`` and guarantees the plaintext file and the lease are released on
    every exit path (success, failure, cancellation, timeout, cleanup).
    """
