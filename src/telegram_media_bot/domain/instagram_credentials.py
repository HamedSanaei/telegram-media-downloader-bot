"""Owner-bound encrypted Instagram credential vault domain model (T017).

Typed, framework-free state for an encrypted per-user Instagram session: the safe lifecycle state
(a project enum), a monotonic per-owner generation, sanitized lifecycle events, an AEAD envelope,
and a bounded expiring lease. Only sanitized metadata/flows cross application boundaries; raw
cookie bytes, ciphertext, nonces, and key IDs never become domain tokens used broadly. Passwords,
2FA codes, and upstream secrets are never part of any durable model.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

#: Provider constant used as associated-data binding; a user credential is always Instagram.
CREDENTIAL_PROVIDER = "instagram"
#: Current AEAD/CryptoAuth envelope version.
ENVELOPE_VERSION = 1
#: Retention for sanitized lifecycle events (ADR-033).
CREDENTIAL_EVENT_RETENTION_DAYS = 90
#: Leases are purged shortly after expiry (ADR-033).
LEASE_PURGE_HOURS = 24


class InstagramCredentialState(StrEnum):
    """Stable sanitized lifecycle state surfaced to application/Telegram code."""

    CONNECTED = "connected"
    EXPIRED = "expired"
    CHALLENGE_REQUIRED = "challenge_required"
    REVOKED = "revoked"
    DISCONNECTED = "disconnected"


class CredentialEventKind(StrEnum):
    """Sanitized lifecycle audit events (no credential values ever)."""

    CONNECTED = "connected"
    RECONNECTED = "reconnected"
    EXPIRED = "expired"
    CHALLENGE_REQUIRED = "challenge_required"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    ROTATED = "rotated"


class LeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


def new_credential_id() -> str:
    return str(_uuid.uuid4())


def new_lease_id() -> str:
    return str(_uuid.uuid4())


def new_random_nonce() -> bytes:
    """Random 96-bit nonce for AES-GCM (never reused across encryptions)."""
    return secrets.token_bytes(12)


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    """Versioned AEAD envelope. Fields are secret-adjacent and never logged/searched."""

    version: int = ENVELOPE_VERSION
    key_id: str = ""
    nonce: bytes = b""
    ciphertext: bytes = b""

    def serialized(self) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": self.version,
                    "k": self.key_id,
                    "n": base64.urlsafe_b64encode(self.nonce).decode("ascii"),
                    "c": base64.urlsafe_b64encode(self.ciphertext).decode("ascii"),
                }
            ).encode("utf-8")
        ).decode("ascii")

    @classmethod
    def parse(cls, value: str) -> CredentialEnvelope:
        try:
            raw = json.loads(base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8"))
            return cls(
                version=int(raw["v"]),
                key_id=str(raw["k"]),
                nonce=base64.urlsafe_b64decode(raw["n"].encode("ascii")),
                ciphertext=base64.urlsafe_b64decode(raw["c"].encode("ascii")),
            )
        except KeyError, TypeError, ValueError:
            from telegram_media_bot.domain.errors import CredentialDecryptError

            raise CredentialDecryptError("malformed credential envelope") from None


@dataclass(frozen=True, slots=True)
class InstagramCredential:
    """Safe current projection of one owner's Instagram credential row."""

    credential_id: str
    provider: str
    owner_user_id: int
    state: InstagramCredentialState
    generation: int
    envelope: CredentialEnvelope | None
    last_verified_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_category: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SafeCredentialView:
    """Sanitized view for Telegram/admin/application layers; never contains secret material."""

    state: InstagramCredentialState
    generation: int
    last_verified_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_category: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialEvent:
    event_id: str
    credential_id: str
    owner_user_id: int
    kind: CredentialEventKind
    generation: int
    created_at: datetime
    #: Safe actor role ("user" or "admin"); never a secret, username, or raw value.
    actor_role: str = "user"


@dataclass(frozen=True, slots=True)
class CredentialLease:
    lease_id: str
    credential_id: str
    owner_user_id: int
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: LeaseState = LeaseState.ACTIVE


def aad_for(*, provider: str, credential_id: str, owner_user_id: int, generation: int) -> bytes:
    """Canonical associated-data binding used by the envelope codec (plain ASCII, never secret)."""
    return (
        f"tmb{ENVELOPE_VERSION}:{provider}:{credential_id}:{owner_user_id}:{generation}"
    ).encode("ascii")


__all__ = [
    "CREDENTIAL_EVENT_RETENTION_DAYS",
    "CREDENTIAL_PROVIDER",
    "ENVELOPE_VERSION",
    "LEASE_PURGE_HOURS",
    "CredentialEnvelope",
    "CredentialEvent",
    "CredentialEventKind",
    "CredentialLease",
    "InstagramCredential",
    "InstagramCredentialState",
    "LeaseState",
    "SafeCredentialView",
    "aad_for",
    "new_credential_id",
    "new_lease_id",
    "new_random_nonce",
]
