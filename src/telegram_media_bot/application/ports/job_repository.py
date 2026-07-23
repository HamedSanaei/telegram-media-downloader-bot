from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.models import (
    ErrorCategory,
    JobCounts,
    JobId,
    JobRecord,
    JobStatus,
    SelectionRecord,
    SelectionToken,
)


class JobRepository(Protocol):
    def initialize(self) -> None: ...

    def healthy(self) -> bool: ...

    def save_selection(self, selection: SelectionRecord) -> None: ...

    def get_selection(self, token: SelectionToken, owner_user_id: int) -> SelectionRecord: ...

    def create_job(self, record: JobRecord) -> JobRecord: ...

    def get_job(self, job_id: JobId) -> JobRecord | None: ...

    def find_active_job(self, idempotency_key: str) -> JobRecord | None: ...

    def set_status_message(self, job_id: JobId, message_id: int) -> None: ...

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
    ) -> None: ...

    def request_cancel(self, job_id: JobId, owner_user_id: int) -> bool: ...

    def is_cancel_requested(self, job_id: JobId) -> bool: ...

    def reconcile_abandoned(self, older_than: datetime) -> tuple[JobRecord, ...]: ...

    def purge_expired(self, now: datetime, job_retention_days: int) -> int: ...

    def failed_jobs(self, limit: int = 10) -> tuple[JobRecord, ...]: ...

    def counts(self) -> JobCounts: ...

    def block_user(self, user_id: int, blocked_by: int) -> None: ...

    def unblock_user(self, user_id: int) -> None: ...

    def is_user_blocked(self, user_id: int) -> bool: ...
