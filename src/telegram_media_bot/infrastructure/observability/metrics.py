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
                "# HELP media_bot_queue_depth Current ARQ queue depth.",
                "# TYPE media_bot_queue_depth gauge",
                f"media_bot_queue_depth {queue_depth}",
            )
        )
        return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return _SAFE_LABEL.sub("_", value)[:64] or "unknown"
