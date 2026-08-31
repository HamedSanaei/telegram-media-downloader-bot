"""Secure companion web boundary domain model (T016).

Typed, framework-free types for the separate least-privilege companion process: short-lived,
single-use, purpose-bound Ed25519 handoff claims; browser-session and CSRF tokens; bounded
in-memory interactive flow state; and provider-callback verification outcomes. No HTTP framework,
didn't store password/2FA material, and no bot token ever appears in this layer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic


class HandoffPurpose(StrEnum):
    """The single available claim purpose for browser link exchanges."""

    INSTAGRAM_CONNECT = "instagram_connect"


class HandoffVerificationOutcome(StrEnum):
    """Stable, generic verification result for a presented handoff token.

    Failure outcomes deliberately carry no exploitable detail about why a token was rejected;
    the caller presents the same generic "expired or invalid session" message regardless.
    """

    VERIFIED = "verified"
    MALFORMED = "malformed"
    INVALID_SIGNATURE = "invalid_signature"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    WRONG_PURPOSE = "wrong_purpose"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class HandoffClaim:
    """The authenticated statement the bot signs and the companion verifies.

    ``nonce`` is a high-entropy single-use value consumed exactly once by the companion's durable
    nonce store; ``issued_at``/``expires_at`` are UTC instants with a bounded fixed lifetime (the
    companion validates a configurable clock-skew window on top of them).
    """

    purpose: HandoffPurpose
    owner_user_id: int
    nonce: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HandoffVerification:
    """Outcome of verifying one presented handoff token."""

    outcome: HandoffVerificationOutcome
    claim: HandoffClaim | None = None

    @property
    def verified(self) -> bool:
        return self.outcome is HandoffVerificationOutcome.VERIFIED


class InstagramConnectStage(StrEnum):
    """Sanitized stage of a browser Instagram connection flow.

    Only safe presentation state is surfaced to the admin/user; no credentials, cookie bytes,
    upstream error text, or existence evidence is ever part of this model.
    """

    NOT_AVAILABLE = "not_available"
    NEED_CREDENTIALS = "need_credentials"
    NEED_2FA = "need_2fa"
    CONNECTED = "connected"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class InstagramConnectResult:
    """A safe, bounded view of one browser connection-flow step."""

    stage: InstagramConnectStage
    message: str = ""


class PaymentCallbackOutcome(StrEnum):
    """Normalized result of processing one verified payment callback."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_AVAILABLE = "not_available"


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """Server-side browser session bound to a verified handoff.

    ``id`` is an unguessable random token stored only as its SHA-256 digest server-side; the raw
    value is delivered to the user agent as a Secure/HttpOnly/SameSite cookie. ``csrf_token`` is a
    synchronizer token returned to the browser and required on every state-mutating request.
    ``label`` is a safe, purpose-scoped marker used to keep Instagram and payment browser routes
    isolated even though they share one session table.
    """

    id: str
    csrf_token: str
    owner_user_id: int
    purpose: HandoffPurpose
    created_at: datetime
    expires_at: datetime
    #: Fixes when the browser session was first established so a replaced session cannot inherit
    #: stale state; also used to bound the lifetime of the in-memory interactive flow.
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionExpiryPolicy(StrEnum):
    """How long a browser session and its interactive flow may live."""

    SHORT = "short"
    INTERACTIVE = "interactive"


def new_handoff_nonce() -> str:
    """Cryptographically random high-entropy single-use nonce (no URL-safe padding)."""
    return secrets.token_urlsafe(24)


def new_browser_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def sha256_digest(value: str) -> str:
    """Deterministic, non-reversible digest for storing session IDs and nonces at rest."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BoundedMemoryFlowState:
    """Bounded in-memory interactive flow state with max-age and max-count eviction.

    Used only for transient login/2FA/checkpoint state that must never be durable. Values are
    bounded strings; no password or 2FA code is ever persisted by the application. Entry lifetimes
    are capped by ``max_age_seconds`` and the table by ``max_entries``; stale entries are evicted
    lazily on access and eagerly on insert when full. Intentionally not thread-safe: the companion
    can bound interactive sessions to a single event loop, matching the single-listener topology.
    """

    def __init__(self, *, max_age_seconds: int, max_entries: int) -> None:
        if max_age_seconds < 1 or max_entries < 1:
            raise ValueError("flow-state bounds must be positive")
        self._max_age_seconds = max_age_seconds
        self._max_entries = max_entries
        self._entries: dict[str, tuple[float, str]] = {}

    def _now(self) -> float:
        return monotonic()

    def set(self, key: str, value: str) -> None:
        now = self._now()
        self._expire(now)
        self._entries[key] = (now, value)
        if len(self._entries) > self._max_entries:
            # Deterministic bounded eviction: drop the oldest remaining entry.
            oldest = min(self._entries.items(), key=lambda item: item[1][0])
            del self._entries[oldest[0]]

    def get(self, key: str, *, consume: bool = False) -> str | None:
        now = self._now()
        self._expire(now)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if consume:
            del self._entries[key]
        return entry[1]

    def drop(self, key: str) -> None:
        self._entries.pop(key, None)

    def _expire(self, now: float) -> None:
        stale = [
            key
            for key, (created, _value) in self._entries.items()
            if now - created >= self._max_age_seconds
        ]
        for key in stale:
            del self._entries[key]

    @property
    def size(self) -> int:
        self._expire(self._now())
        return len(self._entries)


def utc_from_timestamp(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def utc_to_timestamp(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())


__all__ = [
    "BoundedMemoryFlowState",
    "BrowserSession",
    "HandoffClaim",
    "HandoffPurpose",
    "HandoffVerification",
    "HandoffVerificationOutcome",
    "InstagramConnectResult",
    "InstagramConnectStage",
    "PaymentCallbackOutcome",
    "SessionExpiryPolicy",
    "new_browser_session_id",
    "new_csrf_token",
    "new_handoff_nonce",
    "sha256_digest",
    "utc_from_timestamp",
    "utc_to_timestamp",
]
