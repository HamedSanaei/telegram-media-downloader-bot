from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from telegram_media_bot.domain.errors import (
    GalleryDlAccessDeniedError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlRateLimitedError,
    GalleryDlTransientError,
    GalleryDlUnavailableError,
    MediaBotError,
    MediaUnavailableError,
)
from telegram_media_bot.domain.failures import FailureStage

_HTTP_STATUS_PATTERN = re.compile(
    r"\b(?:http(?:error| error)?|status(?:[_ ]code)?)[\"']?\s*[:=]?\s*[\"']?([1-5]\d{2})\b"
    r"|[\"']([1-5]\d{2}) [a-z]",
    re.IGNORECASE,
)
_RETRY_AFTER_PATTERN = re.compile(
    r"\bretry-after[\"']?\s*[:=]\s*[\"']?([^\r\n\"']+)", re.IGNORECASE
)


def map_process_failure(return_code: int, stderr: bytes) -> MediaBotError:
    """Translate bounded vendor evidence, retaining only fixed categories and numbers.

    A denied request is not proof that a personal session expired. Likewise neither empty
    output nor an unknown extractor exception establishes a transient failure.
    """
    text = stderr[: 256 * 1024].decode("utf-8", errors="replace").casefold()
    http_status = _extract_http_status(text)
    error: MediaBotError
    category: str
    if "no module named gallery_dl" in text or "not recognized" in text:
        error = GalleryDlUnavailableError("gallery-dl is unavailable")
        category = "adapter_unavailable"
    elif http_status == 429 or _contains(
        text, "rate limit", "too many requests", "please wait a few minutes"
    ):
        error = GalleryDlRateLimitedError("gallery provider rate limited the request")
        category = "rate_limited"
    elif _contains(
        text,
        "expired cookie",
        "invalid cookie",
        "cookies have expired",
        "session expired",
        "invalid session",
        "session has expired",
        "session is invalid",
    ):
        error = GalleryDlCookiesExpiredError("configured gallery cookies were rejected")
        category = "session_rejected"
    elif _contains(text, "checkpoint_required", "challenge_required", "redirect to challenge page"):
        error = GalleryDlAuthenticationRequiredError(
            "gallery provider requires a session challenge"
        )
        category = "challenge_required"
    elif http_status == 401 or _contains(
        text,
        "login required",
        "login_required",
        "cookies required",
        "authrequired:",
        "authentication required",
        "authenticated cookies",
        "please log in",
        "redirect to login page",
    ):
        error = GalleryDlAuthenticationRequiredError("gallery provider authentication is required")
        category = "authentication_required"
    elif http_status == 403:
        error = GalleryDlAccessDeniedError("gallery provider denied access to this content")
        category = "access_denied"
    elif http_status is not None and 500 <= http_status <= 599:
        error = GalleryDlTransientError("gallery provider encountered a server failure")
        category = "upstream_server"
    elif _contains(
        text,
        "connectionerror",
        "connection error",
        "connection reset",
        "connection aborted",
        "connection refused",
        "connecttimeout",
        "readtimeout",
        "timed out",
        "timeout error",
        "temporary failure in name resolution",
        "name resolution",
        "failed to resolve",
        "remote end closed connection",
        "chunkedencodingerror",
        "name or service not known",
    ):
        error = GalleryDlTransientError("gallery provider request encountered a network failure")
        category = "network"
    elif _contains(
        text,
        "keyerror",
        "typeerror",
        "attributeerror",
        "indexerror",
        "jsondecodeerror",
        "failed to parse json",
        "unsupported with graphql api",
        "noextractorerror",
        "unexpected error occurred",
        "unknown doc_id",
        "invalid doc_id",
        "persistedquerynotfound",
    ):
        error = GalleryDlOutputChangedError(
            "gallery extractor is incompatible with the provider response"
        )
        category = "extractor_incompatible"
    elif "graphql" in text and _contains(text, "query not found", "document not found", "http 404"):
        error = GalleryDlOutputChangedError(
            "gallery extractor query is incompatible with the provider"
        )
        category = "extractor_incompatible"
    elif "graphql" in text and _contains(text, "error", "failed", '"errors"'):
        error = GalleryDlExtractionError("gallery provider returned a GraphQL error")
        category = "graphql_error"
    elif http_status in {404, 410} or _contains(
        text,
        "notfounderror:",
        "not found",
        "is private",
        "private video",
        "posts are private",
        "content unavailable",
        "media unavailable",
        "deleted",
        "requested resource does not exist",
    ):
        error = MediaUnavailableError("gallery content is unavailable")
        category = "media_unavailable"
    else:
        error = GalleryDlExtractionError(
            "gallery extraction failed without a definitive provider cause"
        )
        category = "extraction_unknown"
    error.http_status = http_status
    error.adapter = "gallery-dl"
    error.exit_code = return_code
    error.diagnostic_category = category
    error.retry_after_seconds = _extract_retry_after(text) if error.retryable else None
    error.failure_stage = (
        FailureStage.AUTHENTICATION
        if isinstance(error, (GalleryDlAuthenticationRequiredError, GalleryDlCookiesExpiredError))
        else FailureStage.EXTRACTION
    )
    return error


def _contains(text: str, *markers: str) -> bool:
    return any(marker in text for marker in markers)


def _extract_http_status(text: str) -> int | None:
    match = _HTTP_STATUS_PATTERN.search(text)
    return int(match.group(1) or match.group(2)) if match is not None else None


def _extract_retry_after(text: str) -> float | None:
    match = _RETRY_AFTER_PATTERN.search(text)
    if match is None:
        return None
    value = match.group(1).strip()
    if value.isascii() and value.isdecimal():
        # Preserve an oversized cooldown as a terminal floor rather than retrying early.
        digits = value.lstrip("0") or "0"
        return float(digits) if len(digits) <= 9 else 1_000_000_000.0
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            return None
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    except ValueError, TypeError, OverflowError:
        return None
