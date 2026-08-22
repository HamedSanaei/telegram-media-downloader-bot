from __future__ import annotations

import shutil
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    MediaTooLargeError,
    MediaUnavailableError,
    NativeFormatUnavailableError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    MediaFormatOption,
    MediaProcessingKind,
    Mp4NativeFallback,
    NativeVideoCodec,
    OutputContainer,
    SizeConfidence,
)

FormatSelector = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]
_VIDEO_TARGET_HEIGHTS = {
    DownloadMode.BEST: 1080,
    DownloadMode.VIDEO_2160: 2160,
    DownloadMode.VIDEO_1440: 1440,
    DownloadMode.VIDEO_1080: 1080,
    DownloadMode.VIDEO_720: 720,
    DownloadMode.VIDEO_480: 480,
}
_FIXED_VIDEO_MODES = {
    DownloadMode.VIDEO_2160,
    DownloadMode.VIDEO_1440,
    DownloadMode.VIDEO_1080,
    DownloadMode.VIDEO_720,
    DownloadMode.VIDEO_480,
}


class YtDlpOptionsFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def inspect_options(self, *, single_video: bool = False) -> dict[str, Any]:
        """Build options for metadata-only inspection.

        yt-dlp's format probing (``_check_formats``) writes its scratch files under the
        configured ``temp`` output path and silently falls back to the process working
        directory (or the ambient temp resolution) when it is unset, which fails on the
        read-only application filesystem used in production. Every inspection therefore
        receives a private workspace beneath the configured writable storage temp root, and
        both ``home`` and ``temp`` point at it so no yt-dlp path resolution can fall back to
        the working directory. Callers must delete the workspace with
        :func:`remove_inspection_workspace` when the ``YoutubeDL`` run finishes; the
        maintenance sweep reclaims it as an ordinary orphan if a crash leaks it.
        """
        options = self._base_options()
        workspace = self._create_inspection_workspace()
        options.update(
            {
                "skip_download": True,
                "extract_flat": False,
                "playlistend": max(
                    self._settings.media.playlist_max_items,
                    self._settings.media.instagram.max_videos,
                ),
                "noplaylist": single_video,
                "paths": {"home": str(workspace), "temp": str(workspace)},
            }
        )
        return options

    def _create_inspection_workspace(self) -> Path:
        """Create a unique writable scratch directory for one inspection run.

        The directory lives under the canonical configured storage temp hierarchy, is owned
        by the runtime user that creates it, never collides with per-job workspaces (which
        are named by job id), and is removed by the caller after the run.
        """
        root = self._settings.storage.temp_path()
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="inspect-", dir=root))

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
                "format": self.format_for_request(request),
                "outtmpl": {"default": output_template, "thumbnail": output_template},
                "paths": {
                    "home": str(request.output_directory),
                    "temp": str(temp_directory),
                },
                "writethumbnail": self._settings.yt_dlp.write_thumbnail,
                "postprocessors": self._postprocessors(request.mode),
            }
        )
        if request.mode in {
            DownloadMode.YOUTUBE_THUMBNAIL,
            DownloadMode.SOUNDCLOUD_ARTWORK,
        }:
            options.update(
                {
                    "format": "b",
                    "skip_download": True,
                    "writethumbnail": True,
                    "write_all_thumbnails": False,
                    "postprocessors": [],
                }
            )
        if canonicalize_media_url(request.url).single_video_forced:
            options["noplaylist"] = True
        if request.container in {OutputContainer.MP4, OutputContainer.WEBM}:
            options["merge_output_format"] = request.container.value
            if request.container_policy is not ContainerPolicy.EXPLICIT_TRANSCODE:
                options["postprocessor_args"] = {
                    "merger+ffmpeg_o": native_merge_output_args(request.container)
                }
        if request.allow_collection:
            options["noplaylist"] = False
            options["playlistend"] = self._settings.media.instagram.max_videos
        if progress_hook is not None:
            options["progress_hooks"] = [progress_hook]
        if postprocessor_hook is not None:
            options["postprocessor_hooks"] = [postprocessor_hook]
        if match_filter is not None:
            options["match_filter"] = match_filter
        return options

    def format_for_request(self, request: DownloadRequest) -> str:
        if request.mode in {
            DownloadMode.YOUTUBE_THUMBNAIL,
            DownloadMode.SOUNDCLOUD_ARTWORK,
        }:
            return "b"
        if request.selected_format_ids:
            return "+".join(request.selected_format_ids)
        base = self._settings.media.formats.for_mode(request.mode)
        if request.container not in {OutputContainer.MP4, OutputContainer.WEBM}:
            return base
        native = native_container_selector(request.container)
        if request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE:
            return base
        return native

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
        if cookie_file := self._settings.effective_cookie_file():
            options["cookiefile"] = str(cookie_file)
        proxy = ytdlp.effective_proxy()
        if proxy is not None:
            options["proxy"] = proxy
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


