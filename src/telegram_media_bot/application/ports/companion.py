"""Companion web boundary ports (T016).

Framework-free contracts so application and infrastructure layers stay decoupled from aiohttp and
``cryptography``. The bot signs handoff claims through ``HandoffSigner``; the companion verifies
them and resolves the claim through ``HandoffVerifier``, consumes each nonce exactly once through
``HandoffNonceRepository``, records interactive flow state through ``InteractiveFlowStore``, and
verifies provider payment callbacks through an explicitly registered ``ProviderCallbackVerifier``
registry. No port here can ever receive a bot token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.web_companion import (
    HandoffClaim,
    HandoffPurpose,
    HandoffVerification,
    InstagramConnectResult,
    PaymentCallbackOutcome,
)


class HandoffSigner(Protocol):
    """Bot-side contract that turns a claim into a compact signed token."""

    def sign(self, claim: HandoffClaim) -> str:
        """Return a signed token (fragment-safe, no secrets) for the given claim."""


class HandoffVerifier(Protocol):
    """Companion-side contract that verifies an untrusted presented token."""

    def verify(self, token: str, *, now: datetime) -> HandoffVerification:
        """Verify signature, envelope, purpose/audience, and time window.

        The outcome must be generic; no detail about WHY a verification failed is surfaced.
        """


class HandoffNonceRepository(Protocol):
    """Durable exactly-once consumption of a handoff nonce."""

    def initialize(self) -> None: ...

    def reserve_once(
        self,
        *,
        nonce_hash: str,
        purpose: HandoffPurpose,
        owner_user_id: int,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        """Atomically reserve ``nonce_hash``. Returns True only the first time it is reserved."""

    def purge_expired(self, *, now: datetime, before: datetime) -> int:
        """Delete nonce rows older than ``before``; returns the number removed."""


class InteractiveFlowStore(Protocol):
    """Bounded in-memory interactive flow state (transient secrets, never durable)."""

    def set(self, key: str, value: str) -> None: ...

    def get(self, key: str, *, consume: bool = False) -> str | None: ...

    def drop(self, key: str) -> None: ...


class ProviderCallbackVerifier(Protocol):
    """Companion-side verifier for one registered payment provider's machine callbacks."""

    def verify_callback(self, provider_payload: bytes) -> bool:
        """Verify provider signature/freshness/replay and return whether it is trustworthy."""


class ProviderCallbackRegistry(Protocol):
    """Composition-resolved lookup of registered payment callback verifiers."""

    def verifier_for(self, provider_id: str) -> ProviderCallbackVerifier | None:
        """Return the registered verifier for a provider, or None when none is registered."""


class InstagramConnectFlow(Protocol):
    """Browser-side Instagram connection flow port.

    T016 registers a disabled implementation that returns ``NOT_AVAILABLE``; the real transient
    login/2FA flow (T018) replaces it at composition without touching the HTTP boundary.
    Inputs are bounded plain strings, never credentials stored by the companion.
    """

    async def step(
        self,
        *,
        owner_user_id: int,
        session_id: str,
        input_value: str | None,
    ) -> InstagramConnectResult:
        """Advance the flow one step and return a sanitized domain view."""


class PaymentCallbackProcessor(Protocol):
    """Handles a cryptographically-verified payment callback by handing it to the billing service."""

    async def process(
        self,
        *,
        provider_id: str,
        provider_payload: bytes,
    ) -> PaymentCallbackOutcome:
        """Return a normalized outcome after server-side economic handling.

        Must never confirm/activate an entitlement itself; the billing service does that.
        """
