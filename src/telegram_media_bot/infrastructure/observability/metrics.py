from __future__ import annotations

import re
import threading
from collections import defaultdict

_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9_.-]")


class MetricsRegistry:
    """Dependency-free Prometheus registry for the small fixed project metric set."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_count = 0
        self._duration_sum = 0.0
        self._bytes = 0
        self._cleanup_bytes = 0
        self._cleanup_files = 0
        self._cleanup_directories = 0
        self._cleanup_failures = 0
        self._cleanup_duration = 0.0
        self._queue_depth = 0
        self._recoverable = {"cookie": 0, "app_fix": 0}
        self._recovery_deferred = 0
        self._recovery_batch_size = 0
        self._inbound_purged = 0
        self._inbound_stuck = 0
        self._effects_stale_pending = 0
        self._effects_marked_uncertain = 0
        self._recovery_effective_threshold = 0
        self._recovery_outstanding_queue_depth = 0
        self._recovery_available_headroom = 0

    def record_job(self, *, outcome: str, source: str = "unknown", error: str = "none") -> None:
        labels = (_label(outcome), _label(source), _label(error))
        with self._lock:
            self._jobs[labels] += 1

    def observe_duration(self, seconds: float) -> None:
        with self._lock:
            self._duration_count += 1
            self._duration_sum += max(0.0, seconds)

    def add_bytes(self, value: int) -> None:
        with self._lock:
            self._bytes += max(0, value)

    def record_recovery(self, kind: str) -> None:
        """Count one bounded automatic job recovery (``cookie`` or ``app_fix``)."""
        label = _label(kind)
        with self._lock:
            self._recoverable[label] += 1

    def record_recovery_deferred(self) -> None:
        """Count one recovery batch deferred under queue pressure."""
        with self._lock:
            self._recovery_deferred += 1

    def set_recoverable_batch_size(self, value: int) -> None:
        with self._lock:
            self._recovery_batch_size = max(0, value)

    def record_inbound_purged(self, count: int) -> None:
        with self._lock:
            self._inbound_purged += max(0, count)

    def set_inbound_stuck(self, value: int) -> None:
        with self._lock:
            self._inbound_stuck = max(0, value)

    def record_effects_marked_uncertain(self, count: int) -> None:
        with self._lock:
            self._effects_marked_uncertain += max(0, count)

    def set_effects_stale_pending(self, value: int) -> None:
        with self._lock:
            self._effects_stale_pending = max(0, value)

    def set_recovery_effective_threshold(self, value: int) -> None:
        with self._lock:
            self._recovery_effective_threshold = max(0, value)

    def set_recovery_outstanding_queue_depth(self, value: int) -> None:
        with self._lock:
            self._recovery_outstanding_queue_depth = max(0, value)

    def set_recovery_available_headroom(self, value: int) -> None:
        with self._lock:
            self._recovery_available_headroom = max(0, value)

    def set_recoverable_pending(self, value: int) -> None:
        with self._lock:
            self._recoverable["pending"] = max(0, value)

    def record_workspace_cleanup(
        self,
        *,
        files_deleted: int,
        directories_deleted: int,
        bytes_reclaimed: int,
        failed_paths: int,
        duration_seconds: float,
    ) -> None:
        with self._lock:
            self._cleanup_files += max(0, files_deleted)
            self._cleanup_directories += max(0, directories_deleted)
            self._cleanup_bytes += max(0, bytes_reclaimed)
            self._cleanup_failures += max(0, failed_paths)
            self._cleanup_duration += max(0.0, duration_seconds)

    def set_queue_depth(self, value: int) -> None:
        with self._lock:
            self._queue_depth = max(0, value)

    def render(self) -> str:
        with self._lock:
            jobs = dict(self._jobs)
            duration_count = self._duration_count
            duration_sum = self._duration_sum
            byte_count = self._bytes
            cleanup_bytes = self._cleanup_bytes
            cleanup_files = self._cleanup_files
            cleanup_directories = self._cleanup_directories
            cleanup_failures = self._cleanup_failures
            cleanup_duration = self._cleanup_duration
            queue_depth = self._queue_depth
            recoverable = dict(self._recoverable)
            recovery_deferred = self._recovery_deferred
            recovery_batch_size = self._recovery_batch_size
            inbound_purged = self._inbound_purged
            inbound_stuck = self._inbound_stuck
            effects_stale_pending = self._effects_stale_pending
            effects_marked_uncertain = self._effects_marked_uncertain
            recovery_effective_threshold = self._recovery_effective_threshold
            recovery_outstanding_queue_depth = self._recovery_outstanding_queue_depth
            recovery_available_headroom = self._recovery_available_headroom
        lines = [
            "# HELP media_bot_jobs_total Completed jobs by outcome, source and error category.",
            "# TYPE media_bot_jobs_total counter",
        ]
        for (outcome, source, error), value in sorted(jobs.items()):
            lines.append(
                f'media_bot_jobs_total{{outcome="{outcome}",source="{source}",error="{error}"}} {value}'
            )
        lines.extend(
            (
                "# HELP media_bot_job_duration_seconds Total processing duration.",
                "# TYPE media_bot_job_duration_seconds summary",
                f"media_bot_job_duration_seconds_count {duration_count}",
                f"media_bot_job_duration_seconds_sum {duration_sum:.6f}",
                "# HELP media_bot_delivered_bytes_total Successfully delivered bytes.",
                "# TYPE media_bot_delivered_bytes_total counter",
                f"media_bot_delivered_bytes_total {byte_count}",
                "# HELP media_bot_workspace_reclaimed_bytes_total Reclaimed job workspace bytes.",
                "# TYPE media_bot_workspace_reclaimed_bytes_total counter",
                f"media_bot_workspace_reclaimed_bytes_total {cleanup_bytes}",
                "# HELP media_bot_workspace_deleted_files_total Deleted job workspace files.",
                "# TYPE media_bot_workspace_deleted_files_total counter",
                f"media_bot_workspace_deleted_files_total {cleanup_files}",
                "# HELP media_bot_workspace_deleted_directories_total Deleted job directories.",
                "# TYPE media_bot_workspace_deleted_directories_total counter",
                f"media_bot_workspace_deleted_directories_total {cleanup_directories}",
                "# HELP media_bot_workspace_cleanup_failures_total Failed workspace path removals.",
                "# TYPE media_bot_workspace_cleanup_failures_total counter",
                f"media_bot_workspace_cleanup_failures_total {cleanup_failures}",
                "# HELP media_bot_workspace_cleanup_duration_seconds_total Cleanup duration.",
                "# TYPE media_bot_workspace_cleanup_duration_seconds_total counter",
                f"media_bot_workspace_cleanup_duration_seconds_total {cleanup_duration:.6f}",
            )
        )
        lines.extend(
            (
                "# HELP media_bot_recoverable_jobs_total Automatically recovered jobs by kind.",
                "# TYPE media_bot_recoverable_jobs_total counter",
            )
        )
        for kind in sorted(("cookie", "app_fix")):
            lines.append(
                f'media_bot_recoverable_jobs_total{{kind="{kind}"}} {recoverable.get(kind, 0)}'
            )
        lines.append(f"media_bot_recoverable_jobs_pending {recoverable.get('pending', 0)}")
        lines.extend(
            (
                "# HELP media_bot_recoverable_jobs_deferred_queue_pressure_total "
                "Recovery batches deferred under queue pressure.",
                "# TYPE media_bot_recoverable_jobs_deferred_queue_pressure_total counter",
                f"media_bot_recoverable_jobs_deferred_queue_pressure_total {recovery_deferred}",
                "# HELP media_bot_recoverable_jobs_batch_size Configured recovery batch size.",
                "# TYPE media_bot_recoverable_jobs_batch_size gauge",
                f"media_bot_recoverable_jobs_batch_size {recovery_batch_size}",
                "# HELP media_bot_inbound_updates_purged_total Purged terminal inbox rows.",
                "# TYPE media_bot_inbound_updates_purged_total counter",
                f"media_bot_inbound_updates_purged_total {inbound_purged}",
                "# HELP media_bot_inbound_updates_stuck Unfinished updates older than the stuck threshold.",
                "# TYPE media_bot_inbound_updates_stuck gauge",
                f"media_bot_inbound_updates_stuck {inbound_stuck}",
                f"media_bot_telegram_effects_stale_pending {effects_stale_pending}",
                f"media_bot_telegram_effects_marked_uncertain_total {effects_marked_uncertain}",
                f"media_bot_recovery_effective_queue_threshold {recovery_effective_threshold}",
                "# HELP media_bot_recovery_outstanding_queue_depth Outstanding ARQ queue entries "
                "(waiting + running + deferred/retry).",
                "# TYPE media_bot_recovery_outstanding_queue_depth gauge",
                f"media_bot_recovery_outstanding_queue_depth {recovery_outstanding_queue_depth}",
                f"media_bot_recovery_available_queue_headroom {recovery_available_headroom}",
                "# HELP media_bot_queue_depth Current ARQ queue depth.",
                "# TYPE media_bot_queue_depth gauge",
                f"media_bot_queue_depth {queue_depth}",
            )
        )
        return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return _SAFE_LABEL.sub("_", value)[:64] or "unknown"
