"""VIP public Instagram credential-routing policy (T020).

This module only decides which explicit credential context may be attempted. Adapters remain
unaware of subscriptions and never retry or switch credentials themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram_media_bot.domain.credential_resolution import (
    ContentAccessScope,
    CredentialAttemptPhase,
    CredentialKind,
    CredentialPolicy,
    CredentialResolutionCategory,
)


@dataclass(frozen=True, slots=True)
class PublicFallbackState:
    policy: CredentialPolicy
    scope: ContentAccessScope = ContentAccessScope.UNKNOWN
    phase: CredentialAttemptPhase = CredentialAttemptPhase.NOT_STARTED
    user_generation: int | None = None
    operator_generation: int | None = None
    fallback_used: bool = False

    def initial_kind(self, *, user_session_healthy: bool) -> CredentialKind:
        if self.policy is CredentialPolicy.USER_ONLY:
            return CredentialKind.USER_INSTAGRAM
        if self.policy is CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK and user_session_healthy:
            return CredentialKind.USER_INSTAGRAM
        return CredentialKind.OPERATOR_PUBLIC

    def can_fallback(self, category: CredentialResolutionCategory) -> bool:
        return (
            self.policy is CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK
            and self.scope is not ContentAccessScope.USER_RESTRICTED
            and self.phase is CredentialAttemptPhase.USER_ATTEMPT
            and not self.fallback_used
            and category.is_credential_or_session_failure
        )

    def after_user_failure(self, category: CredentialResolutionCategory) -> PublicFallbackState:
        if not self.can_fallback(category):
            return PublicFallbackState(
                policy=self.policy,
                scope=self.scope,
                phase=CredentialAttemptPhase.FAILED,
                user_generation=self.user_generation,
                operator_generation=self.operator_generation,
                fallback_used=self.fallback_used,
            )
        return PublicFallbackState(
            policy=self.policy,
            scope=self.scope,
            phase=CredentialAttemptPhase.OPERATOR_ATTEMPT,
            user_generation=self.user_generation,
            operator_generation=self.operator_generation,
            fallback_used=True,
        )


def choose_public_policy(*, vip_active: bool, user_session_healthy: bool) -> PublicFallbackState:
    """Return the authoritative initial state for a public request."""
    if not vip_active:
        return PublicFallbackState(policy=CredentialPolicy.OPERATOR_PUBLIC)
    if user_session_healthy:
        return PublicFallbackState(policy=CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK)
    return PublicFallbackState(policy=CredentialPolicy.OPERATOR_PUBLIC)


__all__ = ["PublicFallbackState", "choose_public_policy"]
