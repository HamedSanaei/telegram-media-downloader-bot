from __future__ import annotations

import mimetypes
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as ytdlp_version

from telegram_media_bot.application.ports.download_engine import CancellationCheck, ProgressSink
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DownloadFailedError,
    JobCancelledError,
    MediaBotError,
    MediaTooLargeError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadRequest,
    DownloadResult,
    MediaInfo,
    MediaKind,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.infrastructure.ytdlp.error_mapper import map_ytdlp_error
from telegram_media_bot.infrastructure.ytdlp.mapper import (
    detect_kind,
    map_media_info,
    normalize_source,
)
from telegram_media_bot.infrastructure.ytdlp.options import (
    YtDlpOptionsFactory,
    bounded_format_selector,
    final_media_files,
)


class YtDlpEngine:
    """The only application adapter that directly knows yt-dlp types and options."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._options = YtDlpOptionsFactory(settings)
        self._url_validator = PublicUrlValidator(
            reject_private_networks=settings.security.reject_private_network_urls
        )

    def inspect(self, url: str) -> MediaInfo:
        try:
            with YoutubeDL(self._options.inspect_options()) as ydl:
                raw = ydl.extract_info(url, download=False)
                info = self._sanitize(ydl, raw)
            self._validate_info_urls(info)
        except MediaBotError:
            raise
        except Exception as exc:
            raise map_ytdlp_error(exc) from exc
        return map_media_info(info, original_url=url)

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> DownloadResult:
        job_dir = self._safe_job_directory(request.output_directory)
        self._reset_job_directory(job_dir)
        if request.temp_directory is not None:
            temp_dir = self._safe_temp_directory(request.temp_directory)
            self._reset_job_directory(temp_dir)
        max_size = self._settings.media.max_file_size_mb * 1024 * 1024
        observed_downloads: dict[str, int] = {}

        def cancellation_check() -> None:
            if is_cancelled is not None and is_cancelled():
                raise JobCancelledError("Download was cancelled")

        def progress_hook(raw_progress: dict[str, Any]) -> None:
            cancellation_check()
            downloaded = raw_progress.get("downloaded_bytes")
            if isinstance(downloaded, (int, float)):
                progress_key = str(
                    raw_progress.get("filename") or raw_progress.get("tmpfilename") or "current"
                )
                observed_downloads[progress_key] = max(0, int(downloaded))
                if sum(observed_downloads.values()) > max_size:
                    raise MediaTooLargeError("Downloaded streams exceed configured size limit")
            if progress is not None:
                progress(self._map_progress(request, raw_progress))

        def postprocessor_hook(raw_progress: dict[str, Any]) -> None:
            cancellation_check()
            if progress is not None:
                progress(
                    ProgressEvent(
                        job_id=request.job_id,
                        status=str(raw_progress.get("status") or "postprocessing"),
                    )
                )

        try:
            cancellation_check()
            options = self._options.download_options(
                request,
                progress_hook=progress_hook,
                postprocessor_hook=postprocessor_hook,
                match_filter=self._match_filter,
            )
            with YoutubeDL(options) as ydl:
                base_selector = ydl.format_selector
                if not callable(base_selector):
                    raise DownloadFailedError("Configured format selector is unavailable")
                ydl.format_selector = bounded_format_selector(
                    base_selector,
                    mode=request.mode,
                    max_size_bytes=max_size,
                )
                raw = ydl.extract_info(request.url, download=True)
                info = self._sanitize(ydl, raw)
            self._validate_info_urls(info)
            files = final_media_files(job_dir)
            if not files:
                raise DownloadFailedError("yt-dlp completed without a final output file")
            detected_kind = detect_kind(info)
            if detected_kind is not MediaKind.IMAGE:
                non_images = [
                    path
                    for path in files
                    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                ]
                if non_images:
                    files = non_images
            if sum(path.stat().st_size for path in files) > max_size:
                raise MediaTooLargeError("Final media exceeds configured size limit")
            final_file = self._bundle_playlist(job_dir, files) if len(files) > 1 else files[0]
            size = final_file.stat().st_size
            if size > max_size:
                raise MediaTooLargeError("Final media exceeds configured size limit")
            return DownloadResult(
                job_id=request.job_id,
                media_id=str(info.get("id") or final_file.stem),
                title=str(info.get("title") or "Untitled"),
                source=normalize_source(info),
                kind=MediaKind.PLAYLIST if len(files) > 1 else detect_kind(info),
                file_path=final_file,
                file_size_bytes=size,
                duration_seconds=map_media_info(info, original_url=request.url).duration_seconds,
                mime_type=mimetypes.guess_type(final_file.name)[0],
            )
        except Exception as exc:
            if isinstance(exc, MediaBotError):
                raise
            raise map_ytdlp_error(exc) from exc

    def health(self) -> ComponentHealth:
        return ComponentHealth(name="yt_dlp", healthy=True, detail=ytdlp_version)

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

    def _safe_temp_directory(self, requested: Path) -> Path:
        root = self._settings.storage.temp_path()
        resolved = requested.resolve()
        if not resolved.is_relative_to(root):
            raise DownloadFailedError("Temporary directory escapes configured storage root")
        return resolved

    @staticmethod
    def _reset_job_directory(job_dir: Path) -> None:
        if job_dir.exists():
            shutil.rmtree(job_dir)
        job_dir.mkdir(parents=True, exist_ok=False)

    def _match_filter(self, info: dict[str, Any]) -> str | None:
        self._validate_info_urls(info)
        return None

    def _validate_info_urls(self, info: Mapping[str, Any]) -> None:
        if not self._settings.security.reject_private_network_urls:
            return
        candidates: list[str] = []
        for key in ("webpage_url", "original_url", "url", "manifest_url"):
            value = info.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                candidates.append(value)
        requested = info.get("requested_formats")
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, Mapping):
                    value = item.get("url")
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        candidates.append(value)
        entries = info.get("entries")
        if isinstance(entries, list):
            for entry in entries[: self._settings.media.playlist_max_items]:
                if isinstance(entry, Mapping):
                    self._validate_info_urls(entry)
        for candidate in candidates:
            self._url_validator.validate(candidate)

    @staticmethod
    def _map_progress(request: DownloadRequest, raw: Mapping[str, Any]) -> ProgressEvent:
        total_raw = raw.get("total_bytes") or raw.get("total_bytes_estimate")
        downloaded_raw = raw.get("downloaded_bytes")
        speed_raw = raw.get("speed")
        eta_raw = raw.get("eta")
        return ProgressEvent(
            job_id=request.job_id,
            status=str(raw.get("status") or "downloading"),
            downloaded_bytes=int(downloaded_raw) if isinstance(downloaded_raw, (int, float)) else 0,
            total_bytes=int(total_raw) if isinstance(total_raw, (int, float)) else None,
            speed_bytes_per_second=(
                float(speed_raw) if isinstance(speed_raw, (int, float)) else None
            ),
            eta_seconds=int(eta_raw) if isinstance(eta_raw, (int, float)) else None,
        )

    @staticmethod
    def _bundle_playlist(job_dir: Path, files: list[Path]) -> Path:
        archive = job_dir / "playlist.zip"
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=6) as bundle:
            for index, path in enumerate(files, start=1):
                bundle.write(path, arcname=f"{index:03d}-{path.name}")
        for path in files:
            path.unlink(missing_ok=True)
        return archive
