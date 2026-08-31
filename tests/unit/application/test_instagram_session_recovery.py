from datetime import UTC, datetime

from telegram_media_bot.application.services.instagram_session_recovery import (
    eligible_session_recovery,
)
from telegram_media_bot.domain.credential_resolution import CredentialResolutionCategory
from telegram_media_bot.domain.models import JobId, JobKind, JobRecord, JobStatus


def _job(
    *, user_id: int = 1, generation: int = 4, status: JobStatus = JobStatus.FAILED
) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        JobId("job"),
        JobKind.DOWNLOAD,
        status,
        10,
        user_id,
        "https://example.com",
        None,
        "key",
        now,
        now,
        user_credential_generation=generation,
    )


def test_recovery_is_owner_and_generation_scoped() -> None:
    assert eligible_session_recovery(
        _job(), owner_user_id=1, previous_generation=4, failure=CredentialResolutionCategory.EXPIRED
    ).eligible
    assert not eligible_session_recovery(
        _job(user_id=2),
        owner_user_id=1,
        previous_generation=4,
        failure=CredentialResolutionCategory.EXPIRED,
    ).eligible
    assert not eligible_session_recovery(
        _job(), owner_user_id=1, previous_generation=3, failure=CredentialResolutionCategory.EXPIRED
    ).eligible
    assert not eligible_session_recovery(
        _job(),
        owner_user_id=1,
        previous_generation=4,
        failure=CredentialResolutionCategory.ADAPTER_AUTH,
    ).eligible


def test_cancellation_and_uncertain_delivery_never_recover() -> None:
    for status in (JobStatus.CANCELLED, JobStatus.DELIVERY_UNCERTAIN):
        assert not eligible_session_recovery(
            _job(status=status),
            owner_user_id=1,
            previous_generation=4,
            failure=CredentialResolutionCategory.CHALLENGE_REQUIRED,
        ).eligible
