"""Bounded automatic recovery for explicitly recoverable terminal job failures.

Two remediation paths exist, both gated by the central ``recoverability`` vocabulary:

* **Cookie remediation** — after an administrator durably replaces/merges a provider's cookie,
  eligible cookie-recoverable failed jobs of exactly that provider are requeued.
* **App-fix recovery** — on startup, jobs that failed on an older app version with an
  explicitly recoverable application/extractor/runtime failure receive one recovery attempt per
  version.

Recovery is bounded (max attempts and max age), **batched** (per-pass size limits), limited to the
available outstanding-queue headroom under queue pressure, and never touches ``delivery_uncertain``,
cancelled, already-delivered, or unsupported-provider jobs. SQLite state transitions first, then
Redis enqueue happens; a startup reconciliation re-enqueues recovery jobs whose Redis enqueue never
landed, so a crash between the two cannot strand a requeued job forever.

``queue.max_jobs`` is ARQ **worker concurrency** (jobs running simultaneously), while the queue
pressure probe reads the Redis queue sorted set (``zcard``). Under ARQ's pessimistic execution a job
stays in that sorted set while it is waiting, while it runs, and while it is deferred/retried; it is
removed only at final success or failure. ``zcard`` is therefore the number of **outstanding ARQ
queue entries**, not merely waiting jobs. The pressure threshold is derived as ``queue.max_jobs *
queue_backlog_per_worker_slot`` — a heuristic for a few "waves" of outstanding work relative to what
one worker can run at once — never as a percentage of a non-existent fixed queue capacity. A batch
is trimmed to the remaining headroom below that threshold so recovery never overshoots the
outstanding-work budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from telegram_media_bot import __version__
from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.models import JobId, JobKind, JobRecord

logger = structlog.get_logger(__name__)

RecoveryNotifier = Callable[[JobRecord], Awaitable[None]]
#: Returns whether the durable job currently has no live ARQ job (safe to re-enqueue).
ArqStatusProbe = Callable[[JobId], Awaitable[bool]]
#: Returns the current queue depth (0 when unknown/unavailable).
QueueDepthProbe = Callable[[], Awaitable[int]]


class RecoverySummary:
    __slots__ = ("deferred", "discovered", "enqueue_failed", "requeued")

    def __init__(
        self,
        *,
        discovered: int = 0,
        deferred: int = 0,
        requeued: int = 0,
        enqueue_failed: int = 0,
    ) -> None:
        self.discovered = discovered
        self.deferred = deferred
        self.requeued = requeued
        self.enqueue_failed = enqueue_failed

    def merge(self, other: RecoverySummary) -> None:
        self.discovered += other.discovered
        self.deferred += other.deferred
        self.requeued += other.requeued
        self.enqueue_failed += other.enqueue_failed


class JobRecoveryService:
    def __init__(
        self,
        repository: JobRepository,
        queue: JobQueue,
        *,
        max_attempts: int = 2,
        max_age_days: int = 7,
        notify: RecoveryNotifier | None = None,
        remediation_batch_size: int = 20,
        startup_recovery_batch_size: int = 20,
        reconciliation_batch_size: int = 50,
        queue_pressure_threshold: int | None = None,
        max_recovery_per_user: int | None = None,
        queue_depth_probe: QueueDepthProbe | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._max_attempts = max_attempts
        self._max_age_days = max_age_days
        self._notify = notify
        self._remediation_batch_size = remediation_batch_size
        self._startup_recovery_batch_size = startup_recovery_batch_size
        self._reconciliation_batch_size = reconciliation_batch_size
        self._queue_pressure_threshold = queue_pressure_threshold or 0
        self._max_recovery_per_user = max_recovery_per_user
        self._queue_depth_probe = queue_depth_probe

    async def _recovery_batch_limit(self, configured: int) -> int | None:
        """Return the batch limit honoring outstanding-queue headroom, or ``None`` to defer.

        The probe reads the Redis queue sorted set (``zcard``), which under ARQ's pessimistic
        execution counts outstanding entries (waiting + running + deferred/retry), not worker
        concurrency. ``available = threshold - depth``; a batch is trimmed to at most that many
        jobs so recovery never pushes the outstanding-work count past the budget. When depth
        already meets the threshold, ``None`` defers the whole batch to a later maintenance pass.
        """
        if self._queue_pressure_threshold <= 0 or self._queue_depth_probe is None:
            return configured
        try:
            depth = await self._queue_depth_probe()
        except Exception:
            logger.warning("recovery_queue_depth_probe_failed")
            return configured
        headroom = self._queue_pressure_threshold - depth
        if headroom <= 0:
            return None
        return min(configured, headroom)

    async def remediate_cookies(self, cookie_service: CookieService) -> RecoverySummary:
        """Requeue the next bounded batch of eligible cookie failures for exactly ``cookie_service``.

        The provider's remediation is marked available so later maintenance passes keep draining the
        eligible candidate pool in bounded batches without the administrator re-uploading the cookie.
        """
        self._repository.mark_cookie_remediation_available(cookie_service, datetime.now(UTC))
        batch_limit = await self._recovery_batch_limit(self._remediation_batch_size)
        if batch_limit is None:
            summary = RecoverySummary(deferred=1)
            await logger.awarning(
                "recovery_deferred_queue_pressure",
                provider=cookie_service.value,
                queue_pressure_threshold=self._queue_pressure_threshold,
            )
        else:
            summary = await self._remediate_cookie_batch(cookie_service, limit=batch_limit)
        await logger.ainfo(
            "cookie_remediation_attempt",
            provider=cookie_service.value,
            discovered=summary.discovered,
            deferred=summary.deferred,
            requeued=summary.requeued,
            enqueue_failed=summary.enqueue_failed,
            batch_size=self._remediation_batch_size,
        )
        return summary

    async def _remediate_cookie_batch(
        self, cookie_service: CookieService, *, limit: int | None = None
    ) -> RecoverySummary:
        now = datetime.now(UTC)
        batch_limit = limit if limit is not None else self._remediation_batch_size
        if batch_limit <= 0:
            return RecoverySummary(deferred=1)
        candidates = self._repository.cookie_recovery_candidates(
            cookie_service,
            now=now,
            max_age_days=self._max_age_days,
            max_attempts=self._max_attempts,
            limit=batch_limit,
            max_per_user=self._max_recovery_per_user,
        )
        if not candidates:
            # The backlog for this provider is fully drained; stop maintenance draining.
            self._repository.clear_cookie_remediation_available(cookie_service)
        summary = RecoverySummary(discovered=len(candidates))
        for record in candidates:
            if await self._requeue(record, version=__version__, now=now):
                summary.requeued += 1
        return summary

    async def recover_maintenance_batch(self) -> RecoverySummary:
        """Drain the next bounded recovery batch from every provider with a fresh cookie available.

        Runs inside the existing maintenance lifecycle; respects outstanding-queue headroom and
        leaves the provider marker in place until its eligible candidate pool is exhausted.
        """
        summary = RecoverySummary()
        batch_limit = await self._recovery_batch_limit(self._remediation_batch_size)
        if batch_limit is None:
            summary.deferred = 1
            await logger.awarning(
                "recovery_deferred_queue_pressure",
                queue_pressure_threshold=self._queue_pressure_threshold,
            )
            return summary
        for cookie_service in self._repository.active_cookie_remediation_providers():
            partial = await self._remediate_cookie_batch(cookie_service, limit=batch_limit)
            summary.merge(partial)
        if summary.requeued or summary.discovered:
            await logger.ainfo(
                "recovery_maintenance_batch",
                discovered=summary.discovered,
                deferred=summary.deferred,
                requeued=summary.requeued,
            )
        return summary

    async def recover_after_app_fix(self, current_version: str | None = None) -> RecoverySummary:
        """Give eligible app-fix jobs one recovery attempt on a newer app version (bounded)."""
        version = current_version or __version__
        batch_limit = await self._recovery_batch_limit(self._startup_recovery_batch_size)
        if batch_limit is None:
            return RecoverySummary(deferred=1)
        now = datetime.now(UTC)
        candidates = self._repository.app_fix_recovery_candidates(
            version,
            now=now,
            max_age_days=self._max_age_days,
            max_attempts=self._max_attempts,
            limit=batch_limit,
            max_per_user=self._max_recovery_per_user,
        )
        summary = RecoverySummary(discovered=len(candidates))
        for record in candidates:
            if await self._requeue(record, version=version, now=now):
                summary.requeued += 1
        await logger.ainfo(
            "app_fix_recovery_attempt",
            app_version=version,
            discovered=summary.discovered,
            requeued=summary.requeued,
            enqueue_failed=summary.enqueue_failed,
            batch_size=self._startup_recovery_batch_size,
        )
        return summary

    async def reconcile_recovery_requeues(self, is_missing_in_arq: ArqStatusProbe) -> int:
        """Re-enqueue recovery-requeued jobs whose Redis enqueue never landed (crash gap).

        Unlike fresh recovery batches, this is durable-state repair: the job is already committed
        as QUEUED in SQLite, and leaving it permanently missing from Redis would strand it. It is
        therefore NOT gated by queue pressure — it only re-enqueues jobs the probe reports as
        missing, and converges whenever a missing job is detected.
        """
        pending = self._repository.recovery_requeues(self._reconciliation_batch_size)
        reenqueued = 0
        for record in pending:
            if not await is_missing_in_arq(record.job_id):
                continue
            if await self._enqueue(record):
                reenqueued += 1
        if reenqueued:
            await logger.ainfo("recovery_requeue_reconciliation", jobs_reenqueued=reenqueued)
        return reenqueued

    def pending_recoverable_count(self) -> int:
        return self._repository.pending_recoverable_count()

    @property
    def effective_queue_threshold(self) -> int:
        return self._queue_pressure_threshold

    async def queue_observability(self) -> tuple[int, int]:
        """Return ``(outstanding_depth, available_headroom)`` for aggregate metrics."""
        if self._queue_pressure_threshold <= 0 or self._queue_depth_probe is None:
            return (0, 0)
        try:
            depth = await self._queue_depth_probe()
        except Exception:
            return (0, 0)
        return depth, max(0, self._queue_pressure_threshold - depth)

    async def _requeue(
        self,
        record: JobRecord,
        *,
        version: str,
        now: datetime,
    ) -> bool:
        updated = self._repository.mark_recovery_requeued(record.job_id, version=version, now=now)
        if updated is None:
            # Cancel/status race: another path terminalized the job; leave it alone.
            return False
        enqueued = await self._enqueue(updated)
        if not enqueued:
            # The row is already QUEUED; startup reconciliation retries the enqueue.
            logger.warning(
                "recovery_enqueue_failed",
                job_id=record.job_id,
                recovery_attempt=updated.recovery_attempt_count,
            )
            return True
        if self._notify is not None and not updated.recovery_notification_sent:
            await self._notify(updated)
            self._repository.mark_recovery_notification_sent(record.job_id)
        return True

    async def _enqueue(self, record: JobRecord) -> bool:
        try:
            if record.kind is JobKind.INSPECTION:
                await self._queue.enqueue_inspection(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                )
            elif record.kind is JobKind.DOWNLOAD and record.mode is not None:
                await self._queue.enqueue_download(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                    mode=record.mode,
                    container=record.container,
                    container_policy=record.container_policy,
                    native_video_codec=record.native_video_codec,
                    selected_format_ids=record.selected_format_ids,
                    image_delivery_mode=record.image_delivery_mode,
                    story_delivery_mode=record.story_delivery_mode,
                )
            else:
                return False
        except Exception:
            logger.exception("recovery_enqueue_error", job_id=record.job_id)
            return False
        return True
