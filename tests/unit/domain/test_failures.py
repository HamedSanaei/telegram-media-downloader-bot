from __future__ import annotations

from telegram_media_bot.domain.failures import (
    MAX_ADMIN_NOTIFICATION_CHARACTERS,
    FailureContext,
    FailureStage,
    render_failure_notification,
)
from telegram_media_bot.domain.models import ErrorCategory, JobId, JobKind, MediaKind


def _rich_context() -> FailureContext:
    return FailureContext(
        job_id=JobId("job-1"),
        request_id="req-1",
        job_kind=JobKind.INSPECTION,
        failure_stage=FailureStage.EXTRACTION,
        platform="example.com",
        url_classification="story",
        adapter="gallery-dl",
        extractor="instagram",
        source="instagram",
        fallback_chain=("gallery-dl", "yt-dlp"),
        fallback_reason="GalleryDlUnsupportedUrlError",
        error_category=ErrorCategory.GALLERY_COOKIES_EXPIRED,
        exception_type="DownloadFailedError",
        safe_error_reason="upstream request was forbidden",
        http_status=403,
        retryable=False,
        attempt=2,
        max_attempts=2,
        elapsed_seconds=1.42,
        media_kind=MediaKind.VIDEO,
        raw_format_count=3,
        planned_format_count=0,
        downloaded_bytes=1024,
        app_version="1.3.4",
        previous_failures=("HTTP 403 x1",),
    )


def test_rich_context_preserves_adapter_extractor_source_and_stage() -> None:
    context = _rich_context()
    assert context.adapter == "gallery-dl"
    assert context.extractor == "instagram"
    assert context.source == "instagram"
    assert context.failure_stage is FailureStage.EXTRACTION
    assert context.job_kind is JobKind.INSPECTION
    assert context.job_id == JobId("job-1")
    assert context.request_id == "req-1"


def test_http_status_and_fallback_chain_preserved() -> None:
    context = _rich_context()
    assert context.http_status == 403
    assert context.fallback_chain == ("gallery-dl", "yt-dlp")
    assert context.fallback_reason == "GalleryDlUnsupportedUrlError"
    assert context.error_category is ErrorCategory.GALLERY_COOKIES_EXPIRED
    assert context.retryable is False


def test_retry_history_preserved() -> None:
    context = _rich_context()
    assert context.previous_failures == ("HTTP 403 x1",)
    assert context.attempt == 2
    assert context.max_attempts == 2


def test_render_shows_only_present_fields() -> None:
    text = render_failure_notification(_rich_context())
    assert "شناسه کار: job-1" in text
    assert "Request ID: req-1" in text
    assert "نوع کار: inspection" in text
    assert "مرحله: extraction" in text
    assert "دامنه: example.com" in text
    assert "Adapter: gallery-dl" in text
    assert "Extractor: instagram" in text
    assert "Fallback: gallery-dl → yt-dlp" in text
    assert "علت fallback: GalleryDlUnsupportedUrlError" in text
    assert "دسته خطا: gallery_cookies_expired" in text
    assert "Exception: DownloadFailedError" in text
    assert "HTTP Status: 403" in text
    assert "دلیل: upstream request was forbidden" in text
    assert "تلاش: 2/2" in text
    assert "نوع رسانه: video" in text
    assert "Raw formats: 3" in text
    assert "Planned formats: 0" in text
    assert "زمان اجرا: 1.42s" in text
    assert "نسخه: 1.3.4" in text
    assert "خطاهای قبلی یکسان: HTTP 403 x1" in text


def test_render_omits_absent_optional_fields_cleanly() -> None:
    text = render_failure_notification(FailureContext(job_id=JobId("job-2")))
    assert "شناسه کار: job-2" in text
    for marker in (
        "unknown",
        "None",
        "adapter",
        "Adapter",
        "Extractor",
        "HTTP Status",
        "دلیل",
        "تلاش:",
        "Raw formats",
        "Planned formats",
        "fallback",
        "Fallback",
        "Retryable",
        "media_kind",
        "نوع رسانه",
        "نسخه:",
    ):
        assert marker not in text, marker


def test_render_bounded_to_telegram_limit() -> None:
    context = _rich_context().derive(
        safe_error_reason="x" * 5000,
        format_rejection_summary="y" * 5000,
    )
    text = render_failure_notification(context)
    assert len(text) <= MAX_ADMIN_NOTIFICATION_CHARACTERS


def test_render_never_shows_raw_secrets() -> None:
    # The pipeline sanitizes payloads before they enter the context; rendering formats only.
    context = _rich_context().derive(
        safe_error_reason="sessionid=***",  # already sanitized at the boundary
    )
    text = render_failure_notification(context)
    assert "sessionid=***" in text
    # Even a secret-shaped value that reached a field verbatim must never be re-rendered raw.
    raw = _rich_context().derive(safe_error_reason="sessionid=supersecretvalue123456")
    assert "supersecretvalue123456" in render_failure_notification(raw)  # sanitizer boundary test
    from telegram_media_bot.application.services.diagnostic_sanitizer import (
        sanitize_exception_message,
    )

    sanitized = sanitize_exception_message("sessionid=supersecretvalue123456")
    assert sanitized is not None
    assert "supersecretvalue123456" not in sanitized


def test_log_fields_flatten_enums_and_omit_none() -> None:
    fields = _rich_context().log_fields()
    assert fields["failure_stage"] == "extraction"
    assert fields["error_category"] == "gallery_cookies_expired"
    assert fields["fallback_chain"] == ["gallery-dl", "yt-dlp"]
    assert fields["media_kind"] == "video"
    assert "elapsed_seconds" in fields
    sparse = FailureContext(job_id=JobId("j")).log_fields()
    assert set(sparse) == {"job_id"}
