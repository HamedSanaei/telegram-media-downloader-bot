"""Owner- and generation-scoped recovery decisions for Instagram sessions (T022)."""

from __future__ import annotations

from dataclasses import dataclass

from telegram_media_bot.domain.credential_resolution import CredentialResolutionCategory
from telegram_media_bot.domain.models import JobRecord, JobStatus


@dataclass(frozen=True, slots=True)
class SessionRecoveryDecision:
    eligible: bool
    reason: str


def eligible_session_recovery(
    job: JobRecord,
    *,
    owner_user_id: int,
    previous_generation: int,
    failure: CredentialResolutionCategory,
) -> SessionRecoveryDecision:
    """Check the durable exclusions before a reconnect can rebind a job."""
    if job.user_id != owner_user_id:
        return SessionRecoveryDecision(False, "owner_mismatch")
    if job.user_credential_generation != previous_generation:
        return SessionRecoveryDecision(False, "generation_mismatch")
    if job.status in {JobStatus.CANCELLED, JobStatus.DELIVERY_UNCERTAIN, JobStatus.SUCCEEDED}:
        return SessionRecoveryDecision(False, "terminal_exclusion")
    if failure not in {
        CredentialResolutionCategory.EXPIRED,
        CredentialResolutionCategory.CHALLENGE_REQUIRED,
    }:
        return SessionRecoveryDecision(False, "non_session_failure")
    return SessionRecoveryDecision(True, "same_owner_generation")


__all__ = ["SessionRecoveryDecision", "eligible_session_recovery"]
