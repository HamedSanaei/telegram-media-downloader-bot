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

    def set_queue_depth(self, value: int) -> None:
        with self._lock:
            self._queue_depth = max(0, value)

    def render(self) -> str:
        with self._lock:
            jobs = dict(self._jobs)
            duration_count = self._duration_count
            duration_sum = self._duration_sum
            byte_count = self._bytes
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
                "# HELP media_bot_queue_depth Current ARQ queue depth.",
                "# TYPE media_bot_queue_depth gauge",
                f"media_bot_queue_depth {queue_depth}",
            )
        )
        return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return _SAFE_LABEL.sub("_", value)[:64] or "unknown"
