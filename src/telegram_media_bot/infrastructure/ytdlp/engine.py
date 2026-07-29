from __future__ import annotations

import mimetypes
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import structlog
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as ytdlp_version

from telegram_media_bot.application.ports.download_engine import CancellationCheck, ProgressSink
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DownloadFailedError,
    JobCancelledError,
    MediaBotError,
    MediaTooLargeError,
    MediaUnavailableError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    ContainerPolicy,
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    ProgressEvent,
    SizeConfidence,
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
    inspect_format_option,
    native_container_selector,
    video_target_height,
)
from telegram_media_bot.infrastructure.ytdlp.transcoder import (
    TranscodeGate,
    is_guaranteed_container_compatible,
    is_inline_video_streamable,
    probe_video,
    transcode_video_to_container,
    transcode_video_to_limit,
)

logger = structlog.get_logger(__name__)


def _sum_known(values: list[int | None]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


class YtDlpEngine:
    """The only application adapter that directly knows yt-dlp types and options."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._options = YtDlpOptionsFactory(settings)
        self._url_validator = PublicUrlValidator(
            reject_private_networks=settings.security.reject_private_network_urls
        )
        self._transcode_gate = TranscodeGate(settings.media.transcode.max_concurrent)

    def inspect(self, url: str) -> MediaInfo:
        try:
            with YoutubeDL(self._options.inspect_options()) as ydl:
                raw = ydl.extract_info(url, download=False)
                format_options = self._inspect_format_options(ydl, raw)
                info = self._sanitize(ydl, raw)
            self._validate_info_urls(info)
        except MediaBotError:
            raise
        except Exception as exc:
            raise map_ytdlp_error(exc) from exc
        return replace(
            map_media_info(info, original_url=url),
            format_options=format_options,
        )

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
        if request.allow_collection:
            max_size = min(
                max_size,
                self._settings.media.instagram.max_total_size_mb * 1024 * 1024,
            )
        source_max_size = self._settings.media.max_source_size_mb * 1024 * 1024
        target_height = video_target_height(request.mode)
        transfer_limit = source_max_size if target_height is not None else max_size
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
                if sum(observed_downloads.values()) > transfer_limit:
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
                    max_size_bytes=transfer_limit,
                    compatible_container=(
                        request.container
                        if request.container_policy is ContainerPolicy.GUARANTEED
                        and request.container in {OutputContainer.MP4, OutputContainer.WEBM}
                        else None
                    ),
                    mp4_native_fallback=self._settings.media.mp4_native_fallback,
                )
                raw = ydl.extract_info(request.url, download=True)
                info = self._sanitize(ydl, raw)
            self._validate_info_urls(info)
            files = final_media_files(job_dir)
            if not files:
                raise DownloadFailedError("yt-dlp completed without a final output file")
            detected_kind = detect_kind(info)
            if request.allow_collection or detected_kind is not MediaKind.IMAGE:
                non_images = [
                    path
                    for path in files
                    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                ]
                if request.allow_collection and not non_images:
                    raise MediaUnavailableError("Instagram collection contains no videos")
                if non_images:
                    files = non_images
            if request.allow_collection:
                files = _order_collection_files(files, info)
            requested_container = request.container
            requested_video_container = requested_container in {
                OutputContainer.MP4,
                OutputContainer.WEBM,
            }
            incompatible_container = (
                len(files) == 1
                and detected_kind is MediaKind.VIDEO
                and requested_container is not None
                and requested_video_container
                and request.container_policy
                in {ContainerPolicy.GUARANTEED, ContainerPolicy.EXPLICIT_TRANSCODE}
                and not is_guaranteed_container_compatible(files[0], requested_container)
            )
            if incompatible_container and request.container_policy is ContainerPolicy.GUARANTEED:
                raise MediaUnavailableError(
                    "Downloaded media does not satisfy the requested native codec contract"
                )
            should_transcode = (
                len(files) == 1
                and detected_kind is MediaKind.VIDEO
                and (
                    (
                        requested_container is not None
                        and requested_video_container
                        and request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
                        and (
                            incompatible_container
                            or files[0].suffix.casefold() != f".{requested_container.value}"
                            or files[0].stat().st_size > max_size
                        )
                    )
                    or (
                        request.container is None
                        and target_height is not None
                        and files[0].stat().st_size > max_size
                    )
                )
            )
            transcode_reason = (
                "explicit_transcode_selected"
                if incompatible_container
                and request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
                else "size_limit"
                if should_transcode and files[0].stat().st_size > max_size
                else "guaranteed_container_contract"
                if should_transcode
                else None
            )
            if detected_kind is MediaKind.VIDEO or request.allow_collection:
                self._log_selected_media(info, files, request, transcode_reason)
            if should_transcode:
                if progress is not None:
                    progress(ProgressEvent(job_id=request.job_id, status="transcoding"))
                if request.container is None:
                    transcoded = transcode_video_to_limit(
                        files[0],
                        target_height=target_height or 4320,
                        max_size_bytes=max_size,
                        is_cancelled=is_cancelled,
                        **self._transcode_options(),
                    )
                else:
                    transcoded = transcode_video_to_container(
                        files[0],
                        target_height=target_height or 4320,
                        max_size_bytes=max_size,
                        container=request.container,
                        is_cancelled=is_cancelled,
                        reason=transcode_reason or "guaranteed_container_contract",
                        **self._transcode_options(),
                    )
                files = [transcoded]
            elif (
                request.allow_collection
                and request.container in {OutputContainer.MP4, OutputContainer.WEBM}
                and request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
            ):
                converted: list[Path] = []
                for path in files:
                    if is_guaranteed_container_compatible(path, request.container):
                        converted.append(path)
                        continue
                    if progress is not None:
                        progress(ProgressEvent(job_id=request.job_id, status="transcoding"))
                    converted.append(
                        transcode_video_to_container(
                            path,
                            target_height=4320,
                            max_size_bytes=max_size,
                            container=request.container,
                            is_cancelled=is_cancelled,
                            reason="guaranteed_collection_codec_contract",
                            **self._transcode_options(),
                        )
                    )
                files = converted
            if sum(path.stat().st_size for path in files) > max_size:
                raise MediaTooLargeError("Final media exceeds configured size limit")
            source = normalize_source(info)
            is_instagram_collection = source == "instagram" and len(files) > 1
            final_file = (
                files[0]
                if is_instagram_collection
                else self._bundle_playlist(job_dir, files)
                if len(files) > 1
                else files[0]
            )
            size = (
                sum(path.stat().st_size for path in files)
                if is_instagram_collection
                else final_file.stat().st_size
            )
            if size > max_size:
                raise MediaTooLargeError("Final media exceeds configured size limit")
            artifacts = (
                tuple(
                    DownloadArtifact(
                        file_path=path,
                        file_size_bytes=path.stat().st_size,
                        kind=MediaKind.VIDEO,
                        mime_type=mimetypes.guess_type(path.name)[0],
                        title=f"{info.get('title') or 'Instagram'!s} {index}",
                        inline_video_streamable=self._inline_streamable(path),
                    )
                    for index, path in enumerate(files, start=1)
                )
                if is_instagram_collection
                else ()
            )
            return DownloadResult(
                job_id=request.job_id,
                media_id=str(info.get("id") or final_file.stem),
                title=str(info.get("title") or "Untitled"),
                source=source,
                kind=MediaKind.PLAYLIST if len(files) > 1 else detect_kind(info),
                file_path=final_file,
                file_size_bytes=size,
                duration_seconds=map_media_info(info, original_url=request.url).duration_seconds,
                mime_type=mimetypes.guess_type(final_file.name)[0],
                artifacts=artifacts,
                inline_video_streamable=self._inline_streamable(final_file),
            )
        except Exception as exc:
            if isinstance(exc, MediaBotError):
                raise
            raise map_ytdlp_error(exc) from exc

    def health(self) -> ComponentHealth:
        return ComponentHealth(name="yt_dlp", healthy=True, detail=ytdlp_version)

    def _transcode_options(self) -> dict[str, Any]:
        settings = self._settings.media.transcode
        return {
            "threads": settings.threads,
            "timeout_seconds": settings.timeout_seconds,
            "progress_interval_seconds": settings.progress_interval_seconds,
            "gate": self._transcode_gate,
            "enabled": settings.enabled,
        }

    def _inspect_format_options(self, ydl: YoutubeDL, raw: Any) -> tuple[MediaFormatOption, ...]:
        if not isinstance(raw, dict):
            return ()
        entries = raw.get("entries")
        contexts = (
            [item for item in entries if isinstance(item, dict)]
            if isinstance(entries, list)
            else [raw]
        )
        if not contexts:
            return ()
        if normalize_source(raw) == "instagram":
            return ()
        if not any(isinstance(context.get("formats"), list) for context in contexts):
            return ()
        max_size = self._settings.media.max_file_size_mb * 1024 * 1024
        source_max_size = self._settings.media.max_source_size_mb * 1024 * 1024
        try:
            mp3_bitrate = int(self._settings.yt_dlp.audio_quality)
        except ValueError:
            mp3_bitrate = 192
        options: list[MediaFormatOption] = []
        for mode in self._settings.media.enabled_modes:
            if mode is DownloadMode.AUDIO_BEST:
                continue
            if mode is DownloadMode.AUDIO_MP3:
                audio = self._inspect_mode(
                    ydl,
                    contexts,
                    mode=mode,
                    selector_expression=self._settings.media.formats.for_mode(mode),
                    max_size=max_size,
                    source_max_size=source_max_size,
                    mp3_bitrate=mp3_bitrate,
                    container=OutputContainer.MP3,
                    policy=ContainerPolicy.GUARANTEED,
                )
                if audio is not None:
                    options.append(audio)
                continue
            for container in (OutputContainer.MP4, OutputContainer.WEBM):
                policy = (
                    ContainerPolicy.NATIVE_ONLY
                    if mode is DownloadMode.BEST_ORIGINAL
                    else ContainerPolicy.GUARANTEED
                )
                native = self._inspect_mode(
                    ydl,
                    contexts,
                    mode=mode,
                    selector_expression=native_container_selector(container),
                    max_size=max_size,
                    source_max_size=source_max_size,
                    mp3_bitrate=mp3_bitrate,
                    container=container,
                    policy=policy,
                    compatible_container=(
                        container if policy is ContainerPolicy.GUARANTEED else None
                    ),
                )
                if native is not None:
                    options.append(native)
                if (
                    container is OutputContainer.MP4
                    and mode is not DownloadMode.BEST_ORIGINAL
                    and self._settings.media.transcode.enabled
                    and self._settings.media.transcode.explicit_mp4_enabled
                ):
                    explicit = self._inspect_mode(
                        ydl,
                        contexts,
                        mode=mode,
                        selector_expression=self._settings.media.formats.for_mode(mode),
                        max_size=max_size,
                        source_max_size=source_max_size,
                        mp3_bitrate=mp3_bitrate,
                        container=OutputContainer.MP4,
                        policy=ContainerPolicy.EXPLICIT_TRANSCODE,
                    )
                    if explicit is not None:
                        options.append(explicit)
        return tuple(options)

    def _inspect_mode(
        self,
        ydl: YoutubeDL,
        contexts: list[dict[str, Any]],
        *,
        mode: DownloadMode,
        selector_expression: str,
        max_size: int,
        source_max_size: int,
        mp3_bitrate: int,
        container: OutputContainer | None = None,
        policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
        compatible_container: OutputContainer | None = None,
    ) -> MediaFormatOption | None:
        per_item: list[MediaFormatOption] = []
        for context in contexts:
            duration_raw = context.get("duration")
            duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
            transfer_limit = source_max_size if video_target_height(mode) is not None else max_size
            selector = ydl.build_format_selector(selector_expression)
            option = inspect_format_option(
                selector,
                context,
                mode=mode,
                max_size_bytes=transfer_limit,
                duration_seconds=duration,
                mp3_bitrate_kbps=mp3_bitrate,
                container=container,
                container_policy=policy,
                compatible_container=compatible_container,
                mp4_native_fallback=self._settings.media.mp4_native_fallback,
            )
            if option is None:
                return None
            per_item.append(option)
        return self._aggregate_format_options(mode, per_item)

    @staticmethod
    def _aggregate_format_options(
        mode: DownloadMode,
        options: list[MediaFormatOption],
    ) -> MediaFormatOption:
        known_sizes = [item.size_bytes for item in options if item.size_bytes is not None]
        if len(known_sizes) != len(options):
            size = None
            confidence = SizeConfidence.UNKNOWN
        else:
            size = sum(known_sizes)
            confidence = (
                SizeConfidence.EXACT
                if all(item.size_confidence is SizeConfidence.EXACT for item in options)
                else SizeConfidence.ESTIMATED
            )
        widths = [item.width for item in options if item.width is not None]
        heights = [item.height for item in options if item.height is not None]
        frame_rates = [item.fps for item in options if item.fps is not None]
        return MediaFormatOption(
            mode=mode,
            container=options[0].container,
            container_policy=options[0].container_policy,
            requires_transcode=any(item.requires_transcode for item in options),
            width=min(widths) if len(widths) == len(options) else None,
            height=min(heights) if len(heights) == len(options) else None,
            fps=min(frame_rates) if len(frame_rates) == len(options) else None,
            is_hdr=any(item.is_hdr for item in options),
            size_bytes=size,
            size_confidence=confidence,
            selection_reason=options[0].selection_reason,
            fallback_reason=options[0].fallback_reason,
            selected_format_ids=tuple(
                dict.fromkeys(
                    format_id for option in options for format_id in option.selected_format_ids
                )
            ),
            video_codec=(
                options[0].video_codec
                if all(item.video_codec == options[0].video_codec for item in options)
                else None
            ),
            audio_codec=(
                options[0].audio_codec
                if all(item.audio_codec == options[0].audio_codec for item in options)
                else None
            ),
            dynamic_range=(
                options[0].dynamic_range
                if all(item.dynamic_range == options[0].dynamic_range for item in options)
                else ("HDR" if any(item.is_hdr for item in options) else "SDR")
            ),
            video_size_bytes=_sum_known([item.video_size_bytes for item in options]),
            audio_size_bytes=_sum_known([item.audio_size_bytes for item in options]),
            quality_score=min(
                (item.quality_score for item in options if item.quality_score is not None),
                default=None,
            ),
        )

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

    def _inline_streamable(self, path: Path) -> bool:
        if self._settings.telegram.upload_as_document or path.suffix.casefold() != ".mp4":
            return False
        return is_inline_video_streamable(path)

    @staticmethod
    def _log_selected_media(
        info: Mapping[str, Any],
        files: list[Path],
        request: DownloadRequest,
        transcode_reason: str | None,
    ) -> None:
        selected_ids = _selected_format_ids(info)
        target_codec = (
            "h264"
            if request.container is OutputContainer.MP4
            and request.container_policy
            in {ContainerPolicy.GUARANTEED, ContainerPolicy.EXPLICIT_TRANSCODE}
            else "vp9"
            if request.container is OutputContainer.WEBM
            and request.container_policy
            in {ContainerPolicy.GUARANTEED, ContainerPolicy.EXPLICIT_TRANSCODE}
            else None
        )
        selection = _selection_log_fields(info, request)
        for path in files:
            source_container: str
            source_video_codec: str | None
            source_audio_codec: str | None
            try:
                probe = probe_video(path)
                source_container = probe.source_container or path.suffix.casefold().lstrip(".")
                source_video_codec = probe.video_codec
                source_audio_codec = probe.audio_codec
            except MediaBotError:
                source_container, source_video_codec, source_audio_codec = _metadata_codecs(
                    info,
                    path,
                )
            logger.info(
                "downloaded_media_selected",
                source_container=source_container,
                source_video_codec=source_video_codec,
                source_audio_codec=source_audio_codec,
                source_file_size=path.stat().st_size,
                selected_format_ids=selected_ids,
                transcode_reason=transcode_reason,
                target_codec=target_codec,
                target_bitrate=None,
                target_crf=None,
                final_file_size=path.stat().st_size if transcode_reason is None else None,
                requested_mode=request.mode.value,
                requested_container=request.container.value if request.container else None,
                requested_container_policy=request.container_policy.value,
                requested_height=video_target_height(request.mode),
                transcode_required=transcode_reason is not None,
                estimated_transcode_seconds=None,
                transcode_timeout_seconds=None,
                **selection,
            )

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


def _selected_format_ids(info: Mapping[str, Any]) -> tuple[str, ...]:
    selected: list[str] = []
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, Mapping) and item.get("format_id") is not None:
                selected.append(str(item["format_id"]))
    elif info.get("format_id") is not None:
        selected.append(str(info["format_id"]))
    entries = info.get("entries")
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, Mapping):
                selected.extend(_selected_format_ids(item))
    return tuple(dict.fromkeys(selected))


def _metadata_codecs(
    info: Mapping[str, Any],
    path: Path,
) -> tuple[str, str | None, str | None]:
    contexts: list[Mapping[str, Any]] = [info]
    entries = info.get("entries")
    if isinstance(entries, list):
        contexts.extend(item for item in entries if isinstance(item, Mapping))
    video_codec = next(
        (str(item["vcodec"]) for item in contexts if item.get("vcodec") not in {None, "none"}),
        None,
    )
    audio_codec = next(
        (str(item["acodec"]) for item in contexts if item.get("acodec") not in {None, "none"}),
        None,
    )
    source_container = next(
        (str(item["ext"]) for item in contexts if item.get("ext") is not None),
        path.suffix.casefold().lstrip("."),
    )
    return source_container, video_codec, audio_codec


def _selection_log_fields(
    info: Mapping[str, Any],
    request: DownloadRequest,
) -> dict[str, object]:
    formats = info.get("formats")
    candidates = (
        [item for item in formats if isinstance(item, Mapping)] if isinstance(formats, list) else []
    )
    requested = info.get("requested_formats")
    selected = (
        [item for item in requested if isinstance(item, Mapping)]
        if isinstance(requested, list)
        else [info]
    )
    selected_video = next(
        (item for item in selected if item.get("vcodec") not in {None, "none"}),
        None,
    )
    selected_audio = next(
        (item for item in selected if item.get("acodec") not in {None, "none"}),
        None,
    )
    target_height = video_target_height(request.mode)
    selected_height = (
        int(selected_video["height"])
        if selected_video is not None and isinstance(selected_video.get("height"), (int, float))
        else None
    )
    selected_fps = (
        float(selected_video["fps"])
        if selected_video is not None and isinstance(selected_video.get("fps"), (int, float))
        else None
    )
    native_h264 = [
        item
        for item in candidates
        if item.get("vcodec") not in {None, "none"}
        and str(item.get("ext") or "").casefold() == "mp4"
        and (
            str(item.get("vcodec") or "").casefold() == "h264"
            or str(item.get("vcodec") or "").casefold().startswith("avc1")
        )
    ]
    exact = [
        item
        for item in native_h264
        if target_height is not None
        and isinstance(item.get("height"), (int, float))
        and int(item["height"]) == target_height
    ]
    lower = [
        item
        for item in native_h264
        if target_height is not None
        and isinstance(item.get("height"), (int, float))
        and int(item["height"]) < target_height
    ]
    selection_reason = (
        "best_original_native"
        if request.mode is DownloadMode.BEST_ORIGINAL
        else "explicit_transcode_selected"
        if request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
        else "webm_native"
        if request.container is OutputContainer.WEBM
        else "native_h264_lower_resolution"
        if request.container is OutputContainer.MP4
        and target_height is not None
        and selected_height is not None
        and selected_height < target_height
        else "native_combined_h264_mp4"
        if request.container is OutputContainer.MP4 and len(selected) == 1
        else "native_h264_exact_resolution"
        if request.container is OutputContainer.MP4
        else None
    )
    video_codecs = {
        str(item.get("vcodec") or "").casefold()
        for item in candidates
        if item.get("vcodec") not in {None, "none"}
    }
    fallback_reason = (
        "exact_h264_not_available"
        if selection_reason == "native_h264_lower_resolution"
        else "only_av1_available"
        if request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
        and video_codecs
        and all(codec == "av1" or codec.startswith("av01") for codec in video_codecs)
        else "only_vp9_available"
        if request.container_policy is ContainerPolicy.EXPLICIT_TRANSCODE
        and video_codecs
        and all(codec == "vp9" or codec.startswith("vp09") for codec in video_codecs)
        else None
    )
    return {
        "candidate_count": len(candidates),
        "native_h264_candidate_count": len(native_h264),
        "exact_height_candidate_count": len(exact),
        "lower_height_candidate_count": len(lower),
        "selected_video_codec": (
            str(selected_video.get("vcodec")) if selected_video is not None else None
        ),
        "selected_audio_codec": (
            str(selected_audio.get("acodec")) if selected_audio is not None else None
        ),
        "selected_height": selected_height,
        "selected_fps": selected_fps,
        "selection_reason": selection_reason,
        "fallback_reason": fallback_reason,
    }


def _order_collection_files(
    files: list[Path],
    info: Mapping[str, Any],
) -> list[Path]:
    entries = info.get("entries")
    if not isinstance(entries, list):
        return files
    order = {
        str(item["id"]): index
        for index, item in enumerate(entries)
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    return sorted(files, key=lambda path: order.get(path.stem, len(order)))
