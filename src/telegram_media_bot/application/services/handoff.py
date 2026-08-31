"""Handoff link creation (bot) and exactly-once exchange (companion) service (T016).

The bot side mints short-lived purpose-bound claims and signs them; the companion side verifies
signature/time/purpose, then consumes the one-time nonce exactly once so replayed or concurrent
presentations can never establish a second browser session for the same nonce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_media_bot.application.ports.companion import (
    HandoffNonceRepository,
    HandoffSigner,
    HandoffVerifier,
)
from telegram_media_bot.domain.web_companion import (
    HandoffClaim,
    HandoffPurpose,
    HandoffVerification,
    HandoffVerificationOutcome,
    new_handoff_nonce,
    sha256_digest,
)


class HandoffLinkService:
    """Bot-side factory for signed, single-use, purpose-bound connection links."""

    def __init__(self, signer: HandoffSigner, *, lifetime: timedelta) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("handoff lifetime must be positive")
        self._signer = signer
        self._lifetime = lifetime

    def create(
        self,
        *,
        purpose: HandoffPurpose,
        owner_user_id: int,
        now: datetime | None = None,
    ) -> str:
        issued = (now or datetime.now(UTC)).astimezone(UTC)
        claim = HandoffClaim(
            purpose=purpose,
            owner_user_id=owner_user_id,
            nonce=new_handoff_nonce(),
            issued_at=issued,
            expires_at=issued + self._lifetime,
        )
        return self._signer.sign(claim)


class CompanionHandoffService:
    """Companion-side verifier that additionally guarantees each presented nonce is used once."""

    def __init__(
        self,
        verifier: HandoffVerifier,
        nonce_repository: HandoffNonceRepository,
    ) -> None:
        self._verifier = verifier
        self._nonce_repository = nonce_repository

    def exchange(
        self,
        token: str,
        purpose: HandoffPurpose,
        now: datetime | None = None,
    ) -> HandoffVerification:
        """Verify and atomically consume the presented token; returns generic outcomes.

        ``purpose`` is positional so the bound method can be injected directly into the web
        companion (``Callable[[str, HandoffPurpose], object]``).
        """
        result = self._verifier.verify(token, now=now or datetime.now(UTC))
        if not result.verified or result.claim is None:
            return result
        claim = result.claim
        if claim.purpose is not purpose:
            return HandoffVerification(HandoffVerificationOutcome.WRONG_PURPOSE, claim)
        consumed = self._nonce_repository.reserve_once(
            nonce_hash=sha256_digest(claim.nonce),
            purpose=purpose,
            owner_user_id=claim.owner_user_id,
            expires_at=claim.expires_at,
            now=datetime.now(UTC),
        )
        if not consumed:
            return HandoffVerification(HandoffVerificationOutcome.REPLAYED, claim)
        return result


__all__ = ["CompanionHandoffService", "HandoffLinkService"]
