from __future__ import annotations

import errno
from collections.abc import Iterator

from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    GeoRestrictedError,
    InvalidUrlError,
    LocalRuntimeError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
)

#: Errno values describing local filesystem/workspace conditions with a specific,
#: operator-meaningful reason. Kept deliberately narrow so remote/network-shaped OSErrors
#: (timeouts, connection resets, DNS failures) keep their remote failure classification.
_SPECIFIC_LOCAL_ERRNO_REASONS: dict[int, str] = {
    errno.EACCES: "Permission denied accessing the local media workspace",
    errno.EPERM: "Local media workspace rejected the required operation",
    errno.EROFS: "Local temporary workspace is not writable: read-only filesystem",
    errno.ENOSPC: "Local storage is full",
}

#: Additional errno values treated as generic local filesystem/runtime failures instead of
#: remote media failures: missing or unreachable local paths, I/O errors, and descriptor
#: exhaustion. Everything else (including every network-shaped OSError) stays remote.
_GENERIC_LOCAL_ERRNOS = frozenset(
    {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.EISDIR,
        errno.ENAMETOOLONG,
        errno.EIO,
        errno.ENFILE,
        errno.EMFILE,
    }
)

_MAX_CAUSE_DEPTH = 4


def map_ytdlp_error(exc: Exception) -> Exception:
    local_error = _find_local_filesystem_error(exc)
    if local_error is not None:
        return _map_local_runtime_error(local_error)
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


def _find_local_filesystem_error(exc: BaseException) -> OSError | None:
    """Find a local filesystem OSError in the yt-dlp exception chain.

    yt-dlp wraps underlying failures in its own exception types (``DownloadError`` keeps an
    ``exc_info`` tuple; ``raise ... from`` chains are followed through ``__cause__``). Only
    OSErrors with local filesystem errnos qualify, so a local workspace failure can never be
    reported as a remote provider failure and vice versa.
    """
    for candidate in _chained_exceptions(exc):
        if isinstance(candidate, OSError) and _is_local_filesystem_errno(candidate):
            return candidate
    return None


def _chained_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and its wrapped causes through ``__cause__``/``exc_info`` links."""
    current: BaseException | object | None = exc
    for _depth in range(_MAX_CAUSE_DEPTH):
        if not isinstance(current, BaseException):
            return
        yield current
        chained: object = current.__cause__
        if chained is None:
            exc_info = getattr(current, "exc_info", None)
            if isinstance(exc_info, tuple) and exc_info:
                chained = exc_info[1]
        current = chained


def _is_local_filesystem_errno(os_error: OSError) -> bool:
    if isinstance(os_error, PermissionError):
        return True
    return os_error.errno in _SPECIFIC_LOCAL_ERRNO_REASONS or (
        os_error.errno is not None and os_error.errno in _GENERIC_LOCAL_ERRNOS
    )


def _map_local_runtime_error(os_error: OSError) -> LocalRuntimeError:
    errno_value = os_error.errno
    specific_reason = (
        _SPECIFIC_LOCAL_ERRNO_REASONS.get(errno_value) if errno_value is not None else None
    )
    if specific_reason is not None:
        reason = specific_reason
    elif isinstance(os_error, PermissionError):
        reason = "Permission denied accessing the local media workspace"
    else:
        reason = f"{type(os_error).__name__} while accessing the local media workspace"
    errno_suffix = f" [Errno {errno_value}]" if errno_value is not None else ""
    # The reason intentionally carries only the exception class and errno: absolute paths
    # never enter operator-visible diagnostics.
    return LocalRuntimeError(f"{reason}{errno_suffix}", os_errno=errno_value)


def _extract_http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction from yt-dlp/urllib wrapped errors."""
    for candidate in _chained_exceptions(exc):
        code = getattr(candidate, "code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
        status = getattr(candidate, "status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return status
    return None
