from __future__ import annotations

from pathlib import Path
from typing import Any

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest


class YtDlpOptionsFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def inspect_options(self) -> dict[str, Any]:
        options = self._base_options()
        options.update(
            {
                "skip_download": True,
                "extract_flat": False,
                "playlistend": self._settings.media.playlist_max_items,
            }
        )
        return options

    def download_options(self, request: DownloadRequest) -> dict[str, Any]:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        output_template = str(request.output_directory / "%(id)s.%(ext)s")
        options = self._base_options()
        options.update(
            {
                "format": self._settings.media.formats.for_mode(request.mode),
                "outtmpl": {"default": output_template, "thumbnail": output_template},
                "paths": {
                    "home": str(request.output_directory),
                    "temp": str(request.output_directory / ".tmp"),
                },
                "max_filesize": self._settings.media.max_file_size_mb * 1024 * 1024,
                "writethumbnail": self._settings.yt_dlp.write_thumbnail,
                "postprocessors": self._postprocessors(request.mode),
            }
        )
        return options

    def _base_options(self) -> dict[str, Any]:
        ytdlp = self._settings.yt_dlp
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "noplaylist": not self._settings.media.allow_playlists,
            "playlistend": self._settings.media.playlist_max_items,
            "socket_timeout": ytdlp.socket_timeout_seconds,
            "retries": ytdlp.retries,
            "fragment_retries": ytdlp.fragment_retries,
            "extractor_retries": ytdlp.extractor_retries,
            "concurrent_fragment_downloads": ytdlp.concurrent_fragments,
            "restrictfilenames": ytdlp.restrict_filenames,
            "overwrites": False,
            "continuedl": True,
            "nopart": False,
            "windowsfilenames": True,
        }
        if ytdlp.cookies_file and ytdlp.cookies_file.exists():
            options["cookiefile"] = str(ytdlp.cookies_file)
        if ytdlp.proxy:
            options["proxy"] = ytdlp.proxy
        if ytdlp.user_agent:
            options["user_agent"] = ytdlp.user_agent
        return options

    def _postprocessors(self, mode: DownloadMode) -> list[dict[str, Any]]:
        processors: list[dict[str, Any]] = []
        if mode is DownloadMode.AUDIO_MP3:
            processors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self._settings.yt_dlp.audio_format,
                    "preferredquality": self._settings.yt_dlp.audio_quality,
                }
            )
        if self._settings.yt_dlp.embed_metadata:
            processors.append({"key": "FFmpegMetadata"})
        if self._settings.yt_dlp.embed_thumbnail:
            processors.append({"key": "EmbedThumbnail"})
        return processors


def final_media_files(directory: Path) -> list[Path]:
    ignored_suffixes = {".part", ".ytdl", ".tmp", ".temp", ".json"}
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() not in ignored_suffixes
    ]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True)
