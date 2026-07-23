from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import MediaTooLargeError
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest

FormatSelector = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]
_VIDEO_TARGET_HEIGHTS = {
    DownloadMode.BEST: 1080,
    DownloadMode.VIDEO_1080: 1080,
    DownloadMode.VIDEO_720: 720,
    DownloadMode.VIDEO_480: 480,
}


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

    def download_options(
        self,
        request: DownloadRequest,
        *,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        postprocessor_hook: Callable[[dict[str, Any]], None] | None = None,
        match_filter: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> dict[str, Any]:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        temp_directory = request.temp_directory or request.output_directory / ".tmp"
        temp_directory.mkdir(parents=True, exist_ok=True)
        output_template = "%(id)s.%(ext)s"
        options = self._base_options()
        options.update(
            {
                "format": self._settings.media.formats.for_mode(request.mode),
                "outtmpl": {"default": output_template, "thumbnail": output_template},
                "paths": {
                    "home": str(request.output_directory),
                    "temp": str(temp_directory),
                },
                "writethumbnail": self._settings.yt_dlp.write_thumbnail,
                "postprocessors": self._postprocessors(request.mode),
            }
        )
        if progress_hook is not None:
            options["progress_hooks"] = [progress_hook]
        if postprocessor_hook is not None:
            options["postprocessor_hooks"] = [postprocessor_hook]
        if match_filter is not None:
            options["match_filter"] = match_filter
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
            "js_runtimes": {ytdlp.javascript_runtime: {}},
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


def bounded_format_selector(
    base_selector: FormatSelector,
    *,
    mode: DownloadMode,
    max_size_bytes: int,
) -> FormatSelector:
    """Choose the best complete configured selection whose known stream sum fits."""

    def select(context: dict[str, Any]) -> Iterator[dict[str, Any]]:
        formats = [item for item in context.get("formats", []) if isinstance(item, dict)]
        target_height = video_target_height(mode)
        if target_height is not None:
            formats = [
                item
                for item in formats
                if not _is_video(item)
                or not isinstance(item.get("height"), (int, float))
                or int(item["height"]) <= target_height
            ]
            sdr_heights = {
                int(item["height"])
                for item in formats
                if _is_video(item)
                and not _is_hdr(item)
                and isinstance(item.get("height"), (int, float))
            }
            formats = [
                item
                for item in formats
                if not _is_hdr(item)
                or not isinstance(item.get("height"), (int, float))
                or int(item["height"]) not in sdr_heights
            ]
        quality_index = {
            str(item.get("format_id")): index
            for index, item in enumerate(formats)
            if item.get("format_id") is not None
        }
        pending: deque[frozenset[str]] = deque([frozenset()])
        visited: set[frozenset[str]] = set()
        best: tuple[tuple[int, ...], dict[str, Any]] | None = None
        unknown: tuple[tuple[int, ...], dict[str, Any]] | None = None

        while pending and len(visited) < 1024:
            excluded = pending.popleft()
            if excluded in visited:
                continue
            visited.add(excluded)
            available = [item for item in formats if str(item.get("format_id")) not in excluded]
            candidate_context = {**context, "formats": available}
            for candidate in base_selector(candidate_context):
                components = _selected_components(candidate)
                score = _selection_score(components, quality_index, mode)
                size = _known_total_size(components)
                if size is None:
                    if unknown is None or score > unknown[0]:
                        unknown = (score, candidate)
                    continue
                if size <= max_size_bytes:
                    if best is None or score > best[0]:
                        best = (score, candidate)
                    continue
                for component in components:
                    format_id = component.get("format_id")
                    if format_id is not None:
                        pending.append(excluded | {str(format_id)})

        if best is not None:
            yield best[1]
            return
        if unknown is not None:
            yield unknown[1]
            return
        raise MediaTooLargeError("No complete configured format fits the size limit")

    return select


def video_target_height(mode: DownloadMode) -> int | None:
    return _VIDEO_TARGET_HEIGHTS.get(mode)


def _is_video(item: Mapping[str, Any]) -> bool:
    return item.get("vcodec") not in {None, "none"}


def _selected_components(candidate: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    requested = candidate.get("requested_formats")
    if isinstance(requested, list):
        components = tuple(item for item in requested if isinstance(item, Mapping))
        if components:
            return components
    return (candidate,)


def _known_total_size(components: tuple[Mapping[str, Any], ...]) -> int | None:
    total = 0
    for component in components:
        raw_size = component.get("filesize") or component.get("filesize_approx")
        if not isinstance(raw_size, (int, float)) or raw_size <= 0:
            return None
        total += int(raw_size)
    return total


def _selection_score(
    components: tuple[Mapping[str, Any], ...],
    quality_index: dict[str, int],
    mode: DownloadMode,
) -> tuple[int, ...]:
    video_components = tuple(item for item in components if _is_video(item))
    video = max(
        (quality_index.get(str(item.get("format_id")), -1) for item in video_components),
        default=-1,
    )
    video_height = max(
        (
            int(item["height"])
            for item in video_components
            if isinstance(item.get("height"), (int, float))
        ),
        default=-1,
    )
    standard_dynamic_range = int(
        bool(video_components) and all(not _is_hdr(item) for item in video_components)
    )
    audio = max(
        (
            quality_index.get(str(item.get("format_id")), -1)
            for item in components
            if item.get("acodec") not in {None, "none"}
        ),
        default=-1,
    )
    if mode in {DownloadMode.AUDIO_BEST, DownloadMode.AUDIO_MP3}:
        return audio, video_height, standard_dynamic_range, video
    return video_height, standard_dynamic_range, video, audio


def _is_hdr(item: Mapping[str, Any]) -> bool:
    dynamic_range = item.get("dynamic_range")
    if isinstance(dynamic_range, str) and dynamic_range.casefold() not in {"", "sdr"}:
        return True
    format_note = item.get("format_note")
    return isinstance(format_note, str) and "hdr" in format_note.casefold()


def final_media_files(directory: Path) -> list[Path]:
    ignored_suffixes = {".part", ".ytdl", ".tmp", ".temp", ".json"}
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and ".tmp" not in path.relative_to(directory).parts
        and path.suffix.casefold() not in ignored_suffixes
    ]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True)
