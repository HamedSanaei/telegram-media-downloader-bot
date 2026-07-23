from __future__ import annotations

from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    GeoRestrictedError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
)


def map_ytdlp_error(exc: Exception) -> Exception:
    message = str(exc).casefold()
    if "requested format is not available" in message:
        return MediaUnavailableError("Requested format is unavailable")
    if "login" in message or "cookies" in message or "authentication" in message:
        return AuthenticationRequiredError("Authentication is required")
    if "geo" in message and ("restrict" in message or "country" in message):
        return GeoRestrictedError("Media is geographically restricted")
    if "too many requests" in message or "http error 429" in message:
        return RateLimitedError("Remote source rate limited the request")
    if "larger than max-filesize" in message or "file is larger" in message:
        return MediaTooLargeError("Media exceeds configured size limit")
    if "playlist" in message and "not" in message:
        return PlaylistNotAllowedError("Playlist download is not allowed")
    if "postprocess" in message or "ffmpeg" in message:
        return PostProcessingError("Media post-processing failed")
    if "unavailable" in message or "private" in message or "removed" in message:
        return MediaUnavailableError("Media is unavailable")
    return DownloadFailedError("Media download failed")
