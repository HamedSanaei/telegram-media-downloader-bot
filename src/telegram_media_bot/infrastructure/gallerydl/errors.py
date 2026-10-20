from __future__ import annotations

import json
import math
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
_DIAGNOSTIC_PREFIX = "tmb-gallery-diagnostic: "
_ERRORS: dict[str, tuple[type[MediaBotError], str]] = {
    "adapter_unavailable": (GalleryDlUnavailableError, "gallery-dl is unavailable"),
    "rate_limited": (GalleryDlRateLimitedError, "gallery provider rate limited the request"),
    "session_rejected": (GalleryDlCookiesExpiredError, "configured gallery cookies were rejected"),
    "challenge_required": (
        GalleryDlAuthenticationRequiredError,
        "gallery provider requires a session challenge",
    ),
    "authentication_required": (
        GalleryDlAuthenticationRequiredError,
        "gallery provider authentication is required",
    ),
    "access_denied": (GalleryDlAccessDeniedError, "gallery provider denied access to this content"),
    "upstream_server": (GalleryDlTransientError, "gallery provider encountered a server failure"),
    "network": (GalleryDlTransientError, "gallery provider request encountered a network failure"),
    "extractor_incompatible": (
        GalleryDlOutputChangedError,
        "gallery extractor is incompatible with the provider response",
    ),
    "graphql_error": (GalleryDlExtractionError, "gallery provider returned a GraphQL error"),
    "media_unavailable": (MediaUnavailableError, "gallery content is unavailable"),
    "extraction_unknown": (
        GalleryDlExtractionError,
        "gallery extraction failed without a definitive provider cause",
    ),
}


def map_process_failure(return_code: int, stderr: bytes) -> MediaBotError:
    """Translate bounded vendor evidence, retaining only fixed categories and numbers.

    A denied request is not proof that a personal session expired. Likewise neither empty
    output nor an unknown extractor exception establishes a transient failure.
    """
    text = stderr[: 256 * 1024].decode("utf-8", errors="replace").casefold()
    if diagnostic := _extract_diagnostic(text):
        category, http_status, retry_after = diagnostic
    else:
        http_status = _extract_http_status(text)
        category = _classify_text(text, http_status)
        retry_after = _extract_retry_after(text)
    error_type, reason = _ERRORS[category]
    error = error_type(reason)
    error.http_status = http_status
    error.adapter = "gallery-dl"
    error.exit_code = return_code
    error.diagnostic_category = category
    error.retry_after_seconds = retry_after if error.retryable else None
    error.failure_stage = (
        FailureStage.AUTHENTICATION
        if isinstance(error, (GalleryDlAuthenticationRequiredError, GalleryDlCookiesExpiredError))
        else FailureStage.EXTRACTION
    )
    return error


def format_process_diagnostic(error: MediaBotError) -> str:
    """Preserve only normalized evidence before gallery-dl DataJob swallows exceptions."""
    return _DIAGNOSTIC_PREFIX + json.dumps(
        {
            "category": error.diagnostic_category,
            "http_status": error.http_status,
            "retry_after_seconds": error.retry_after_seconds,
        },
        separators=(",", ":"),
    )


def _extract_diagnostic(text: str) -> tuple[str, int | None, float | None] | None:
    for line in text.splitlines():
        if not (payload := line.partition(_DIAGNOSTIC_PREFIX)[2]):
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        category = data.get("category")
        status = data.get("http_status")
        cooldown = data.get("retry_after_seconds")
        if not isinstance(category, str) or category not in _ERRORS:
            continue
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            continue
        if cooldown is not None and (
            type(cooldown) not in (int, float)
            or not 0 <= cooldown <= 1_000_000_000
            or not math.isfinite(cooldown)
        ):
            continue
        return category, status, cooldown
    return None


def _classify_text(text: str, http_status: int | None) -> str:
    if "no module named gallery_dl" in text or "not recognized" in text:
        return "adapter_unavailable"
    elif http_status == 429 or _contains(
        text, "rate limit", "too many requests", "please wait a few minutes"
    ):
        return "rate_limited"
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
        return "session_rejected"
    elif _contains(text, "checkpoint_required", "challenge_required", "redirect to challenge page"):
        return "challenge_required"
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
        return "authentication_required"
    elif http_status == 403:
        return "access_denied"
    elif http_status is not None and 500 <= http_status <= 599:
        return "upstream_server"
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
        return "network"
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
    ) or (
        "graphql" in text and _contains(text, "query not found", "document not found", "http 404")
    ):
        return "extractor_incompatible"
    elif "graphql" in text and _contains(text, "error", "failed", '"errors"'):
        return "graphql_error"
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
        return "media_unavailable"
    else:
        return "extraction_unknown"


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
