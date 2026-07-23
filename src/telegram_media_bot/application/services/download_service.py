from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from telegram_media_bot.application.ports.download_engine import DownloadEngine
from telegram_media_bot.domain.errors import (
    InvalidUrlError,
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
    def __init__(self, engine: DownloadEngine, enabled_sources: frozenset[str]) -> None:
        self._engine = engine
        self._enabled_sources = enabled_sources

    def inspect(self, url: str) -> MediaInfo:
        normalized_url = self.validate_url(url)
        info = self._engine.inspect(normalized_url)
        self._validate_source(info.source)
        return info

    def download(
        self,
        *,
        job_id: JobId,
        url: str,
        mode: DownloadMode,
        output_directory: Path,
    ) -> DownloadResult:
        normalized_url = self.validate_url(url)
        info = self._engine.inspect(normalized_url)
        self._validate_source(info.source)
        if info.kind is MediaKind.PLAYLIST:
            raise PlaylistNotAllowedError("Playlist jobs require the dedicated playlist flow")
        request = DownloadRequest(
            job_id=job_id,
            url=normalized_url,
            mode=mode,
            output_directory=output_directory,
        )
        result = self._engine.download(request)
        self._validate_source(result.source)
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
