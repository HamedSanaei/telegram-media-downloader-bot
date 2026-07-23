from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DownloadFailedError, MediaTooLargeError
from telegram_media_bot.domain.models import DownloadRequest, DownloadResult, MediaInfo
from telegram_media_bot.infrastructure.ytdlp.error_mapper import map_ytdlp_error
from telegram_media_bot.infrastructure.ytdlp.mapper import detect_kind, map_media_info, normalize_source
from telegram_media_bot.infrastructure.ytdlp.options import YtDlpOptionsFactory, final_media_files


class YtDlpEngine:
    """The only application adapter that directly knows yt-dlp types and options."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._options = YtDlpOptionsFactory(settings)

    def inspect(self, url: str) -> MediaInfo:
        try:
            with YoutubeDL(self._options.inspect_options()) as ydl:
                raw = ydl.extract_info(url, download=False)
                info = self._sanitize(ydl, raw)
        except Exception as exc:
            raise map_ytdlp_error(exc) from exc
        return map_media_info(info, original_url=url)

    def download(self, request: DownloadRequest) -> DownloadResult:
        job_dir = self._safe_job_directory(request.output_directory)
        self._reset_job_directory(job_dir)
        try:
            with YoutubeDL(self._options.download_options(request)) as ydl:
                raw = ydl.extract_info(request.url, download=True)
                info = self._sanitize(ydl, raw)
            files = final_media_files(job_dir)
            if not files:
                raise DownloadFailedError("yt-dlp completed without a final output file")
            final_file = files[0]
            size = final_file.stat().st_size
            max_size = self._settings.media.max_file_size_mb * 1024 * 1024
            if size > max_size:
                raise MediaTooLargeError("Final media exceeds configured size limit")
            return DownloadResult(
                job_id=request.job_id,
                media_id=str(info.get("id") or final_file.stem),
                title=str(info.get("title") or "Untitled"),
                source=normalize_source(info),
                kind=detect_kind(info),
                file_path=final_file,
                file_size_bytes=size,
            )
        except Exception as exc:
            if isinstance(exc, (DownloadFailedError, MediaTooLargeError)):
                raise
            raise map_ytdlp_error(exc) from exc

    @staticmethod
    def _sanitize(ydl: YoutubeDL, raw: Any) -> dict[str, Any]:
        sanitized = ydl.sanitize_info(raw)
        if not isinstance(sanitized, dict):
            raise DownloadFailedError("Unexpected yt-dlp metadata type")
        return sanitized

    def _safe_job_directory(self, requested: Path) -> Path:
        root = self._settings.storage.downloads_path()
        resolved = requested.resolve()
        if not resolved.is_relative_to(root):
            raise DownloadFailedError("Output directory escapes configured storage root")
        return resolved

    @staticmethod
    def _reset_job_directory(job_dir: Path) -> None:
        if job_dir.exists():
            shutil.rmtree(job_dir)
        job_dir.mkdir(parents=True, exist_ok=False)
