"""Ed25519 handoff signing/verification for the companion boundary (T016).

The bot holds the private key and signs short-lived purpose-bound claims; the companion holds only
the public key and verifies. Tokens are fragment-safe URL-safe base64 without padding, formed as
``payload.signature`` over the JSON-encoded claim envelope, with constant-time signature checking
and a bounded acceptable clock-skew window. No upstream secret, nonce, or token value is ever
logged here.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from telegram_media_bot.domain.web_companion import (
    HandoffClaim,
    HandoffPurpose,
    HandoffVerification,
    HandoffVerificationOutcome,
    utc_from_timestamp,
    utc_to_timestamp,
)

_ENVELOPE_VERSION = 1


class HandoffCryptoError(ValueError):
    """Unsafe key material or malformed token/claim input."""


class _Ed25519HandoffMixin:
    """Shared serialization helpers for the signer/verifier pair."""

    @staticmethod
    def _encode_key(private_key: Ed25519PrivateKey) -> str:
        return base64.urlsafe_b64encode(
            private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        ).decode("ascii")

    @staticmethod
    def _claim_to_payload(claim: HandoffClaim) -> bytes:
        envelope = {
            "v": _ENVELOPE_VERSION,
            "purpose": claim.purpose.value,
            "owner": claim.owner_user_id,
            "nonce": claim.nonce,
            "iat": utc_to_timestamp(claim.issued_at),
            "exp": utc_to_timestamp(claim.expires_at),
        }
        return json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    @staticmethod
    def _b64(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(value: str) -> bytes:
        # Restore the canonical no-padding length before decoding.
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)

    @classmethod
    def _parse_payload(cls, payload: str) -> HandoffClaim:
        try:
            raw = json.loads(cls._unb64(payload).decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HandoffCryptoError("malformed handoff payload") from exc
        if not isinstance(raw, dict) or raw.get("v") != _ENVELOPE_VERSION:
            raise HandoffCryptoError("unknown handoff envelope version")
        try:
            purpose = HandoffPurpose(str(raw["purpose"]))
            owner = int(raw["owner"])
            nonce = str(raw["nonce"])
            issued = utc_from_timestamp(int(raw["iat"]))
            expires = utc_from_timestamp(int(raw["exp"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise HandoffCryptoError("incomplete handoff claim") from exc
        if not nonce:
            raise HandoffCryptoError("empty handoff nonce")
        return HandoffClaim(
            purpose=purpose,
            owner_user_id=owner,
            nonce=nonce,
            issued_at=issued,
            expires_at=expires,
        )


class Ed25519HandoffSigner(_Ed25519HandoffMixin):
    """Bot-side signer; the private key exists only in the bot's configuration surface."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> tuple[Ed25519HandoffSigner, str]:
        """Generate a fresh keypair, returning the signer and the encoded private key bytes."""
        key = Ed25519PrivateKey.generate()
        return cls(key), cls._encode_key(key)

    @classmethod
    def from_encoded(cls, encoded: str) -> Ed25519HandoffSigner:
        try:
            key = Ed25519PrivateKey.from_private_bytes(cls._unb64(encoded))
        except (ValueError, TypeError) as exc:
            raise HandoffCryptoError("invalid handoff private key") from exc
        return cls(key)

    def sign(self, claim: HandoffClaim) -> str:
        payload = self._b64(self._claim_to_payload(claim))
        signature = self._private_key.sign(payload.encode("ascii"))
        return f"{payload}.{self._b64(signature)}"


class Ed25519HandoffVerifier(_Ed25519HandoffMixin):
    """Companion-side verifier; only the public key is present in this process."""

    def __init__(self, public_key: Ed25519PublicKey, *, max_clock_skew_seconds: int = 30) -> None:
        if max_clock_skew_seconds < 0:
            raise ValueError("clock skew must be non-negative")
        self._public_key = public_key
        self._max_clock_skew = timedelta(seconds=max_clock_skew_seconds)

    @classmethod
    def from_private_encoded(
        cls, private_encoded: str, *, max_clock_skew_seconds: int = 30
    ) -> Ed25519HandoffVerifier:
        """Derive the verifying public key from the encoded private key (operator convenience)."""
        signer = Ed25519HandoffSigner.from_encoded(private_encoded)
        return cls(signer._private_key.public_key(), max_clock_skew_seconds=max_clock_skew_seconds)

    @classmethod
    def from_public_pem(
        cls, pem: bytes, *, max_clock_skew_seconds: int = 30
    ) -> Ed25519HandoffVerifier:
        try:
            key = serialization.load_pem_public_key(pem)
        except (ValueError, TypeError) as exc:
            raise HandoffCryptoError("invalid handoff public key") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise HandoffCryptoError("handoff public key is not Ed25519")
        return cls(key, max_clock_skew_seconds=max_clock_skew_seconds)

    @staticmethod
    def _public_key_to_pem(public_key: Ed25519PublicKey) -> bytes:
        return public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def public_key_pem(self) -> bytes:
        return self._public_key_to_pem(self._public_key)

    def verify(self, token: str, *, now: datetime) -> HandoffVerification:
        try:
            payload, signature = token.split(".", 1)
        except ValueError:
            return HandoffVerification(HandoffVerificationOutcome.MALFORMED)
        try:
            self._public_key.verify(self._unb64(signature), payload.encode("ascii"))
        except InvalidSignature:
            return HandoffVerification(HandoffVerificationOutcome.INVALID_SIGNATURE)
        except HandoffCryptoError:
            return HandoffVerification(HandoffVerificationOutcome.MALFORMED)
        try:
            claim = self._parse_payload(payload)
        except HandoffCryptoError:
            return HandoffVerification(HandoffVerificationOutcome.MALFORMED)
        now_utc = now.astimezone(UTC)
        if claim.issued_at - now_utc > self._max_clock_skew:
            return HandoffVerification(HandoffVerificationOutcome.NOT_YET_VALID)
        if claim.expires_at + self._max_clock_skew < now_utc:
            return HandoffVerification(HandoffVerificationOutcome.EXPIRED)
        return HandoffVerification(HandoffVerificationOutcome.VERIFIED, claim)

    def verify_for_purpose(
        self, token: str, *, purpose: HandoffPurpose, now: datetime
    ) -> HandoffVerification:
        result = self.verify(token, now=now)
        if result.outcome is HandoffVerificationOutcome.VERIFIED:
            assert result.claim is not None
            if result.claim.purpose is not purpose:
                return HandoffVerification(HandoffVerificationOutcome.WRONG_PURPOSE, result.claim)
        return result


__all__ = [
    "Ed25519HandoffSigner",
    "Ed25519HandoffVerifier",
    "HandoffCryptoError",
]
