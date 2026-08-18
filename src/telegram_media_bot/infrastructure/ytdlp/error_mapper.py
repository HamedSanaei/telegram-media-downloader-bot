from __future__ import annotations

from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    GeoRestrictedError,
    InvalidUrlError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
)


def map_ytdlp_error(exc: Exception) -> Exception:
    message = str(exc).casefold()
    http_status = _extract_http_status(exc)
    if "unsupported url" in message or "is not a valid url" in message:
        return InvalidUrlError("URL is not supported")
    if "requested format is not available" in message:
        return MediaUnavailableError("Requested format is unavailable")
    if "login" in message or "cookies" in message or "authentication" in message:
        return AuthenticationRequiredError("Authentication is required", http_status=http_status)
    if "geo" in message and ("restrict" in message or "country" in message):
        return GeoRestrictedError("Media is geographically restricted", http_status=http_status)
    if "too many requests" in message or "http error 429" in message:
        return RateLimitedError(
            "Remote source rate limited the request", http_status=http_status or 429
        )
    if "larger than max-filesize" in message or "file is larger" in message:
        return MediaTooLargeError("Media exceeds configured size limit")
    if "playlist" in message and "not" in message:
        return PlaylistNotAllowedError("Playlist download is not allowed")
    if "postprocess" in message or "ffmpeg" in message:
        return PostProcessingError("Media post-processing failed")
    if "unavailable" in message or "private" in message or "removed" in message:
        return MediaUnavailableError("Media is unavailable", http_status=http_status)
    return DownloadFailedError("Media download failed", http_status=http_status)


def _extract_http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction from yt-dlp/urllib wrapped errors."""
    current: object = exc
    for _depth in range(4):
        code = getattr(current, "code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
        status = getattr(current, "status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return status
        current = getattr(current, "exc_info", None)
        if not isinstance(current, BaseException):
            break
    return None
