from telegram_media_bot.application.services.public_fallback import (
    PublicFallbackState,
    choose_public_policy,
)
from telegram_media_bot.domain.credential_resolution import (
    ContentAccessScope,
    CredentialAttemptPhase,
    CredentialKind,
    CredentialPolicy,
    CredentialResolutionCategory,
)


def test_free_users_always_use_operator_even_with_a_session() -> None:
    state = choose_public_policy(vip_active=False, user_session_healthy=True)
    assert state.policy is CredentialPolicy.OPERATOR_PUBLIC
    assert state.initial_kind(user_session_healthy=True) is CredentialKind.OPERATOR_PUBLIC


def test_vip_healthy_session_starts_with_user() -> None:
    state = choose_public_policy(vip_active=True, user_session_healthy=True)
    assert state.policy is CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK
    assert state.initial_kind(user_session_healthy=True) is CredentialKind.USER_INSTAGRAM


def test_only_typed_user_auth_failure_allows_one_switch() -> None:
    state = PublicFallbackState(
        policy=CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK,
        phase=CredentialAttemptPhase.USER_ATTEMPT,
        scope=ContentAccessScope.PUBLIC,
    )
    switched = state.after_user_failure(CredentialResolutionCategory.EXPIRED)
    assert switched.phase is CredentialAttemptPhase.OPERATOR_ATTEMPT
    assert switched.fallback_used is True
    assert switched.can_fallback(CredentialResolutionCategory.EXPIRED) is False


def test_restricted_and_local_failures_never_switch() -> None:
    state = PublicFallbackState(
        policy=CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK,
        phase=CredentialAttemptPhase.USER_ATTEMPT,
        scope=ContentAccessScope.USER_RESTRICTED,
    )
    assert (
        state.after_user_failure(CredentialResolutionCategory.EXPIRED).phase
        is CredentialAttemptPhase.FAILED
    )
    public = PublicFallbackState(
        policy=CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK,
        phase=CredentialAttemptPhase.USER_ATTEMPT,
        scope=ContentAccessScope.PUBLIC,
    )
    assert (
        public.after_user_failure(CredentialResolutionCategory.MATERIALIZATION_LOCAL).fallback_used
        is False
    )
