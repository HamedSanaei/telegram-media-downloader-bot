"""Companion web boundary ports (T016).

Framework-free contracts so application and infrastructure layers stay decoupled from aiohttp and
``cryptography``. The bot signs handoff claims through ``HandoffSigner``; the companion verifies
them and resolves the claim through ``HandoffVerifier``, consumes each nonce exactly once through
``HandoffNonceRepository``, records interactive flow state through ``InteractiveFlowStore``, and
verifies provider payment callbacks through an explicitly registered ``ProviderCallbackVerifier``
registry. No port here can ever receive a bot token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class PaymentCallbackTrigger:
    """Normalized provider-neutral callback trigger produced by a callback adapter.

    A trigger locates the LOCAL order reference only. It is NEVER payment proof: the companion
    processor follows it with a point-in-time ``query_payment`` and only ``BillingService``
    settles the verified result. ``authentic`` records signature/credential checks for signed
    providers; unsigned wake-up callbacks stay authentic-as-trigger and are equally powerless.
    """

    provider_id: str
    order_reference: str | None
    authentic: bool = True


class PaymentCallbackAdapter(Protocol):
    """Companion-side, provider-specific normalizer of one untrusted callback request.

    Implementations parse the provider's own contract (signed JSON IPN, unsigned form POST, or a
    GET with no body) into a bounded ``PaymentCallbackTrigger``. They never settle anything and
    never expose provider secrets.
    """

    def normalize(
        self,
        *,
        method: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        body: bytes,
    ) -> PaymentCallbackTrigger:
        """Normalize one untrusted callback request into a bounded local trigger."""


class ProviderCallbackRegistry(Protocol):
    """Composition-resolved lookup of registered payment callback adapters."""

    def adapter_for(self, provider_id: str) -> PaymentCallbackAdapter | None:
        """Return the registered adapter for a provider, or None when none is registered."""


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
        input_value: object | None,
    ) -> InstagramConnectResult:
        """Advance the flow one step and return a sanitized domain view.

        ``input_value`` is a transient bounded value (a JSON object for identity+password+2FA
        steps); the flow never persists or logs secrets.
        """


class PaymentCallbackProcessor(Protocol):
    """Handles a normalized callback trigger with a server-side authoritative query + settle."""

    async def process(
        self,
        *,
        trigger: PaymentCallbackTrigger,
    ) -> PaymentCallbackOutcome:
        """Locate the local order, run the authoritative provider query, and settle ONLY a
        verified paid result. Never confirms from the callback payload itself."""
