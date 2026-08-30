from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.models import (
    DeliveryItemRecord,
    ErrorCategory,
    HighlightTrayRecord,
    JobCancellationResult,
    JobCounts,
    JobId,
    JobRecord,
    JobRecoveryRecord,
    JobStatus,
    SelectionRecord,
    SelectionToken,
)


class JobRepository(Protocol):
    def initialize(self) -> None: ...

    def healthy(self) -> bool: ...

    def save_selection(self, selection: SelectionRecord) -> None: ...

    def get_selection(self, token: SelectionToken, owner_user_id: int) -> SelectionRecord: ...

    def save_highlight_tray(self, tray: HighlightTrayRecord) -> None: ...

    def get_highlight_tray(
        self, token: SelectionToken, owner_user_id: int
    ) -> HighlightTrayRecord: ...

    def create_job(self, record: JobRecord) -> JobRecord: ...

    def get_job(self, job_id: JobId) -> JobRecord | None: ...

    def find_active_job(self, idempotency_key: str) -> JobRecord | None: ...

    def set_status_message(self, job_id: JobId, message_id: int) -> None: ...

    def upsert_delivery_item(self, item: DeliveryItemRecord) -> None: ...

    def delivery_items(self, job_id: JobId) -> tuple[DeliveryItemRecord, ...]: ...

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
    ) -> bool: ...

    def request_cancel(self, job_id: JobId, owner_user_id: int) -> bool: ...

    def cancel_job(self, job_id: JobId, owner_user_id: int) -> JobCancellationResult: ...

    def finalize_cancelled(self, job_id: JobId, *, source: str) -> bool: ...

    def is_cancel_requested(self, job_id: JobId) -> bool: ...

    def reconcile_abandoned(self, older_than: datetime) -> tuple[JobRecoveryRecord, ...]: ...

    def record_recoverable_failure(
        self,
        job_id: JobId,
        category: ErrorCategory | None,
        app_version: str,
    ) -> None: ...

    def cookie_recovery_candidates(
        self,
        cookie_service: CookieService,
        *,
        now: datetime,
        max_age_days: int,
        max_attempts: int,
        limit: int = 25,
        max_per_user: int | None = None,
    ) -> tuple[JobRecord, ...]: ...

    def app_fix_recovery_candidates(
        self,
        current_version: str,
        *,
        now: datetime,
        max_age_days: int,
        max_attempts: int,
        limit: int = 25,
        max_per_user: int | None = None,
    ) -> tuple[JobRecord, ...]: ...

    def recovery_requeues(self, limit: int = 50) -> tuple[JobRecord, ...]: ...

    def mark_recovery_requeued(
        self, job_id: JobId, *, version: str, now: datetime
    ) -> JobRecord | None: ...

    def mark_recovery_notification_sent(self, job_id: JobId) -> None: ...

    def pending_recoverable_count(self) -> int: ...

    def mark_cookie_remediation_available(
        self, cookie_service: CookieService, now: datetime
    ) -> None:
        """Durably remember a freshly validated cookie for one provider."""

    def clear_cookie_remediation_available(self, cookie_service: CookieService) -> None: ...

    def active_cookie_remediation_providers(self) -> tuple[CookieService, ...]: ...

    def purge_expired(self, now: datetime, job_retention_days: int) -> int: ...

    def failed_jobs(self, limit: int = 10) -> tuple[JobRecord, ...]: ...

    def counts(self) -> JobCounts: ...

    def block_user(self, user_id: int, blocked_by: int) -> None: ...

    def unblock_user(self, user_id: int) -> None: ...

    def is_user_blocked(self, user_id: int) -> bool: ...
