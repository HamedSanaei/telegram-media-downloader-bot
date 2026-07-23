from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from telegram_media_bot.application.ports.download_engine import (
    CancellationCheck,
    DownloadEngine,
    ProgressSink,
)
from telegram_media_bot.application.ports.url_validator import UrlValidator
from telegram_media_bot.domain.errors import (
    InvalidUrlError,
    MediaTooLargeError,
    PlaylistNotAllowedError,
    UnsupportedSourceError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    JobId,
    MediaInfo,
    MediaKind,
)


class DownloadService:
    def __init__(
        self,
        engine: DownloadEngine,
        enabled_sources: frozenset[str],
        *,
        url_validator: UrlValidator | None = None,
        allow_playlists: bool = False,
        playlist_max_items: int = 20,
        max_duration_seconds: int = 14400,
        max_file_size_bytes: int | None = None,
    ) -> None:
        self._engine = engine
        self._enabled_sources = enabled_sources
        self._url_validator = url_validator
        self._allow_playlists = allow_playlists
        self._playlist_max_items = playlist_max_items
        self._max_duration_seconds = max_duration_seconds
        self._max_file_size_bytes = max_file_size_bytes

    def inspect(self, url: str) -> MediaInfo:
        normalized_url = self.validate_url(url)
        if self._url_validator is not None:
            normalized_url = self._url_validator.validate(normalized_url)
        info = self._engine.inspect(normalized_url)
        self._validate_source(info.source)
        if self._url_validator is not None:
            self._url_validator.validate(info.webpage_url)
        self._validate_limits(info)
        return info

    def download(
        self,
        *,
        job_id: JobId,
        url: str,
        mode: DownloadMode,
        output_directory: Path,
        temp_directory: Path | None = None,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> DownloadResult:
        info = self.inspect(url)
        request = DownloadRequest(
            job_id=job_id,
            url=info.webpage_url,
            mode=mode,
            output_directory=output_directory,
            temp_directory=temp_directory,
        )
        result = self._engine.download(
            request,
            progress=progress,
            is_cancelled=is_cancelled,
        )
        self._validate_source(result.source)
        if (
            self._max_file_size_bytes is not None
            and result.file_size_bytes > self._max_file_size_bytes
        ):
            raise MediaTooLargeError("Final media exceeds configured size limit")
        return result

    @staticmethod
    def validate_url(url: str) -> str:
        candidate = url.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidUrlError("Only absolute HTTP(S) URLs are accepted")
        if parsed.username or parsed.password:
            raise InvalidUrlError("Credentials in URLs are not accepted")
        return candidate

    def _validate_source(self, source: str) -> None:
        if source.casefold() not in self._enabled_sources:
            raise UnsupportedSourceError(f"Source is disabled: {source}")

    def _validate_limits(self, info: MediaInfo) -> None:
        if info.kind is MediaKind.PLAYLIST:
            if not self._allow_playlists:
                raise PlaylistNotAllowedError("Playlist download is not allowed")
            if info.item_count is None or info.item_count > self._playlist_max_items:
                raise PlaylistNotAllowedError("Playlist exceeds the configured item limit")
        if info.duration_seconds is not None and info.duration_seconds > self._max_duration_seconds:
            raise MediaTooLargeError("Media duration exceeds the configured limit")
        if (
            self._max_file_size_bytes is not None
            and info.estimated_size_bytes is not None
            and info.estimated_size_bytes > self._max_file_size_bytes
        ):
            raise MediaTooLargeError("Estimated media size exceeds the configured limit")
