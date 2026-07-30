from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from telegram_media_bot.application.ports.usage_analytics import UsageAnalyticsRepository
from telegram_media_bot.domain.analytics import UsageActivity
from telegram_media_bot.domain.errors import PersistenceError
from telegram_media_bot.domain.models import (
    DownloadMode,
    JobKind,
    JobStatus,
    OutputContainer,
)


class SqliteUsageAnalyticsRepository(UsageAnalyticsRepository):
    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def load_activity(self, start_at: datetime, end_at: datetime) -> tuple[UsageActivity, ...]:
        try:
            with sqlite3.connect(self._path, timeout=30) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT
                        jobs.user_id,
                        jobs.kind,
                        jobs.status,
                        jobs.created_at,
                        jobs.source,
                        jobs.mode,
                        jobs.container,
                        COALESCE(download_usage_events.delivered_bytes, 0) AS delivered_bytes
                    FROM jobs
                    LEFT JOIN download_usage_events
                        ON download_usage_events.job_id = jobs.job_id
                    WHERE jobs.created_at >= ? AND jobs.created_at <= ?
                    ORDER BY jobs.created_at
                    """,
                    (
                        start_at.isoformat(timespec="microseconds"),
                        end_at.isoformat(timespec="microseconds"),
                    ),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("Usage analytics query failed") from exc
        return tuple(_activity_from_row(row) for row in rows)


def _activity_from_row(row: sqlite3.Row) -> UsageActivity:
    return UsageActivity(
        user_id=int(row["user_id"]),
        kind=JobKind(str(row["kind"])),
        status=JobStatus(str(row["status"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        source=str(row["source"]) if row["source"] is not None else None,
        mode=DownloadMode(str(row["mode"])) if row["mode"] is not None else None,
        container=(
            OutputContainer(str(row["container"])) if row["container"] is not None else None
        ),
        delivered_bytes=int(row["delivered_bytes"]),
    )
