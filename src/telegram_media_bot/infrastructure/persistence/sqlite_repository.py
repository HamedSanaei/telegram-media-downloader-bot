from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.domain.errors import (
    JobCancelledError,
    JobNotFoundError,
    PersistenceError,
    SelectionExpiredError,
    SelectionOwnershipError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryItemRecord,
    DeliveryItemStatus,
    DeliveryMethod,
    DeliveryProvider,
    DownloadMode,
    ErrorCategory,
    JobCancellationResult,
    JobCounts,
    JobId,
    JobKind,
    JobRecord,
    JobRecoveryRecord,
    JobStatus,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    RecoveryDecision,
    SelectionRecord,
    SelectionToken,
    SizeConfidence,
    UserProfile,
)

_ACTIVE_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.RETRYING.value,
    JobStatus.DELIVERING.value,
    JobStatus.DELIVERY_UNCERTAIN.value,
)
_CANCELLABLE_STATUSES = tuple(
    status for status in _ACTIVE_STATUSES if status != JobStatus.DELIVERY_UNCERTAIN.value
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
                    container TEXT,
                    container_policy TEXT NOT NULL DEFAULT 'native_only',
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

                CREATE TABLE IF NOT EXISTS delivery_items (
                    job_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    method TEXT NOT NULL,
                    recipient_message_id INTEGER,
                    file_id TEXT,
                    file_unique_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, ordinal),
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS delivery_items_status_idx
                    ON delivery_items(status, updated_at);

                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    blocked_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    private_chat_id INTEGER,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    language_code TEXT,
                    is_premium INTEGER,
                    first_started_at TEXT,
                    last_started_at TEXT,
                    last_activity_at TEXT NOT NULL,
                    start_count INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    successful_download_count INTEGER NOT NULL DEFAULT 0,
                    failed_download_count INTEGER NOT NULL DEFAULT 0,
                    delivered_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS users_last_activity_idx
                    ON users(last_activity_at);

                CREATE TABLE IF NOT EXISTS user_usage_daily (
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    successful_download_count INTEGER NOT NULL DEFAULT 0,
                    failed_download_count INTEGER NOT NULL DEFAULT 0,
                    delivered_bytes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, usage_date),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS download_usage_events (
                    job_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    succeeded INTEGER NOT NULL,
                    delivered_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            _ensure_column(connection, "jobs", "container", "TEXT")
            _ensure_column(
                connection,
                "jobs",
                "container_policy",
                "TEXT NOT NULL DEFAULT 'native_only'",
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
            "format_options": [
                {
                    "mode": option.mode.value,
                    "container": option.container.value if option.container else None,
                    "container_policy": option.container_policy.value,
                    "requires_transcode": option.requires_transcode,
                    "width": option.width,
                    "height": option.height,
                    "fps": option.fps,
                    "is_hdr": option.is_hdr,
                    "size_bytes": option.size_bytes,
                    "size_confidence": option.size_confidence.value,
                    "selection_reason": option.selection_reason,
                    "fallback_reason": option.fallback_reason,
                    "selected_format_ids": list(option.selected_format_ids),
                    "video_codec": option.video_codec,
                    "audio_codec": option.audio_codec,
                    "dynamic_range": option.dynamic_range,
                    "video_size_bytes": option.video_size_bytes,
                    "audio_size_bytes": option.audio_size_bytes,
                    "quality_score": option.quality_score,
                }
                for option in selection.media.format_options
            ],
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
                    job_id, kind, status, chat_id, user_id, url, mode, container,
                    container_policy, idempotency_key,
                    created_at, updated_at, status_message_id, source, error_category,
                    error_summary, cancel_requested, delivery_file_id,
                    delivery_file_unique_id, attempt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def upsert_delivery_item(self, item: DeliveryItemRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_items (
                    job_id, ordinal, provider, status, method,
                    recipient_message_id, file_id, file_unique_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, ordinal) DO UPDATE SET
                    provider = excluded.provider,
                    status = excluded.status,
                    method = excluded.method,
                    recipient_message_id = excluded.recipient_message_id,
                    file_id = excluded.file_id,
                    file_unique_id = excluded.file_unique_id,
                    updated_at = excluded.updated_at
                """,
                (
                    item.job_id,
                    item.ordinal,
                    item.provider.value,
                    item.status.value,
                    item.method.value,
                    item.recipient_message_id,
                    item.file_id,
                    item.file_unique_id,
                    _now_text(),
                ),
            )

    def delivery_items(self, job_id: JobId) -> tuple[DeliveryItemRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_items WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        return tuple(
            DeliveryItemRecord(
                job_id=JobId(str(row["job_id"])),
                ordinal=int(row["ordinal"]),
                provider=DeliveryProvider(str(row["provider"])),
                status=DeliveryItemStatus(str(row["status"])),
                method=DeliveryMethod(str(row["method"])),
                recipient_message_id=(
                    int(row["recipient_message_id"]) if row["recipient_message_id"] else None
                ),
                file_id=str(row["file_id"]) if row["file_id"] else None,
                file_unique_id=(str(row["file_unique_id"]) if row["file_unique_id"] else None),
            )
            for row in rows
        )

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
        columns = ", ".join(f"{name} = ?" for name in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET {columns}
                WHERE job_id = ?
                  AND (? = ? OR (cancel_requested = 0 AND status != ?))
                """,
                (
                    *values.values(),
                    job_id,
                    status.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise JobNotFoundError("Job does not exist")
        if bool(row["cancel_requested"]) or row["status"] == JobStatus.CANCELLED.value:
            return
        raise PersistenceError("Durable job transition was not applied")

    def complete_download(
        self,
        job_id: JobId,
        *,
        user_id: int,
        day: date,
        source: str,
        delivery_file_id: str | None,
        delivery_file_unique_id: str | None,
        attempt: int,
        delivered_bytes: int,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    source = ?,
                    delivery_file_id = ?,
                    delivery_file_unique_id = ?,
                    attempt = ?,
                    error_category = NULL,
                    error_summary = NULL,
                    updated_at = ?
                WHERE job_id = ? AND cancel_requested = 0 AND status != ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    source,
                    delivery_file_id,
                    delivery_file_unique_id,
                    attempt,
                    _now_text(),
                    job_id,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cursor.rowcount != 1:
                cancelled = connection.execute(
                    "SELECT cancel_requested, status FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                connection.execute("ROLLBACK")
                if cancelled is not None and (
                    bool(cancelled["cancel_requested"])
                    or cancelled["status"] == JobStatus.CANCELLED.value
                ):
                    raise JobCancelledError("Job was cancelled before durable completion")
                raise JobNotFoundError("Job does not exist")
            recorded = _record_usage_event(
                connection,
                job_id=job_id,
                user_id=user_id,
                day=day,
                succeeded=True,
                delivered_bytes=delivered_bytes,
            )
            connection.execute("COMMIT")
            return recorded

    def request_cancel(self, job_id: JobId, owner_user_id: int) -> bool:
        return self.cancel_job(job_id, owner_user_id).accepted

    def cancel_job(self, job_id: JobId, owner_user_id: int) -> JobCancellationResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = ? AND user_id = ?",
                (job_id, owner_user_id),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return JobCancellationResult(False, None, None)
            previous_status = JobStatus(str(row["status"]))
            if previous_status is JobStatus.CANCELLED and bool(row["cancel_requested"]):
                connection.execute("COMMIT")
                return JobCancellationResult(
                    True,
                    previous_status,
                    JobStatus.CANCELLED,
                    already_cancelled=True,
                )
            if previous_status.value not in _CANCELLABLE_STATUSES:
                connection.execute("COMMIT")
                return JobCancellationResult(False, previous_status, previous_status)
            connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    cancel_requested = 1,
                    error_category = ?,
                    error_summary = ?,
                    updated_at = ?
                WHERE job_id = ? AND user_id = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    ErrorCategory.CANCELLED.value,
                    "cancelled_by_user",
                    _now_text(),
                    job_id,
                    owner_user_id,
                ),
            )
            connection.execute("COMMIT")
        return JobCancellationResult(True, previous_status, JobStatus.CANCELLED)

    def finalize_cancelled(self, job_id: JobId, *, source: str) -> bool:
        placeholders = ",".join("?" for _ in _CANCELLABLE_STATUSES)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET
                    status = ?,
                    cancel_requested = 1,
                    error_category = ?,
                    error_summary = ?,
                    updated_at = ?
                WHERE job_id = ? AND status IN ({placeholders})
                """,
                (
                    JobStatus.CANCELLED.value,
                    ErrorCategory.CANCELLED.value,
                    f"cancelled_by_{source}",
                    _now_text(),
                    job_id,
                    *_CANCELLABLE_STATUSES,
                ),
            )
        return cursor.rowcount == 1

    def is_cancel_requested(self, job_id: JobId) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row is not None and bool(row["cancel_requested"])

    def reconcile_abandoned(self, older_than: datetime) -> tuple[JobRecoveryRecord, ...]:
        cutoff = _dump_datetime(older_than)
        decisions: list[tuple[JobId, JobStatus, RecoveryDecision]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cancelled_rows = connection.execute(
                """
                SELECT job_id, status FROM jobs
                WHERE cancel_requested = 1 AND status IN (?, ?, ?, ?)
                ORDER BY updated_at
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.RETRYING.value,
                    JobStatus.DELIVERING.value,
                ),
            ).fetchall()
            for row in cancelled_rows:
                previous = JobStatus(str(row["status"]))
                connection.execute(
                    """
                    UPDATE jobs SET
                        status = ?,
                        error_category = ?,
                        error_summary = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        JobStatus.CANCELLED.value,
                        ErrorCategory.CANCELLED.value,
                        "cancelled_during_recovery",
                        _now_text(),
                        row["job_id"],
                    ),
                )
                decisions.append(
                    (
                        JobId(str(row["job_id"])),
                        previous,
                        RecoveryDecision.SKIP_CANCELLED,
                    )
                )
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE cancel_requested = 0 AND status IN (?, ?) AND updated_at < ?
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
                decisions.append(
                    (
                        JobId(str(row["job_id"])),
                        JobStatus(str(row["status"])),
                        (
                            RecoveryDecision.QUARANTINE_DELIVERY
                            if next_status is JobStatus.DELIVERY_UNCERTAIN
                            else RecoveryDecision.REQUEUE_ABANDONED
                        ),
                    )
                )
            connection.execute("COMMIT")
        recovered: list[JobRecoveryRecord] = []
        for job_id, previous_status, decision in decisions:
            current = self.get_job(job_id)
            if current is not None:
                recovered.append(
                    JobRecoveryRecord(
                        job=current,
                        previous_status=previous_status,
                        decision=decision,
                    )
                )
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

    def upsert_user(self, profile: UserProfile, *, started: bool = False) -> None:
        now = _now_text()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    user_id, private_chat_id, username, first_name, last_name,
                    language_code, is_premium, first_started_at, last_started_at,
                    last_activity_at, start_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id),
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    language_code = excluded.language_code,
                    is_premium = excluded.is_premium,
                    first_started_at = COALESCE(users.first_started_at, excluded.first_started_at),
                    last_started_at = CASE
                        WHEN excluded.start_count = 1 THEN excluded.last_started_at
                        ELSE users.last_started_at
                    END,
                    last_activity_at = excluded.last_activity_at,
                    start_count = users.start_count + excluded.start_count
                """,
                (
                    profile.user_id,
                    profile.private_chat_id,
                    profile.username,
                    profile.first_name,
                    profile.last_name,
                    profile.language_code,
                    int(profile.is_premium) if profile.is_premium is not None else None,
                    now if started else None,
                    now if started else None,
                    now,
                    int(started),
                ),
            )

    def record_request(self, user_id: int, day: date) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_stub_user(connection, user_id)
            connection.execute(
                "UPDATE users SET request_count = request_count + 1, last_activity_at = ? "
                "WHERE user_id = ?",
                (_now_text(), user_id),
            )
            connection.execute(
                """
                INSERT INTO user_usage_daily (user_id, usage_date, request_count)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, usage_date) DO UPDATE SET
                    request_count = user_usage_daily.request_count + 1
                """,
                (user_id, day.isoformat()),
            )
            connection.execute("COMMIT")

    def record_download_outcome(
        self,
        *,
        job_id: JobId,
        user_id: int,
        day: date,
        succeeded: bool,
        delivered_bytes: int = 0,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recorded = _record_usage_event(
                connection,
                job_id=job_id,
                user_id=user_id,
                day=day,
                succeeded=succeeded,
                delivered_bytes=delivered_bytes,
            )
            connection.execute("COMMIT")
            return recorded

    def _update(self, job_id: JobId, **values: Any) -> None:
        columns = ", ".join(f"{name} = ?" for name in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {columns} WHERE job_id = ?", (*values.values(), job_id)
            )
        if cursor.rowcount != 1:
            raise JobNotFoundError("Job does not exist")


def _record_usage_event(
    connection: sqlite3.Connection,
    *,
    job_id: JobId,
    user_id: int,
    day: date,
    succeeded: bool,
    delivered_bytes: int,
) -> bool:
    safe_bytes = max(0, delivered_bytes) if succeeded else 0
    _ensure_stub_user(connection, user_id)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO download_usage_events (
            job_id, user_id, usage_date, succeeded, delivered_bytes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            user_id,
            day.isoformat(),
            int(succeeded),
            safe_bytes,
            _now_text(),
        ),
    )
    if cursor.rowcount != 1:
        return False
    success_delta = int(succeeded)
    failure_delta = int(not succeeded)
    connection.execute(
        """
        UPDATE users SET
            successful_download_count = successful_download_count + ?,
            failed_download_count = failed_download_count + ?,
            delivered_bytes = delivered_bytes + ?,
            last_activity_at = ?
        WHERE user_id = ?
        """,
        (success_delta, failure_delta, safe_bytes, _now_text(), user_id),
    )
    connection.execute(
        """
        INSERT INTO user_usage_daily (
            user_id, usage_date, successful_download_count,
            failed_download_count, delivered_bytes
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, usage_date) DO UPDATE SET
            successful_download_count =
                user_usage_daily.successful_download_count
                + excluded.successful_download_count,
            failed_download_count =
                user_usage_daily.failed_download_count
                + excluded.failed_download_count,
            delivered_bytes = user_usage_daily.delivered_bytes + excluded.delivered_bytes
        """,
        (user_id, day.isoformat(), success_delta, failure_delta, safe_bytes),
    )
    return True


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
        format_options=tuple(
            MediaFormatOption(
                mode=DownloadMode(str(item["mode"])),
                container=(
                    OutputContainer(str(item["container"])) if item.get("container") else None
                ),
                container_policy=ContainerPolicy(
                    str(item.get("container_policy", ContainerPolicy.NATIVE_ONLY.value))
                ),
                requires_transcode=bool(item.get("requires_transcode", False)),
                width=int(item["width"]) if item.get("width") is not None else None,
                height=int(item["height"]) if item.get("height") is not None else None,
                fps=float(item["fps"]) if item.get("fps") is not None else None,
                is_hdr=bool(item.get("is_hdr", False)),
                size_bytes=(
                    int(item["size_bytes"]) if item.get("size_bytes") is not None else None
                ),
                size_confidence=SizeConfidence(
                    str(item.get("size_confidence", SizeConfidence.UNKNOWN.value))
                ),
                selection_reason=(
                    str(item["selection_reason"])
                    if item.get("selection_reason") is not None
                    else None
                ),
                fallback_reason=(
                    str(item["fallback_reason"])
                    if item.get("fallback_reason") is not None
                    else None
                ),
                selected_format_ids=tuple(
                    str(value) for value in item.get("selected_format_ids", [])
                ),
                video_codec=(
                    str(item["video_codec"]) if item.get("video_codec") is not None else None
                ),
                audio_codec=(
                    str(item["audio_codec"]) if item.get("audio_codec") is not None else None
                ),
                dynamic_range=(
                    str(item["dynamic_range"]) if item.get("dynamic_range") is not None else None
                ),
                video_size_bytes=(
                    int(item["video_size_bytes"])
                    if item.get("video_size_bytes") is not None
                    else None
                ),
                audio_size_bytes=(
                    int(item["audio_size_bytes"])
                    if item.get("audio_size_bytes") is not None
                    else None
                ),
                quality_score=(
                    float(item["quality_score"]) if item.get("quality_score") is not None else None
                ),
            )
            for item in raw.get("format_options", [])
            if isinstance(item, dict) and item.get("mode") is not None
        ),
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
        container=(OutputContainer(str(row["container"])) if row["container"] else None),
        container_policy=ContainerPolicy(
            str(row["container_policy"])
            if row["container_policy"]
            else ContainerPolicy.NATIVE_ONLY.value
        ),
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
        record.container.value if record.container else None,
        record.container_policy.value,
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


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    existing = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _ensure_stub_user(connection: sqlite3.Connection, user_id: int) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO users (user_id, first_name, last_activity_at)
        VALUES (?, '', ?)
        """,
        (user_id, _now_text()),
    )
