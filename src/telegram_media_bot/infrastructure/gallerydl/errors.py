from __future__ import annotations

from telegram_media_bot.domain.errors import (
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlRateLimitedError,
    GalleryDlUnavailableError,
    MediaUnavailableError,
)


def map_process_failure(return_code: int, stderr: bytes) -> Exception:
    text = stderr.decode("utf-8", errors="replace").casefold()[:8192]
    if "no module named gallery_dl" in text or "not recognized" in text:
        return GalleryDlUnavailableError("gallery-dl is unavailable")
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return GalleryDlRateLimitedError("gallery provider rate limited the request")
    if any(
        marker in text for marker in ("expired cookie", "invalid cookie", "cookies have expired")
    ):
        return GalleryDlCookiesExpiredError("configured gallery cookies were rejected")
    if any(marker in text for marker in ("login required", "cookies required", "authentication")):
        return GalleryDlAuthenticationRequiredError("gallery provider authentication is required")
    if any(marker in text for marker in ("not found", "private", "unavailable", "deleted")):
        return MediaUnavailableError("gallery content is unavailable")
    return GalleryDlExtractionError(f"gallery-dl failed with stable exit class {return_code}")
