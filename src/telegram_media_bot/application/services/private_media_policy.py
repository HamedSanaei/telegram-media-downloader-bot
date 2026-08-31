"""Fail-closed policy for authenticated private Instagram media (T021)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from telegram_media_bot.domain.credential_resolution import CredentialKind, CredentialPolicy
from telegram_media_bot.domain.subscriptions import EntitlementSnapshot


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


def authorize_private_media(
    *,
    entitlement: EntitlementSnapshot | None,
    user_credential_present: bool,
    user_session_healthy: bool,
    legitimate_visibility: bool | None,
) -> PrivateMediaAuthorization:
    """Authorize private content without ever considering the operator credential."""
    if entitlement is None:
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


__all__ = ["PrivateMediaAuthorization", "PrivateMediaDecision", "authorize_private_media"]
