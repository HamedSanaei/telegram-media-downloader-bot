from __future__ import annotations

import gc
import sqlite3
import warnings
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.persistence.sqlite_usage_analytics import (
    SqliteUsageAnalyticsRepository,
)


def test_sqlite_usage_analytics_maps_durable_jobs_without_mutating_them(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    SqliteJobRepository(path).initialize()
    now = datetime.now(UTC)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, first_name, last_activity_at)
            VALUES (20, 'User', ?)
            """,
            (now.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, status, chat_id, user_id, url, mode, container,
                container_policy, idempotency_key, created_at, updated_at
            ) VALUES (
                'download-job', 'download', 'succeeded', 20, 20,
                'https://example.test/video', 'video_1080', 'mp4',
                'guaranteed', 'key', ?, ?
            )
            """,
            (now.isoformat(), now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO download_usage_events (
                job_id, user_id, usage_date, succeeded, delivered_bytes, created_at
            ) VALUES ('download-job', 20, ?, 1, 4096, ?)
            """,
            (now.date().isoformat(), now.isoformat()),
        )
        connection.commit()

    activity = SqliteUsageAnalyticsRepository(path).load_activity(
        now - timedelta(days=1),
        now + timedelta(days=1),
    )

    assert len(activity) == 1
    assert activity[0].source is None
    assert activity[0].mode is not None and activity[0].mode.value == "video_1080"
    assert activity[0].container is not None and activity[0].container.value == "mp4"
    assert activity[0].delivered_bytes == 4096
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_load_activity_deterministically_closes_its_connection(tmp_path: Path) -> None:
    """`load_activity` must release its SQLite connection even when GC runs later.

    Regression: the repository used `with sqlite3.connect(...)`, which commits but never
    closes the connection; every call leaked a connection until CPython GC finalized it,
    emitting `ResourceWarning: unclosed database` in production and test runs.
    """
    path = tmp_path / "jobs.sqlite3"
    SqliteJobRepository(path).initialize()
    repository = SqliteUsageAnalyticsRepository(path)
    now = datetime.now(UTC)

    assert repository.load_activity(now - timedelta(days=1), now + timedelta(days=1)) == ()

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        gc.collect()
        gc.collect()
