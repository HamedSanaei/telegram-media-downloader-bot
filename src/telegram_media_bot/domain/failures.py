"""Typed structured failure diagnostics that survive from the failing layer to the
administrator notification.

A :class:`FailureContext` is created as close as possible to the actual failure and is
threaded through domain/infrastructure -> application service -> worker -> retry -> terminal
failure -> admin notification. Every optional field is omitted when its value is not known;
the final administrator message renders only the fields that exist. All string payloads must
be sanitized before they enter the context (see ``application/services/diagnostic_sanitizer.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from telegram_media_bot.domain.models import ErrorCategory, JobId, JobKind, MediaKind

MAX_ADMIN_NOTIFICATION_CHARACTERS = 4096


class FailureStage(StrEnum):
    """Bounded failure-stage vocabulary used across every job kind."""

    ROUTING = "routing"
    CANONICALIZATION = "canonicalization"
    INSPECTION = "inspection"
    EXTRACTION = "extraction"
    FORMAT_PLANNING = "format_planning"
    DOWNLOAD = "download"
    POSTPROCESS = "postprocess"
    DELIVERY = "delivery"
    CLEANUP = "cleanup"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureContext:
    """Structured, already-sanitized context for one terminal job failure.

    Unknown optional fields stay ``None``; the renderer never prints "unknown" repeatedly.
    Never store raw secrets, raw URLs with query parameters, or unsanitized exception text
    in this object.
    """

    job_id: JobId | None = None
    request_id: str | None = None
    job_kind: JobKind | None = None
    failure_stage: FailureStage | None = None
    #: Platform/domain (for example "instagram.com").
    platform: str | None = None
    #: URL classification (for example "story", "highlight", "profile").
    url_classification: str | None = None
    adapter: str | None = None
    extractor: str | None = None
    source: str | None = None
    fallback_chain: tuple[str, ...] | None = None
    fallback_reason: str | None = None
    error_category: ErrorCategory | None = None
    exception_type: str | None = None
    #: Sanitized, human-safe failure reason (never raw exception text).
    safe_error_reason: str | None = None
    http_status: int | None = None
    retryable: bool | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    elapsed_seconds: float | None = None
    media_kind: MediaKind | None = None
    raw_format_count: int | None = None
    planned_format_count: int | None = None
    format_rejection_summary: str | None = None
    downloaded_bytes: int | None = None
    app_version: str | None = None
    #: Rendered previous-failure history, for example ("HTTP 403 x1",).
    previous_failures: tuple[str, ...] = ()

    def derive(self, **changes: Any) -> Self:
        """Return a new context with ``changes`` applied (immutable update)."""
        return type(self)(**{**self.as_dict(), **changes})

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "request_id": self.request_id,
            "job_kind": self.job_kind,
            "failure_stage": self.failure_stage,
            "platform": self.platform,
            "url_classification": self.url_classification,
            "adapter": self.adapter,
            "extractor": self.extractor,
            "source": self.source,
            "fallback_chain": self.fallback_chain,
            "fallback_reason": self.fallback_reason,
            "error_category": self.error_category,
            "exception_type": self.exception_type,
            "safe_error_reason": self.safe_error_reason,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "media_kind": self.media_kind,
            "raw_format_count": self.raw_format_count,
            "planned_format_count": self.planned_format_count,
            "format_rejection_summary": self.format_rejection_summary,
            "downloaded_bytes": self.downloaded_bytes,
            "app_version": self.app_version,
            "previous_failures": self.previous_failures,
        }

    def log_fields(self) -> dict[str, object]:
        """Structured-log representation with enum values flattened and absent fields omitted."""
        fields: dict[str, object] = {}
        for key, value in self.as_dict().items():
            if value is None or (isinstance(value, tuple) and not value):
                continue
            if isinstance(value, StrEnum):
                fields[key] = value.value
            elif isinstance(value, tuple):
                fields[key] = list(value)
            else:
                fields[key] = value
        return fields


def render_failure_notification(context: FailureContext) -> str:
    """Render the final administrator failure notification.

    Only fields that exist are shown; absent optional fields are omitted instead of printed
    as "unknown". The result is bounded below Telegram's message limit.
    """
    lines = ["🚨 خطای نهایی پردازش", ""]
    if context.job_id is not None:
        lines.append(f"شناسه کار: {context.job_id}")
    if context.request_id is not None:
        lines.append(f"Request ID: {context.request_id}")
    if context.job_id is not None or context.request_id is not None:
        lines.append("")
    if context.job_kind is not None:
        lines.append(f"نوع کار: {context.job_kind.value}")
    if context.failure_stage is not None:
        lines.append(f"مرحله: {context.failure_stage.value}")
    if context.platform is not None:
        lines.append(f"دامنه: {context.platform}")
    if context.url_classification is not None:
        lines.append(f"طبقه‌بندی URL: {context.url_classification}")
    if context.source is not None:
        lines.append(f"منبع: {context.source}")
    if any(
        value is not None
        for value in (
            context.job_kind,
            context.failure_stage,
            context.platform,
            context.url_classification,
            context.source,
        )
    ):
        lines.append("")
    if context.adapter is not None or context.extractor is not None:
        if context.adapter is not None:
            lines.append(f"Adapter: {context.adapter}")
        if context.extractor is not None:
            lines.append(f"Extractor: {context.extractor}")
        lines.append("")
    if context.fallback_chain:
        lines.append(f"Fallback: {' → '.join(context.fallback_chain)}")
        if context.fallback_reason is not None:
            lines.append(f"علت fallback: {context.fallback_reason}")
        lines.append("")
    if context.error_category is not None:
        lines.append(f"دسته خطا: {context.error_category.value}")
    if context.exception_type is not None:
        lines.append(f"Exception: {context.exception_type}")
    if context.http_status is not None:
        lines.append(f"HTTP Status: {context.http_status}")
    if context.safe_error_reason is not None:
        lines.append(f"دلیل: {context.safe_error_reason}")
    if context.retryable is not None:
        lines.append(f"Retryable: {'بله' if context.retryable else 'خیر'}")
    if context.attempt is not None:
        suffix = f"/{context.max_attempts}" if context.max_attempts is not None else ""
        lines.append(f"تلاش: {context.attempt}{suffix}")
    if context.previous_failures:
        lines.append("خطاهای قبلی یکسان: " + "، ".join(context.previous_failures))
    if context.media_kind is not None:
        lines.append(f"نوع رسانه: {context.media_kind.value}")
    if context.raw_format_count is not None:
        lines.append(f"Raw formats: {context.raw_format_count}")
    if context.planned_format_count is not None:
        lines.append(f"Planned formats: {context.planned_format_count}")
    if context.format_rejection_summary is not None:
        lines.append(f"رد فرمت: {context.format_rejection_summary}")
    if context.downloaded_bytes is not None:
        lines.append(f"دانلودشده: {_format_bytes(context.downloaded_bytes)}")
    if context.elapsed_seconds is not None:
        lines.append(f"زمان اجرا: {_format_elapsed(context.elapsed_seconds)}")
    if context.app_version is not None:
        lines.append(f"نسخه: {context.app_version}")
    text = "\n".join(lines)
    return text[:MAX_ADMIN_NOTIFICATION_CHARACTERS]


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:.1f}s"


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
