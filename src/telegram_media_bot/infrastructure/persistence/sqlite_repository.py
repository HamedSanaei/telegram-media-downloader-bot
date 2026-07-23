from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.domain.errors import (
    JobNotFoundError,
    PersistenceError,
    SelectionExpiredError,
    SelectionOwnershipError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    ErrorCategory,
    JobCounts,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaInfo,
    MediaKind,
    SelectionRecord,
    SelectionToken,
)

_ACTIVE_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.RETRYING.value,
    JobStatus.DELIVERING.value,
    JobStatus.DELIVERY_UNCERTAIN.value,
)
_CANCELLABLE_STATUSES = tuple(
    status
    for status in _ACTIVE_STATUSES
    if status not in {JobStatus.DELIVERING.value, JobStatus.DELIVERY_UNCERTAIN.value}
)


class SqliteJobRepository(JobRepository):
    """Small WAL-backed durable store shared by the bot and worker processes."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("Durable state operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS selections (
                    token TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    media_json TEXT NOT NULL,
                    allowed_modes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS selections_expires_idx ON selections(expires_at);

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    mode TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status_message_id INTEGER,
                    source TEXT,
                    error_category TEXT,
                    error_summary TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    delivery_file_id TEXT,
                    delivery_file_unique_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_idempotency_idx
                    ON jobs(idempotency_key, status);
                CREATE INDEX IF NOT EXISTS jobs_updated_idx ON jobs(updated_at);
                CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);

                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    blocked_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def healthy(self) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            return row is not None and row[0] == "ok"
        except PersistenceError:
            return False

    def save_selection(self, selection: SelectionRecord) -> None:
        media = {
            "media_id": selection.media.media_id,
            "title": selection.media.title,
            "source": selection.media.source,
            "kind": selection.media.kind.value,
            "webpage_url": selection.media.webpage_url,
            "uploader": selection.media.uploader,
            "duration_seconds": selection.media.duration_seconds,
            "thumbnail_url": selection.media.thumbnail_url,
            "item_count": selection.media.item_count,
            "estimated_size_bytes": selection.media.estimated_size_bytes,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO selections (
                    token, owner_user_id, chat_id, media_json, allowed_modes_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.token,
                    selection.owner_user_id,
                    selection.chat_id,
                    json.dumps(media, ensure_ascii=False, separators=(",", ":")),
                    json.dumps([mode.value for mode in selection.allowed_modes]),
                    _dump_datetime(selection.created_at),
                    _dump_datetime(selection.expires_at),
                ),
            )

    def get_selection(self, token: SelectionToken, owner_user_id: int) -> SelectionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM selections WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            raise SelectionExpiredError("Selection does not exist or has expired")
        if int(row["owner_user_id"]) != owner_user_id:
            raise SelectionOwnershipError("Selection belongs to another user")
        selection = _selection_from_row(row)
        if selection.expired:
            raise SelectionExpiredError("Selection has expired")
        return selection

    def create_job(self, record: JobRecord) -> JobRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
            existing = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE idempotency_key = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (record.idempotency_key, *_ACTIVE_STATUSES),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return _job_from_row(existing)
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, status, chat_id, user_id, url, mode, idempotency_key,
                    created_at, updated_at, status_message_id, source, error_category,
                    error_summary, cancel_requested, delivery_file_id,
                    delivery_file_unique_id, attempt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _job_values(record),
            )
            connection.execute("COMMIT")
        return record

    def get_job(self, job_id: JobId) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _job_from_row(row) if row is not None else None

    def find_active_job(self, idempotency_key: str) -> JobRecord | None:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs WHERE idempotency_key = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (idempotency_key, *_ACTIVE_STATUSES),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def set_status_message(self, job_id: JobId, message_id: int) -> None:
        self._update(job_id, status_message_id=message_id, updated_at=_now_text())

    def transition(
        self,
        job_id: JobId,
        status: JobStatus,
        *,
        source: str | None = None,
        error_category: ErrorCategory | None = None,
        error_summary: str | None = None,
        delivery_file_id: str | None = None,
        delivery_file_unique_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": _now_text(),
            "error_category": error_category.value if error_category else None,
            "error_summary": error_summary,
        }
        if source is not None:
            values["source"] = source
        if delivery_file_id is not None:
            values["delivery_file_id"] = delivery_file_id
        if delivery_file_unique_id is not None:
            values["delivery_file_unique_id"] = delivery_file_unique_id
        if attempt is not None:
            values["attempt"] = attempt
        self._update(job_id, **values)

    def request_cancel(self, job_id: JobId, owner_user_id: int) -> bool:
        placeholders = ",".join("?" for _ in _CANCELLABLE_STATUSES)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET cancel_requested = 1, updated_at = ?
                WHERE job_id = ? AND user_id = ? AND status IN ({placeholders})
                """,
                (_now_text(), job_id, owner_user_id, *_CANCELLABLE_STATUSES),
            )
            return cursor.rowcount == 1

    def is_cancel_requested(self, job_id: JobId) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row is not None and bool(row["cancel_requested"])

    def reconcile_abandoned(self, older_than: datetime) -> tuple[JobRecord, ...]:
        cutoff = _dump_datetime(older_than)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN (?, ?) AND updated_at < ?
                ORDER BY updated_at
                """,
                (JobStatus.RUNNING.value, JobStatus.DELIVERING.value, cutoff),
            ).fetchall()
            for row in rows:
                next_status = (
                    JobStatus.DELIVERY_UNCERTAIN
                    if row["status"] == JobStatus.DELIVERING.value
                    else JobStatus.QUEUED
                )
                category = (
                    ErrorCategory.DELIVERY_UNCERTAIN.value
                    if next_status is JobStatus.DELIVERY_UNCERTAIN
                    else None
                )
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, error_category = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (next_status.value, category, _now_text(), row["job_id"]),
                )
            connection.execute("COMMIT")
        recovered: list[JobRecord] = []
        for row in rows:
            current = self.get_job(JobId(str(row["job_id"])))
            if current is not None:
                recovered.append(current)
        return tuple(recovered)

    def purge_expired(self, now: datetime, job_retention_days: int) -> int:
        retention_cutoff = _dump_datetime(now - timedelta(days=job_retention_days))
        with self._connect() as connection:
            selections = connection.execute(
                "DELETE FROM selections WHERE expires_at <= ?", (_dump_datetime(now),)
            ).rowcount
            terminal = tuple(status.value for status in JobStatus if status.terminal)
            placeholders = ",".join("?" for _ in terminal)
            jobs = connection.execute(
                f"DELETE FROM jobs WHERE status IN ({placeholders}) AND updated_at < ?",
                (*terminal, retention_cutoff),
            ).rowcount
        return max(0, selections) + max(0, jobs)

    def failed_jobs(self, limit: int = 10) -> tuple[JobRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE status IN (?, ?)
                ORDER BY updated_at DESC LIMIT ?
                """,
                (JobStatus.FAILED.value, JobStatus.DELIVERY_UNCERTAIN.value, limit),
            ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def counts(self) -> JobCounts:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return JobCounts(
            queued=counts.get(JobStatus.QUEUED.value, 0),
            running=counts.get(JobStatus.RUNNING.value, 0)
            + counts.get(JobStatus.DELIVERING.value, 0),
            retrying=counts.get(JobStatus.RETRYING.value, 0),
            failed=counts.get(JobStatus.FAILED.value, 0)
            + counts.get(JobStatus.DELIVERY_UNCERTAIN.value, 0),
        )

    def block_user(self, user_id: int, blocked_by: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO blocked_users (user_id, blocked_by, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, blocked_by, _now_text()),
            )

    def unblock_user(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))

    def is_user_blocked(self, user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row is not None

    def _update(self, job_id: JobId, **values: Any) -> None:
        columns = ", ".join(f"{name} = ?" for name in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {columns} WHERE job_id = ?", (*values.values(), job_id)
            )
        if cursor.rowcount != 1:
            raise JobNotFoundError("Job does not exist")


def _selection_from_row(row: sqlite3.Row) -> SelectionRecord:
    raw = json.loads(str(row["media_json"]))
    media = MediaInfo(
        media_id=str(raw["media_id"]),
        title=str(raw["title"]),
        source=str(raw["source"]),
        kind=MediaKind(str(raw["kind"])),
        webpage_url=str(raw["webpage_url"]),
        uploader=str(raw["uploader"]) if raw.get("uploader") is not None else None,
        duration_seconds=raw.get("duration_seconds"),
        thumbnail_url=(str(raw["thumbnail_url"]) if raw.get("thumbnail_url") else None),
        item_count=raw.get("item_count"),
        estimated_size_bytes=raw.get("estimated_size_bytes"),
    )
    return SelectionRecord(
        token=SelectionToken(str(row["token"])),
        owner_user_id=int(row["owner_user_id"]),
        chat_id=int(row["chat_id"]),
        media=media,
        allowed_modes=tuple(
            DownloadMode(value) for value in json.loads(str(row["allowed_modes_json"]))
        ),
        created_at=_load_datetime(str(row["created_at"])),
        expires_at=_load_datetime(str(row["expires_at"])),
    )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=JobId(str(row["job_id"])),
        kind=JobKind(str(row["kind"])),
        status=JobStatus(str(row["status"])),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        url=str(row["url"]),
        mode=DownloadMode(str(row["mode"])) if row["mode"] else None,
        idempotency_key=str(row["idempotency_key"]),
        created_at=_load_datetime(str(row["created_at"])),
        updated_at=_load_datetime(str(row["updated_at"])),
        status_message_id=(int(row["status_message_id"]) if row["status_message_id"] else None),
        source=str(row["source"]) if row["source"] else None,
        error_category=(
            ErrorCategory(str(row["error_category"])) if row["error_category"] else None
        ),
        error_summary=str(row["error_summary"]) if row["error_summary"] else None,
        cancel_requested=bool(row["cancel_requested"]),
        delivery_file_id=str(row["delivery_file_id"]) if row["delivery_file_id"] else None,
        delivery_file_unique_id=(
            str(row["delivery_file_unique_id"]) if row["delivery_file_unique_id"] else None
        ),
        attempt=int(row["attempt"]),
    )


def _job_values(record: JobRecord) -> tuple[Any, ...]:
    return (
        record.job_id,
        record.kind.value,
        record.status.value,
        record.chat_id,
        record.user_id,
        record.url,
        record.mode.value if record.mode else None,
        record.idempotency_key,
        _dump_datetime(record.created_at),
        _dump_datetime(record.updated_at),
        record.status_message_id,
        record.source,
        record.error_category.value if record.error_category else None,
        record.error_summary,
        int(record.cancel_requested),
        record.delivery_file_id,
        record.delivery_file_unique_id,
        record.attempt,
    )


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _now_text() -> str:
    return _dump_datetime(datetime.now(UTC))
