"""Fail-closed policy for authenticated private Instagram media (T021)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_media_bot.application.services.entitlements import EntitlementService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.domain.credential_resolution import CredentialKind, CredentialPolicy
from telegram_media_bot.domain.errors import (
    CredentialChallengeRequiredError,
    CredentialExpiredError,
    CredentialNotFoundError,
    EntitlementBackendError,
    EntitlementInactiveError,
)
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.domain.subscriptions import Capability, EntitlementSnapshot


class PrivateMediaDecision(StrEnum):
    ALLOW = "allow"
    VIP_REQUIRED = "vip_required"
    CONNECT_REQUIRED = "connect_required"
    RECONNECT_REQUIRED = "reconnect_required"
    ACCESS_DENIED = "access_denied"
    UNKNOWN_DENIED = "unknown_denied"


@dataclass(frozen=True, slots=True)
class PrivateMediaAuthorization:
    decision: PrivateMediaDecision
    policy: CredentialPolicy
    credential_kind: CredentialKind


@dataclass(frozen=True, slots=True)
class PrivateMediaRequestAuthorization:
    entitlement_snapshot: EntitlementSnapshot
    user_credential_generation: int


def authorize_private_request(
    *,
    owner_user_id: int,
    entitlements: EntitlementService | None,
    connection: InstagramConnectionService | None,
    accepted_at: datetime,
) -> PrivateMediaRequestAuthorization:
    """Accept an owner-bound private request without probing content or operator credentials.

    Visibility is established later by the engine using only this owner's session. A connected
    Free account never reaches that engine path, and unavailable backends never authorize it.
    """
    if entitlements is None:
        raise EntitlementInactiveError("Private-media entitlement is unavailable")
    snapshot = entitlements.authorize(
        owner_user_id, Capability.INSTAGRAM_PRIVATE_MEDIA, accepted_at=accepted_at
    )
    if snapshot.owner_user_id != owner_user_id:
        raise EntitlementBackendError("Private-media entitlement is not bound to this owner")
    if connection is None:
        raise CredentialNotFoundError("Personal account connection is unavailable")
    credential = connection.status(owner_user_id)
    if credential is None:
        raise CredentialNotFoundError("Personal account is not connected")
    if credential.state is InstagramCredentialState.EXPIRED:
        raise CredentialExpiredError("Personal account session requires reconnection")
    if credential.state is InstagramCredentialState.CHALLENGE_REQUIRED:
        raise CredentialChallengeRequiredError("Personal account requires a login challenge")
    if credential.state is not InstagramCredentialState.CONNECTED:
        raise CredentialNotFoundError("Personal account is not connected")
    if credential.generation < 1:
        raise CredentialNotFoundError("Personal account generation is unavailable")
    return PrivateMediaRequestAuthorization(snapshot, credential.generation)


def authorize_private_media(
    *,
    entitlement: EntitlementSnapshot | None,
    user_credential_present: bool,
    user_session_healthy: bool,
    legitimate_visibility: bool | None,
) -> PrivateMediaAuthorization:
    """Authorize private content without ever considering the operator credential."""
    if entitlement is None or entitlement.capability is not Capability.INSTAGRAM_PRIVATE_MEDIA:
        return PrivateMediaAuthorization(
            PrivateMediaDecision.VIP_REQUIRED, CredentialPolicy.USER_ONLY, CredentialKind.NONE
        )
    if not user_credential_present:
        return PrivateMediaAuthorization(
            PrivateMediaDecision.CONNECT_REQUIRED, CredentialPolicy.USER_ONLY, CredentialKind.NONE
        )
    if not user_session_healthy:
        return PrivateMediaAuthorization(
            PrivateMediaDecision.RECONNECT_REQUIRED,
            CredentialPolicy.USER_ONLY,
            CredentialKind.USER_INSTAGRAM,
        )
    if legitimate_visibility is not True:
        decision = (
            PrivateMediaDecision.ACCESS_DENIED
            if legitimate_visibility is False
            else PrivateMediaDecision.UNKNOWN_DENIED
        )
        return PrivateMediaAuthorization(
            decision, CredentialPolicy.USER_ONLY, CredentialKind.USER_INSTAGRAM
        )
    return PrivateMediaAuthorization(
        PrivateMediaDecision.ALLOW, CredentialPolicy.USER_ONLY, CredentialKind.USER_INSTAGRAM
    )


__all__ = [
    "PrivateMediaAuthorization",
    "PrivateMediaDecision",
    "PrivateMediaRequestAuthorization",
    "authorize_private_media",
    "authorize_private_request",
]