def inspection_workspace_path(options: Mapping[str, Any]) -> Path | None:
    """Return the private inspection workspace embedded in ``inspect_options()`` output."""
    paths = options.get("paths")
    if not isinstance(paths, Mapping):
        return None
    home = paths.get("home")
    if not isinstance(home, str) or not home:
        return None
    return Path(home)


def remove_inspection_workspace(options: Mapping[str, Any]) -> None:
    """Delete the private inspection workspace created for one ``YoutubeDL`` run.

    Removal is best-effort: a leftover directory is reclaimed by the maintenance sweep as an
    ordinary orphan after the configured grace period.
    """
    workspace = inspection_workspace_path(options)
    if workspace is None:
        return
    shutil.rmtree(workspace, ignore_errors=True)


def bounded_format_selector(
    base_selector: FormatSelector,
    *,
    mode: DownloadMode,
    max_size_bytes: int,
    compatible_container: OutputContainer | None = None,
    native_video_codec: NativeVideoCodec | None = None,
    mp4_native_fallback: Mp4NativeFallback = Mp4NativeFallback.FAIL,
) -> FormatSelector:
    """Choose the best complete configured selection whose known stream sum fits."""

    def select(context: dict[str, Any]) -> Iterator[dict[str, Any]]:
        formats = [item for item in context.get("formats", []) if isinstance(item, dict)]
        candidate_count = len(formats)
        if compatible_container is not None:
            formats = [
                item
                for item in formats
                if _component_matches_container(
                    item,
                    compatible_container,
                    native_video_codec=native_video_codec,
                )
            ]
        target_height = video_target_height(mode)
        allow_lower_height = (
            compatible_container is OutputContainer.MP4
            and mp4_native_fallback is Mp4NativeFallback.LOWER_RESOLUTION
        )
        if target_height is not None:
            formats = [
                item
                for item in formats
                if not _is_video(item)
                or (
                    mode not in _FIXED_VIDEO_MODES
                    and not isinstance(item.get("height"), (int, float))
                )
                or (
                    isinstance(item.get("height"), (int, float))
                    and (
                        int(item["height"]) == target_height
                        if mode in _FIXED_VIDEO_MODES and not allow_lower_height
                        else int(item["height"]) <= target_height
                    )
                )
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
        complete_candidates_seen = False

        while pending and len(visited) < 1024:
            excluded = pending.popleft()
            if excluded in visited:
                continue
            visited.add(excluded)
            available = [item for item in formats if str(item.get("format_id")) not in excluded]
            candidate_context = {
                **context,
                "formats": available,
                "has_merged_format": any(
                    item.get("vcodec") != "none" and item.get("acodec") != "none"
                    for item in available
                ),
                "incomplete_formats": (
                    all(item.get("vcodec") == "none" for item in available)
                    or all(item.get("acodec") == "none" for item in available)
                ),
            }
            for candidate in base_selector(candidate_context):
                components = _selected_components(candidate)
                if not _is_complete_selection(components, mode):
                    continue
                complete_candidates_seen = True
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
        if not complete_candidates_seen:
            # The source offers no complete selection at all (for example a silent video-only
            # story has no audio stream); that is a format-availability condition, never a size
            # violation. Classifying it as too_large produced false oversized failures.
            raise NativeFormatUnavailableError(
                "No complete configured format selection is available"
            )
        if compatible_container is not None and candidate_count:
            raise NativeFormatUnavailableError(
                f"No native {compatible_container.value} codec-compatible format is available"
            )
        raise MediaTooLargeError("No complete configured format fits the size limit")

    return select


def inspect_format_option(
    base_selector: FormatSelector,
    context: dict[str, Any],
    *,
    mode: DownloadMode,
    max_size_bytes: int,
    duration_seconds: int | None,
    mp3_bitrate_kbps: int,
    container: OutputContainer | None = None,
    container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
    requires_transcode: bool = False,
    compatible_container: OutputContainer | None = None,
    native_video_codec: NativeVideoCodec | None = None,
    mp4_native_fallback: Mp4NativeFallback = Mp4NativeFallback.FAIL,
) -> MediaFormatOption | None:
    try:
        candidate = next(
            iter(
                bounded_format_selector(
                    base_selector,
                    mode=mode,
                    max_size_bytes=max_size_bytes,
                    compatible_container=compatible_container,
                    native_video_codec=native_video_codec,
                    mp4_native_fallback=mp4_native_fallback,
                )(context)
            ),
            None,
        )
    except MediaTooLargeError, MediaUnavailableError:
        return None
    if candidate is None:
        return None
    return _describe_candidate(
        candidate,
        mode=mode,
        duration_seconds=duration_seconds,
        mp3_bitrate_kbps=mp3_bitrate_kbps,
        container=container,
        container_policy=container_policy,
        requires_transcode=requires_transcode,
    )


def video_target_height(mode: DownloadMode) -> int | None:
    return _VIDEO_TARGET_HEIGHTS.get(mode)


def _is_video(item: Mapping[str, Any]) -> bool:
    return item.get("vcodec") not in {None, "none"}


def _is_audio(item: Mapping[str, Any]) -> bool:
    if item.get("acodec") not in {None, "none"}:
        return True
    return (
        item.get("vcodec") in {None, "none"}
        and str(item.get("video_ext") or "").casefold() == "none"
        and str(item.get("audio_ext") or "").casefold() not in {"", "none"}
    ) or str(item.get("resolution") or "").casefold() == "audio only"


def effective_audio_codec(item: Mapping[str, Any]) -> str | None:
    codec = item.get("acodec")
    if codec not in {None, "none"}:
        return str(codec)
    format_id = str(item.get("format_id") or "")
    protocol = str(item.get("protocol") or "").casefold()
    ext = str(item.get("ext") or item.get("audio_ext") or "").casefold()
    if (
        _is_audio(item)
        and protocol.startswith("m3u8")
        and ext in {"mp4", "m4a"}
        and format_id.startswith("hls-audio-")
        and format_id.endswith("-Audio")
    ):
        # Twitter's HLS audio renditions omit acodec even though they are AAC in MP4.
        return "aac"
    return None


def _selected_components(candidate: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    requested = candidate.get("requested_formats")
    if isinstance(requested, list):
        components = tuple(item for item in requested if isinstance(item, Mapping))
        if components:
            return components
    return (candidate,)


def _is_complete_selection(
    components: tuple[Mapping[str, Any], ...],
    mode: DownloadMode,
) -> bool:
    has_video = any(_is_video(item) for item in components)
    has_audio = any(_is_audio(item) for item in components)
    if mode in {DownloadMode.AUDIO_BEST, DownloadMode.AUDIO_MP3}:
        return has_audio
    if mode is DownloadMode.BEST and not has_video:
        return has_audio or bool(components)
    return has_video and has_audio


def _describe_candidate(
    candidate: Mapping[str, Any],
    *,
    mode: DownloadMode,
    duration_seconds: int | None,
    mp3_bitrate_kbps: int,
    container: OutputContainer | None,
    container_policy: ContainerPolicy,
    requires_transcode: bool,
) -> MediaFormatOption:
    components = _selected_components(candidate)
    videos = tuple(item for item in components if _is_video(item))
    audios = tuple(item for item in components if _is_audio(item))
    video = max(
        videos,
        key=lambda item: (
            int(item.get("height") or 0),
            int(item.get("width") or 0),
            float(item.get("fps") or 0),
        ),
        default=None,
    )
    audio = max(
        audios,
        key=lambda item: float(item.get("abr") or item.get("tbr") or 0),
        default=None,
    )
    size_bytes: int | None
    if mode is DownloadMode.AUDIO_MP3 and duration_seconds is not None:
        size_bytes = int(duration_seconds * mp3_bitrate_kbps * 1000 / 8)
        confidence = SizeConfidence.ESTIMATED
    else:
        size_bytes, confidence = _estimated_total_size(components, duration_seconds)
    transcode_required = requires_transcode or (
        container is not None
        and container in {OutputContainer.MP4, OutputContainer.WEBM}
        and container_policy in {ContainerPolicy.GUARANTEED, ContainerPolicy.EXPLICIT_TRANSCODE}
        and not _components_match_container(components, container)
    )
    return MediaFormatOption(
        mode=mode,
        container=container,
        container_policy=container_policy,
        requires_transcode=transcode_required,
        processing_kind=(
            MediaProcessingKind.TRANSCODE
            if transcode_required
            else MediaProcessingKind.REMUX
            if len(components) > 1
            else MediaProcessingKind.DIRECT
        ),
        width=_positive_int(video.get("width")) if video is not None else None,
        height=_positive_int(video.get("height")) if video is not None else None,
        fps=_positive_float(video.get("fps")) if video is not None else None,
        is_hdr=any(_is_hdr(item) for item in videos),
        size_bytes=size_bytes,
        size_confidence=confidence,
        selection_reason=_selection_reason(
            mode=mode,
            container=container,
            container_policy=container_policy,
            components=components,
            selected_height=_positive_int(video.get("height")) if video is not None else None,
        ),
        fallback_reason=_fallback_reason(
            mode=mode,
            container=container,
            selected_height=_positive_int(video.get("height")) if video is not None else None,
        ),
        selected_format_ids=tuple(
            str(component["format_id"])
            for component in components
            if component.get("format_id") is not None
        ),
        video_codec=(
            str(video["vcodec"]) if video is not None and video.get("vcodec") is not None else None
        ),
        audio_codec=(effective_audio_codec(audio) if audio is not None else None),
        dynamic_range=_dynamic_range(video),
        video_size_bytes=_component_size(video)[0] if video is not None else None,
        audio_size_bytes=_component_size(audio)[0] if audio is not None else None,
        quality_score=(
            float(video.get("tbr") or video.get("vbr") or 0)
            if video is not None
            else float(audio.get("abr") or audio.get("tbr") or 0)
            if audio is not None
            else None
        ),
    )


def _estimated_total_size(
    components: tuple[Mapping[str, Any], ...],
    duration_seconds: int | None,
) -> tuple[int | None, SizeConfidence]:
    total = 0
    confidence = SizeConfidence.EXACT
    for component in components:
        exact = component.get("filesize")
        if isinstance(exact, (int, float)) and exact > 0:
            total += int(exact)
            continue
        approximate = component.get("filesize_approx")
        if isinstance(approximate, (int, float)) and approximate > 0:
            total += int(approximate)
            confidence = SizeConfidence.ESTIMATED
            continue
        bitrate = component.get("tbr") or component.get("vbr") or component.get("abr")
        if duration_seconds is not None and isinstance(bitrate, (int, float)) and bitrate > 0:
            total += int(duration_seconds * float(bitrate) * 1000 / 8)
            confidence = SizeConfidence.ESTIMATED
            continue
        return None, SizeConfidence.UNKNOWN
    return total, confidence


def _component_size(component: Mapping[str, Any]) -> tuple[int | None, SizeConfidence]:
    exact = component.get("filesize")
    if isinstance(exact, (int, float)) and exact > 0:
        return int(exact), SizeConfidence.EXACT
    approximate = component.get("filesize_approx")
    if isinstance(approximate, (int, float)) and approximate > 0:
        return int(approximate), SizeConfidence.ESTIMATED
    return None, SizeConfidence.UNKNOWN


def _dynamic_range(video: Mapping[str, Any] | None) -> str | None:
    if video is None:
        return None
    value = video.get("dynamic_range")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return "HDR" if _is_hdr(video) else "SDR"


def _positive_int(value: object) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def _positive_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


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
            if _is_audio(item)
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


def native_container_selector(container: OutputContainer) -> str:
    if container is OutputContainer.MP4:
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=mp4]/b[ext=mp4]"
    if container is OutputContainer.WEBM:
        return "bv*[ext=webm]+ba[ext=webm]/b[ext=webm]"
    return "ba/b"


def native_merge_output_args(container: OutputContainer) -> list[str]:
    """Return explicit stream-copy output arguments for yt-dlp's FFmpeg merger."""
    args = ["-c:v", "copy", "-c:a", "copy"]
    if container is OutputContainer.MP4:
        args.extend(["-movflags", "+faststart"])
    return args


def _components_match_container(
    components: tuple[Mapping[str, Any], ...],
    container: OutputContainer,
) -> bool:
    video_codecs = {
        str(item.get("vcodec") or "").casefold() for item in components if _is_video(item)
    }
    audio_codecs = {
        str(effective_audio_codec(item) or "").casefold() for item in components if _is_audio(item)
    }
    if container is OutputContainer.MP4:
        return all(
            codec == "h264"
            or codec.startswith("avc1")
            or codec == "av1"
            or codec.startswith("av01")
            for codec in video_codecs
        ) and all(codec == "aac" or codec.startswith("mp4a") for codec in audio_codecs)
    if container is OutputContainer.WEBM:
        return all(codec == "vp9" or codec.startswith("vp09") for codec in video_codecs) and all(
            codec == "opus" for codec in audio_codecs
        )
    return False


def _component_matches_container(
    item: Mapping[str, Any],
    container: OutputContainer,
    *,
    native_video_codec: NativeVideoCodec | None = None,
) -> bool:
    ext = str(item.get("ext") or item.get("container") or "").casefold()
    video_codec = str(item.get("vcodec") or "").casefold()
    audio_codec = str(effective_audio_codec(item) or "").casefold()
    has_video = video_codec not in {"", "none"}
    has_audio = _is_audio(item)
    if not has_video and not has_audio:
        return False
    if container is OutputContainer.MP4:
        if native_video_codec is NativeVideoCodec.AV1:
            video_codec_ok = video_codec == "av1" or video_codec.startswith("av01")
        elif native_video_codec is NativeVideoCodec.H264:
            video_codec_ok = video_codec == "h264" or video_codec.startswith("avc1")
        else:
            video_codec_ok = video_codec == "h264" or video_codec.startswith("avc1")
        video_ok = not has_video or (ext in {"mp4", "m4v"} and video_codec_ok)
        audio_ok = not has_audio or (
            ext in {"m4a", "mp4"} and (audio_codec == "aac" or audio_codec.startswith("mp4a"))
        )
        return video_ok and audio_ok
    if container is OutputContainer.WEBM:
        if native_video_codec not in {None, NativeVideoCodec.VP9}:
            return False
        video_ok = not has_video or (
            ext == "webm" and (video_codec == "vp9" or video_codec.startswith("vp09"))
        )
        audio_ok = not has_audio or (ext == "webm" and audio_codec == "opus")
        return video_ok and audio_ok
    return False


def _selection_reason(
    *,
    mode: DownloadMode,
    container: OutputContainer | None,
    container_policy: ContainerPolicy,
    components: tuple[Mapping[str, Any], ...],
    selected_height: int | None,
) -> str | None:
    if mode is DownloadMode.BEST_ORIGINAL:
        return "best_original_native"
    if container_policy is ContainerPolicy.EXPLICIT_TRANSCODE:
        return "explicit_transcode_selected"
    if container is OutputContainer.WEBM:
        return "webm_native"
    if container is not OutputContainer.MP4:
        return None
    target_height = video_target_height(mode)
    if target_height is not None and selected_height == target_height:
        return "native_mp4_exact_resolution"
    if (
        target_height is not None
        and selected_height is not None
        and selected_height < target_height
    ):
        return "native_mp4_lower_resolution"
    if len(components) == 1:
        return "native_combined_mp4"
    return "native_mp4_exact_resolution"


def _fallback_reason(
    *,
    mode: DownloadMode,
    container: OutputContainer | None,
    selected_height: int | None,
) -> str | None:
    target_height = video_target_height(mode)
    if (
        container is OutputContainer.MP4
        and target_height is not None
        and selected_height is not None
        and selected_height < target_height
    ):
        return "exact_native_mp4_not_available"
    return None


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
