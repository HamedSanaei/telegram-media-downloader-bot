from datetime import UTC, datetime

from telegram_media_bot.application.services.private_media_policy import (
    PrivateMediaDecision,
    authorize_private_media,
)
from telegram_media_bot.domain.subscriptions import Capability, EntitlementSnapshot, GrantId, PlanId


def _vip() -> EntitlementSnapshot:
    return EntitlementSnapshot(
        capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
        accepted_at=datetime.now(UTC),
        authorized_until=datetime.now(UTC),
        plan_id=PlanId("plan"),
        grant_id=GrantId("grant"),
    )


def test_private_policy_is_fail_closed_and_user_only() -> None:
    assert (
        authorize_private_media(
            entitlement=None,
            user_credential_present=True,
            user_session_healthy=True,
            legitimate_visibility=True,
        ).decision
        is PrivateMediaDecision.VIP_REQUIRED
    )
    assert (
        authorize_private_media(
            entitlement=_vip(),
            user_credential_present=False,
            user_session_healthy=False,
            legitimate_visibility=None,
        ).decision
        is PrivateMediaDecision.CONNECT_REQUIRED
    )
    assert (
        authorize_private_media(
            entitlement=_vip(),
            user_credential_present=True,
            user_session_healthy=True,
            legitimate_visibility=None,
        ).decision
        is PrivateMediaDecision.UNKNOWN_DENIED
    )
    allowed = authorize_private_media(
        entitlement=_vip(),
        user_credential_present=True,
        user_session_healthy=True,
        legitimate_visibility=True,
    )
    assert allowed.decision is PrivateMediaDecision.ALLOW
    assert allowed.credential_kind.value == "user_instagram"
    assert allowed.policy.value == "user_only"
