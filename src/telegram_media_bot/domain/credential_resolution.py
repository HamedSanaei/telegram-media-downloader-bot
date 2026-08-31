"""Credential resolution and operator attestation domain model (T019).

Typed, framework-free vocabulary for choosing a media credential safely. Adapters receive an
explicit ``CredentialContext`` and never branch on VIP/subscription policy. The operator credential
is the existing canonical cookie file; it supplies public access only once a current ``OPERATOR_PUBLIC``
attestation (a dedicated zero-follow Instagram account bound to a keyed verifier of the file's
Instagram records) is valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CredentialKind(StrEnum):
    """Which credential source a single attempt should use."""

    NONE = "none"
    OPERATOR_PUBLIC = "operator_public"
    USER_INSTAGRAM = "user_instagram"


class CredentialPolicy(StrEnum):
    """Business-routing policy selected in application code (never in adapters/Telegram)."""

    OPERATOR_PUBLIC = "operator_public"
    USER_FIRST_PUBLIC_FALLBACK = "user_first_public_fallback"
    USER_ONLY = "user_only"


class ContentAccessScope(StrEnum):
    """Normalized visibility classification produced by inspection (T020/T021 use this)."""

    PUBLIC = "public"
    USER_RESTRICTED = "user_restricted"
    UNKNOWN = "unknown"


class CredentialResolutionCategory(StrEnum):
    """Stable, typed credential routing failures (never matched by exception-string text)."""

    NONE = "none"
    NO_CREDENTIAL = "no_credential"
    OWNER_MISMATCH = "owner_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    REVOKED = "revoked"
    DISCONNECTED = "disconnected"
    EXPIRED = "expired"
    CHALLENGE_REQUIRED = "challenge_required"
    LEASE_BUSY = "lease_busy"
    DECRYPT_FAILED = "decrypt_failed"
    OPERATOR_UNATTESTED = "operator_unattested"
    OPERATOR_ATTESTATION_STALE = "operator_attestation_stale"
    OPERATOR_EXPIRED = "operator_expired"
    MATERIALIZATION_LOCAL = "materialization_local"
    ADAPTER_AUTH = "adapter_auth"

    @property
    def is_credential_or_session_failure(self) -> bool:
        """True only for failures that may (in T020) warrant an operator-public fallback.

        Covers expired/invalid/login-required/credential-rejected user-session failures. Local,
        schema, size, delivery, cancellation, private-scope, and generic failures are excluded by
        construction.
        """
        return self in {
            CredentialResolutionCategory.EXPIRED,
            CredentialResolutionCategory.CHALLENGE_REQUIRED,
            CredentialResolutionCategory.REVOKED,
            CredentialResolutionCategory.DISCONNECTED,
            CredentialResolutionCategory.ADAPTER_AUTH,
        }


@dataclass(frozen=True, slots=True)
class CredentialContext:
    """Explicit, owner-safe credential request context for one engine attempt (never a secret)."""

    kind: CredentialKind
    policy: CredentialPolicy
    #: User credential generation when kind is USER_INSTAGRAM (resolver-safe, non-secret).
    user_generation: int | None = None
    #: Operator attestation generation selected for OPERATOR_PUBLIC.
    operator_generation: int | None = None

    @classmethod
    def none(cls, policy: CredentialPolicy = CredentialPolicy.OPERATOR_PUBLIC) -> CredentialContext:
        return cls(kind=CredentialKind.NONE, policy=policy)


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """A prepared, bounded credential handle for one attempt (never contains cookie bytes)."""

    context: CredentialContext
    #: On USER_INSTAGRAM, a short-lived materialized cookie path leased to this attempt.
    materialized_cookie_path: str | None = None

    @classmethod
    def operator_public(cls) -> ResolvedCredential:
        return cls(
            context=CredentialContext(
                kind=CredentialKind.OPERATOR_PUBLIC,
                policy=CredentialPolicy.OPERATOR_PUBLIC,
            )
        )

    @classmethod
    def none(
        cls, policy: CredentialPolicy = CredentialPolicy.OPERATOR_PUBLIC
    ) -> ResolvedCredential:
        return cls(context=CredentialContext.none(policy))

    def cookie_override(self) -> str | None:
        """Return the ephemeral cookie path, or ``None`` for operator/no-credential contexts."""
        if self.context.kind is CredentialKind.USER_INSTAGRAM:
            if self.materialized_cookie_path is None:
                raise ValueError("user credential context has no materialized cookie path")
            return self.materialized_cookie_path
        return None


@dataclass(frozen=True, slots=True)
class PublicOnlyAttestation:
    """Durable operator public-only attestation (dedicated zero-follow account)."""

    operator_generation: int
    attested_at: datetime
    actor_role: str
    #: Keyed HMAC/SHA-256 verifier of the canonical file's Instagram records at attestation time.
    keyed_verifier: str


def operator_ig_records_verifier(records: tuple[str, ...], *, key: bytes | None) -> str:
    """Deterministic keyed verifier over the canonical file's sorted Instagram records.

    When ``key`` is None a plain SHA-256 digest is used (best effort); with a configured key the
    value is an HMAC-SHA-256. The result detects any replacement/tamper of the Instagram cookie
    records and invalidates ``OPERATOR_PUBLIC`` attestation. Never logged; the records themselves
    contain secret material and must stay out of logs/metrics.
    """
    import hashlib
    import hmac as _hmac

    payload = "\n".join(sorted(records)).encode("utf-8")
    if key:
        return _hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ContentAccessScope",
    "CredentialContext",
    "CredentialKind",
    "CredentialPolicy",
    "CredentialResolutionCategory",
    "PublicOnlyAttestation",
    "ResolvedCredential",
    "operator_ig_records_verifier",
]
