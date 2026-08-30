from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.services.job_recovery_service import JobRecoveryService
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ErrorCategory,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    StoryDeliveryMode,
)
from telegram_media_bot.domain.recoverability import (
    RecoverabilityClass,
    recovery_class_for_job,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository


def _job(
    job_id: str,
    url: str,
    *,
    status: JobStatus = JobStatus.FAILED,
    mode: DownloadMode | None = DownloadMode.BEST,
    story_delivery_mode: StoryDeliveryMode | None = None,
    cancel_requested: bool = False,
    user_id: int = 2,
    age_days: float = 0.0,
) -> JobRecord:
    now = datetime.now(UTC)
    created = now - timedelta(days=age_days)
    return JobRecord(
        job_id=JobId(job_id),
        kind=JobKind.DOWNLOAD,
        status=status,
        chat_id=1,
        user_id=user_id,
        url=url,
        mode=mode,
        idempotency_key=f"key-{job_id}",
        created_at=created,
        updated_at=created,
        container=None,
        container_policy=ContainerPolicy.NATIVE_ONLY,
        selected_format_ids=(),
        story_delivery_mode=story_delivery_mode,
        cancel_requested=cancel_requested,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SqliteJobRepository:
    result = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    result.initialize()
    return result


class FakeQueue:
    def __init__(self, *, depth: int = 0, fail_enqueue: bool = False) -> None:
        self.downloads: list[dict[str, object]] = []
        self.inspections: list[dict[str, object]] = []
        self._depth = depth
        self._fail_enqueue = fail_enqueue

    async def queue_depth(self) -> int:
        return self._depth

    async def enqueue_download(self, **kwargs: object) -> JobId:
        if self._fail_enqueue:
            raise RuntimeError("redis down")
        self.downloads.append(kwargs)
        return JobId(str(kwargs.get("job_id", "")))

    async def enqueue_inspection(self, **kwargs: object) -> JobId:
        self.inspections.append(kwargs)
        return JobId(str(kwargs.get("job_id", "")))


# --- classification ------------------------------------------------------------


def _load(repository: SqliteJobRepository, job_id: str) -> JobRecord:
    record = repository.get_job(JobId(job_id))
    assert record is not None
    return record


def test_recoverability_classification_central_and_typed() -> None:
    assert recovery_class_for_job(
        _job("x", "https://www.instagram.com/p/AB/"), ErrorCategory.AUTHENTICATION
    ) is (RecoverabilityClass.AFTER_COOKIE_CHANGE)
    assert (
        recovery_class_for_job(
            _job("x", "https://www.instagram.com/p/AB/"), ErrorCategory.GALLERY_COOKIES_EXPIRED
        )
        is RecoverabilityClass.AFTER_COOKIE_CHANGE
    )
    assert (
        recovery_class_for_job(_job("x", "https://www.instagram.com/p/AB/"), ErrorCategory.INTERNAL)
        is RecoverabilityClass.AFTER_APP_FIX
    )
    # Unsupported provider => never recoverable even for an INTERNAL failure.
    assert recovery_class_for_job(
        _job("x", "https://www.pornhub.com/view_video.php"), ErrorCategory.INTERNAL
    ) is (RecoverabilityClass.NONE)


def test_unsupported_provider_never_marked_recoverable(repository: SqliteJobRepository) -> None:
    repository.create_job(_job("ph", "https://www.pornhub.com/view_video.php"))
    repository.record_recoverable_failure(JobId("ph"), ErrorCategory.AUTHENTICATION, "1.3.6")
    assert _load(repository, "ph").recoverability_class is None


def test_delivery_uncertain_is_never_a_recovery_candidate(repository: SqliteJobRepository) -> None:
    repository.create_job(
        _job("unc", "https://www.instagram.com/p/AB/", status=JobStatus.DELIVERY_UNCERTAIN)
    )
    repository.record_recoverable_failure(JobId("unc"), ErrorCategory.AUTHENTICATION, "1.3.6")
    now = datetime.now(UTC)
    candidates = repository.cookie_recovery_candidates(
        CookieService.INSTAGRAM, now=now, max_age_days=7, max_attempts=2
    )
    assert candidates == ()


def test_already_delivered_job_is_not_a_candidate(repository: SqliteJobRepository) -> None:
    repository.create_job(
        _job("done", "https://www.instagram.com/p/AB/", status=JobStatus.SUCCEEDED)
    )
    repository.record_recoverable_failure(JobId("done"), ErrorCategory.AUTHENTICATION, "1.3.6")
    now = datetime.now(UTC)
    assert (
        repository.cookie_recovery_candidates(
            CookieService.INSTAGRAM, now=now, max_age_days=7, max_attempts=2
        )
        == ()
    )


def test_cancelled_job_is_not_a_candidate(repository: SqliteJobRepository) -> None:
    repository.create_job(_job("canc", "https://www.instagram.com/p/AB/", cancel_requested=True))
    repository.record_recoverable_failure(JobId("canc"), ErrorCategory.AUTHENTICATION, "1.3.6")
    now = datetime.now(UTC)
    assert (
        repository.cookie_recovery_candidates(
            CookieService.INSTAGRAM, now=now, max_age_days=7, max_attempts=2
        )
        == ()
    )


# --- cookie remediation ---------------------------------------------------------


def test_cookie_remediation_requeues_only_matching_provider(
    repository: SqliteJobRepository,
) -> None:
    repository.create_job(_job("ig", "https://www.instagram.com/p/AB/"))
    repository.create_job(_job("yt", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    repository.create_job(_job("tk", "https://www.tiktok.com/@x/video/1"))
    for job_id in ("ig", "yt", "tk"):
        repository.record_recoverable_failure(JobId(job_id), ErrorCategory.AUTHENTICATION, "1.3.6")

    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(repository, queue, max_attempts=2, max_age_days=7)

    import asyncio

    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))

    requeued = [d["job_id"] for d in cast(FakeQueue, queue).downloads]
    assert requeued == [JobId("ig")]
    assert summary.requeued == 1
    assert _load(repository, "ig").status is JobStatus.QUEUED
    assert _load(repository, "yt").status is JobStatus.FAILED
    assert _load(repository, "tk").status is JobStatus.FAILED


# --- app-fix recovery -----------------------------------------------------------


def test_app_fix_one_recovery_attempt_per_new_version(repository: SqliteJobRepository) -> None:
    repository.create_job(_job("bug", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    # Failed on version X with an INTERNAL/app-fix category.
    repository.record_recoverable_failure(JobId("bug"), ErrorCategory.INTERNAL, "1.3.6")

    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(repository, queue, max_attempts=2, max_age_days=7)

    import asyncio

    # Same version restart => no recovery.
    summary = asyncio.run(service.recover_after_app_fix("1.3.6"))
    assert summary.requeued == 0
    assert _load(repository, "bug").status is JobStatus.FAILED

    # Deploy version Y => one recovery attempt.
    summary = asyncio.run(service.recover_after_app_fix("1.3.7"))
    assert summary.requeued == 1
    assert _load(repository, "bug").status is JobStatus.QUEUED

    # Job is now QUEUED; if it fails again on Y and restarts on Y => no second attempt.
    repository.transition(
        JobId("bug"), JobStatus.FAILED, error_category=ErrorCategory.INTERNAL, error_summary="e"
    )
    repository.record_recoverable_failure(JobId("bug"), ErrorCategory.INTERNAL, "1.3.7")
    summary = asyncio.run(service.recover_after_app_fix("1.3.7"))
    assert summary.requeued == 0


# --- story delivery mode preservation (Feature 3 x Feature 2) --------------------


def test_cookie_remediation_preserves_story_delivery_mode(repository: SqliteJobRepository) -> None:
    record = _job(
        "story",
        "https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
        story_delivery_mode=StoryDeliveryMode.FILE,
    )
    repository.create_job(record)
    repository.record_recoverable_failure(JobId("story"), ErrorCategory.AUTHENTICATION, "1.3.6")

    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(repository, queue, max_attempts=2, max_age_days=7)

    import asyncio

    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))

    assert cast(FakeQueue, queue).downloads[0]["story_delivery_mode"] is StoryDeliveryMode.FILE
    assert _load(repository, "story").story_delivery_mode is StoryDeliveryMode.FILE


# --- Hardening 2: bounded / gradual recoverable-job requeue ---------------------


def _seed_failures(
    repository: SqliteJobRepository,
    count: int,
    *,
    url: str = "https://www.instagram.com/p/AB/",
    prefix: str = "ig",
    user_id: int = 2,
) -> None:
    for index in range(count):
        job_id = f"{prefix}-{index}"
        repository.create_job(_job(job_id, url, user_id=user_id))
        repository.record_recoverable_failure(JobId(job_id), ErrorCategory.AUTHENTICATION, "1.3.6")


def test_cookie_remediation_is_bounded_to_batch_size(repository: SqliteJobRepository) -> None:
    _seed_failures(repository, 50)
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(
        repository, queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 20
    assert len(cast(FakeQueue, queue).downloads) == 20
    # The other 30 stay failed/recoverable for later maintenance passes.
    assert repository.pending_recoverable_count() == 30


def test_maintenance_pass_drains_next_batch(repository: SqliteJobRepository) -> None:
    _seed_failures(repository, 50)
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(
        repository, queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    # No re-upload needed: the provider marker stays until the backlog drains.
    second = asyncio.run(service.recover_maintenance_batch())
    assert second.requeued == 20
    third = asyncio.run(service.recover_maintenance_batch())
    assert third.requeued == 10
    # Fully drained: the provider marker is cleared and further passes are no-ops.
    final = asyncio.run(service.recover_maintenance_batch())
    assert final.requeued == 0
    assert repository.pending_recoverable_count() == 0
    assert repository.active_cookie_remediation_providers() == ()


def test_cookie_remediation_is_provider_isolated(repository: SqliteJobRepository) -> None:
    _seed_failures(repository, 20, url="https://www.instagram.com/p/AB/", prefix="ig")
    _seed_failures(repository, 20, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", prefix="yt")
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(
        repository, queue, max_attempts=2, max_age_days=7, remediation_batch_size=100
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 20
    requeued_ids = {str(d["job_id"]) for d in cast(FakeQueue, queue).downloads}
    assert all(job_id.startswith("ig-") for job_id in requeued_ids)
    assert repository.pending_recoverable_count() == 20  # only YouTube remains


def test_queue_pressure_defers_recovery(repository: SqliteJobRepository) -> None:
    _seed_failures(repository, 5)
    queue = cast(JobQueue, FakeQueue(depth=50))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=20,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.deferred == 1
    assert summary.requeued == 0
    assert cast(FakeQueue, queue).downloads == []
    assert repository.pending_recoverable_count() == 5
    # Still marked available so a later (quiet) maintenance pass drains it.
    assert repository.active_cookie_remediation_providers() == (CookieService.INSTAGRAM,)


def test_headroom_limits_batch_to_available_slots(repository: SqliteJobRepository) -> None:
    """threshold 12, outstanding depth 10, batch 20 => at most 2 recovery jobs (12 - 10)."""
    _seed_failures(repository, 20)
    queue = cast(JobQueue, FakeQueue(depth=10))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 2
    assert len(cast(FakeQueue, queue).downloads) == 2
    # Remaining candidates stay recoverable for later maintenance passes.
    assert repository.pending_recoverable_count() == 18


def test_headroom_below_pressure_allows_full_bounded_batch(
    repository: SqliteJobRepository,
) -> None:
    """threshold 12, outstanding depth 3, batch 20 => at most 9 recovery jobs."""
    _seed_failures(repository, 20)
    queue = cast(JobQueue, FakeQueue(depth=3))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 9
    assert len(cast(FakeQueue, queue).downloads) == 9


def test_depth_counts_running_and_waiting_entries(repository: SqliteJobRepository) -> None:
    """The probe is outstanding ARQ entries (zcard), so running jobs also consume headroom.

    threshold 12 with 3 running + 7 waiting = 10 outstanding entries => only 2 headroom.
    """
    _seed_failures(repository, 20)
    # zcard semantics: the sorted set still contains running jobs until they finish/fail.
    queue = cast(JobQueue, FakeQueue(depth=10))  # 3 running + 7 waiting, all outstanding
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 2


def test_full_pressure_defers_all_recovery(repository: SqliteJobRepository) -> None:
    """threshold 12, outstanding depth 12 => zero automatic recovery jobs."""
    _seed_failures(repository, 5)
    queue = cast(JobQueue, FakeQueue(depth=12))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.deferred == 1
    assert summary.requeued == 0
    assert cast(FakeQueue, queue).downloads == []


def test_headroom_applies_to_app_fix_recovery(repository: SqliteJobRepository) -> None:
    """App-version recovery honors the same outstanding-queue headroom."""
    for index in range(10):
        job_id = f"bug-{index}"
        repository.create_job(_job(job_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        repository.record_recoverable_failure(JobId(job_id), ErrorCategory.INTERNAL, "1.3.6")
    queue = cast(JobQueue, FakeQueue(depth=10))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        startup_recovery_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    summary = asyncio.run(service.recover_after_app_fix("1.3.7"))
    assert summary.requeued == 2
    assert len(cast(FakeQueue, queue).downloads) == 2


def test_maintenance_drain_resumes_when_depth_drops(repository: SqliteJobRepository) -> None:
    """Outstanding-depth drain: high depth defers; once depth drops, maintenance uses new headroom."""
    _seed_failures(repository, 20)
    queue = cast(JobQueue, FakeQueue(depth=12))
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=20,
        queue_pressure_threshold=12,
        queue_depth_probe=queue.queue_depth,
    )
    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert len(cast(FakeQueue, queue).downloads) == 0
    # Fresh traffic finishes; depth drops to 3. Maintenance drains up to 9 more.
    cast(FakeQueue, queue)._depth = 3
    drain = asyncio.run(service.recover_maintenance_batch())
    assert drain.requeued == 9


def test_reconciliation_not_gated_by_queue_pressure(repository: SqliteJobRepository) -> None:
    """Durable-state repair (job already QUEUED in SQLite, missing from Redis) is not throttled."""
    _seed_failures(repository, 2)
    broken_queue = cast(JobQueue, FakeQueue(fail_enqueue=True))
    service = JobRecoveryService(
        repository, broken_queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert all(_load(repository, f"ig-{i}").status is JobStatus.QUEUED for i in range(2))

    healthy_queue = cast(JobQueue, FakeQueue(depth=100))
    reconciler = JobRecoveryService(
        repository,
        healthy_queue,
        max_attempts=2,
        max_age_days=7,
        queue_pressure_threshold=12,
        queue_depth_probe=healthy_queue.queue_depth,
    )

    async def missing_in_arq(job_id: JobId) -> bool:
        return True

    reenqueued = asyncio.run(reconciler.reconcile_recovery_requeues(missing_in_arq))
    assert reenqueued == 2


def test_enqueue_failure_is_reconciled_later(repository: SqliteJobRepository) -> None:
    _seed_failures(repository, 3)
    broken_queue = cast(JobQueue, FakeQueue(fail_enqueue=True))
    service = JobRecoveryService(
        repository, broken_queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    # The SQLite transition committed (status = QUEUED) even though Redis enqueue failed.
    assert summary.requeued == 3
    assert all(_load(repository, f"ig-{i}").status is JobStatus.QUEUED for i in range(3))
    # Startup reconciliation re-enqueues only the jobs actually missing from ARQ.
    healthy_queue = cast(JobQueue, FakeQueue())
    reconciler = JobRecoveryService(repository, healthy_queue, max_attempts=2, max_age_days=7)

    async def missing_in_arq(job_id: JobId) -> bool:
        return True

    reenqueued = asyncio.run(reconciler.reconcile_recovery_requeues(missing_in_arq))
    assert reenqueued == 3
    assert len(cast(FakeQueue, healthy_queue).downloads) == 3


def test_repeated_reconciliation_does_not_duplicate_delivery(
    repository: SqliteJobRepository,
) -> None:
    _seed_failures(repository, 2)
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(repository, queue, max_attempts=2, max_age_days=7)

    async def present_in_arq(job_id: JobId) -> bool:
        return False

    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    first = asyncio.run(service.reconcile_recovery_requeues(present_in_arq))
    second = asyncio.run(service.reconcile_recovery_requeues(present_in_arq))
    assert first == 0 and second == 0
    # A second reconciliation pass that finds the jobs already live in Redis does nothing.
    assert len(cast(FakeQueue, queue).downloads) == 2


def test_recovery_ordering_is_oldest_first_with_per_user_cap(
    repository: SqliteJobRepository,
) -> None:
    # One user has 10 old failures; another has 2 recent ones. The oldest 2 overall belong to
    # the backlog user, but the per-user cap keeps them from monopolizing the batch.
    _seed_failures(repository, 2, prefix="u2", user_id=99, url="https://www.instagram.com/p/AA/")
    _seed_failures(repository, 10, prefix="u1", user_id=7, url="https://www.instagram.com/p/BB/")
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(
        repository,
        queue,
        max_attempts=2,
        max_age_days=7,
        remediation_batch_size=5,
        max_recovery_per_user=1,
    )
    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    requeued_ids = sorted(str(d["job_id"]) for d in cast(FakeQueue, queue).downloads)
    assert len(requeued_ids) == 2
    # One from each user, not five from the backlog user.
    assert any(job_id.startswith("u1-") for job_id in requeued_ids)
    assert any(job_id.startswith("u2-") for job_id in requeued_ids)


def test_age_and_attempt_limits_never_enter_recovery(repository: SqliteJobRepository) -> None:
    too_old = _job("old", "https://www.instagram.com/p/AB/", age_days=30)
    repository.create_job(too_old)
    repository.record_recoverable_failure(JobId("old"), ErrorCategory.AUTHENTICATION, "1.3.6")
    _seed_failures(repository, 1, prefix="fresh")
    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(
        repository, queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 1
    assert [str(d["job_id"]) for d in cast(FakeQueue, queue).downloads] == ["fresh-0"]

    # Attempt-cap: bump the attempt counter to max_attempts (2) so the job can never be
    # requeued again, even though the cookie is still valid.
    now = datetime.now(UTC)
    repository.transition(
        JobId("fresh-0"),
        JobStatus.FAILED,
        error_category=ErrorCategory.AUTHENTICATION,
        error_summary="e",
    )
    assert repository.mark_recovery_requeued(JobId("fresh-0"), version="1.3.6", now=now) is not None
    repository.transition(
        JobId("fresh-0"),
        JobStatus.FAILED,
        error_category=ErrorCategory.AUTHENTICATION,
        error_summary="e",
    )
    repository.record_recoverable_failure(JobId("fresh-0"), ErrorCategory.AUTHENTICATION, "1.3.6")
    assert _load(repository, "fresh-0").recovery_attempt_count == 2
    second = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    assert second.requeued == 0
