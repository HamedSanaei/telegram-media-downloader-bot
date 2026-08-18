from __future__ import annotations

import re

from telegram_media_bot.domain.errors import (
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlRateLimitedError,
    GalleryDlUnavailableError,
    MediaUnavailableError,
)

_HTTP_STATUS_PATTERN = re.compile(r"(?:HTTP(?: error)?|status code)\s+(\d{3})", re.IGNORECASE)


def map_process_failure(return_code: int, stderr: bytes) -> Exception:
    text = stderr.decode("utf-8", errors="replace").casefold()[:8192]
    http_status = _extract_http_status(text)
    if "no module named gallery_dl" in text or "not recognized" in text:
        return GalleryDlUnavailableError("gallery-dl is unavailable")
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return GalleryDlRateLimitedError(
            "gallery provider rate limited the request", http_status=http_status or 429
        )
    if any(
        marker in text for marker in ("expired cookie", "invalid cookie", "cookies have expired")
    ):
        return GalleryDlCookiesExpiredError(
            "configured gallery cookies were rejected", http_status=http_status
        )
    if any(marker in text for marker in ("login required", "cookies required", "authentication")):
        return GalleryDlAuthenticationRequiredError(
            "gallery provider authentication is required", http_status=http_status
        )
    if any(marker in text for marker in ("not found", "private", "unavailable", "deleted")):
        return MediaUnavailableError("gallery content is unavailable", http_status=http_status)
    return GalleryDlExtractionError(
        f"gallery-dl failed with stable exit class {return_code}", http_status=http_status
    )


def _extract_http_status(text: str) -> int | None:
    match = _HTTP_STATUS_PATTERN.search(text)
    if match is None:
        return None
    try:
        status = int(match.group(1))
    except ValueError:
        return None
    return status if 100 <= status <= 599 else None
