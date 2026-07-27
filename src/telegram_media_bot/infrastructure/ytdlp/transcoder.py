from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import structlog

from telegram_media_bot.domain.errors import (
    JobCancelledError,
    MediaTooLargeError,
    PostProcessingError,
)
from telegram_media_bot.domain.models import OutputContainer

_SIZE_MARGIN = 0.88
_MUX_OVERHEAD_BITS_PER_SECOND = 16_000
_MIN_AUDIO_BITRATE = 48_000
_MAX_AUDIO_BITRATE = 96_000
_MIN_VIDEO_BITRATE = 64_000
_MP4_CRF = 20
_WEBM_CRF = 30
_MAX_QUALITY_PASS_GROWTH = 2.0
_QUALITY_PASS_GROWTH_ALLOWANCE = 1024 * 1024

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VideoProbe:
    duration_seconds: float
    height: int
    has_audio: bool
    video_codec: str = "h264"
    audio_codec: str | None = "aac"
    source_container: str = ""


def probe_video(source: Path) -> VideoProbe:
    ffprobe = _find_executable("ffprobe")
    if ffprobe is None:
        raise PostProcessingError("ffprobe is required for container validation")
    return _probe_video(ffprobe, source)


def is_native_container_compatible(source: Path, container: OutputContainer) -> bool:
    """Return whether the streams can remain untouched in the requested container."""
    probe = probe_video(source)
    if container is OutputContainer.MP4:
        return (
            (source.suffix.casefold() == ".mp4" or "mp4" in probe.source_container.split(","))
            and probe.video_codec in {"h264", "hevc", "vp9", "av1"}
            and (not probe.has_audio or probe.audio_codec in {"aac", "mp3", "ac3", "eac3", "alac"})
        )
    if container is OutputContainer.WEBM:
        return (
            (source.suffix.casefold() == ".webm" or "webm" in probe.source_container.split(","))
            and probe.video_codec in {"vp8", "vp9", "av1"}
            and (not probe.has_audio or probe.audio_codec in {"opus", "vorbis"})
        )
    return False


def is_inline_video_streamable(source: Path) -> bool:
    """Return whether Telegram send_video receives its preferred H.264/AAC MP4 shape."""
    probe = probe_video(source)
    is_mp4 = source.suffix.casefold() == ".mp4" or "mp4" in probe.source_container.split(",")
    return (
        is_mp4
        and probe.video_codec == "h264"
        and (not probe.has_audio or probe.audio_codec == "aac")
    )


def is_guaranteed_container_compatible(source: Path, container: OutputContainer) -> bool:
    """Return whether a semantic guaranteed-output codec contract is already met."""
    probe = probe_video(source)
    if container is OutputContainer.MP4:
        return (
            (source.suffix.casefold() == ".mp4" or "mp4" in probe.source_container.split(","))
            and probe.video_codec == "h264"
            and (not probe.has_audio or probe.audio_codec == "aac")
        )
    if container is OutputContainer.WEBM:
        return (
            (source.suffix.casefold() == ".webm" or "webm" in probe.source_container.split(","))
            and probe.video_codec == "vp9"
            and (not probe.has_audio or probe.audio_codec == "opus")
        )
    return False


def transcode_video_to_limit(
    source: Path,
    *,
    target_height: int,
    max_size_bytes: int,
    is_cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Encode H.264 at the selected resolution below the delivery ceiling."""
    return transcode_video_to_container(
        source,
        target_height=target_height,
        max_size_bytes=max_size_bytes,
        container=OutputContainer.MP4,
        is_cancelled=is_cancelled,
        reason="size_limit",
    )


def transcode_video_to_container(
    source: Path,
    *,
    target_height: int,
    max_size_bytes: int,
    container: OutputContainer,
    is_cancelled: Callable[[], bool] | None = None,
    reason: str = "container_contract",
) -> Path:
    """Encode for quality first, then constrain bitrate only when a ceiling requires it."""

    ffmpeg = _find_executable("ffmpeg")
    ffprobe = _find_executable("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise PostProcessingError("ffmpeg and ffprobe are required for bounded video transcoding")
    probe = _probe_video(ffprobe, source)
    output_height = min(target_height, probe.height)
    if container not in {OutputContainer.MP4, OutputContainer.WEBM}:
        raise PostProcessingError("Unsupported video output container")
    absolute_ceiling_bitrate = int(max_size_bytes * 8 * _SIZE_MARGIN / probe.duration_seconds)
    minimum_required_bitrate = _MIN_VIDEO_BITRATE + _MUX_OVERHEAD_BITS_PER_SECOND
    if probe.has_audio:
        minimum_required_bitrate += _MIN_AUDIO_BITRATE
    if absolute_ceiling_bitrate < minimum_required_bitrate:
        raise MediaTooLargeError("Video is too long to transcode safely below the size limit")
    source_size = source.stat().st_size
    output = source.with_name(f"{source.stem}.telegram.{container.value}")
    video_filter = (
        f"scale=-2:{output_height}:flags=lanczos,fps=fps='min(source_fps,60)',format=yuv420p"
    )
    common = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        video_filter,
    ]
    if container is OutputContainer.MP4:
        target_codec = "h264"
        target_crf = _MP4_CRF
        common.extend(["-c:v", "libx264", "-preset", "medium"])
    else:
        target_codec = "vp9"
        target_crf = _WEBM_CRF
        common.extend(["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "2"])
    logger.info(
        "video_transcode_started",
        source_container=probe.source_container or source.suffix.casefold().lstrip("."),
        source_video_codec=probe.video_codec,
        source_audio_codec=probe.audio_codec,
        source_file_size=source_size,
        transcode_reason=reason,
        target_codec=target_codec,
        target_crf=target_crf,
        target_bitrate=None,
    )
    quality_command = [*common, "-crf", str(target_crf)]
    if container is OutputContainer.WEBM:
        quality_command.extend(["-b:v", "0"])
    _append_audio_and_output(quality_command, probe, container, output)
    output.unlink(missing_ok=True)
    _run_process(quality_command, is_cancelled)
    quality_size = _validated_output_size(output)
    growth_ceiling = max(
        int(source_size * _MAX_QUALITY_PASS_GROWTH),
        source_size + _QUALITY_PASS_GROWTH_ALLOWANCE,
    )
    fallback_ceiling = min(max_size_bytes, growth_ceiling)
    if quality_size <= fallback_ceiling:
        source.unlink(missing_ok=True)
        logger.info(
            "video_transcode_completed",
            transcode_reason=reason,
            target_codec=target_codec,
            target_crf=target_crf,
            target_bitrate=None,
            final_file_size=quality_size,
        )
        return output

    total_bitrate = int(fallback_ceiling * 8 * _SIZE_MARGIN / probe.duration_seconds)
    audio_bitrate = (
        min(_MAX_AUDIO_BITRATE, max(_MIN_AUDIO_BITRATE, total_bitrate // 5))
        if probe.has_audio
        else 0
    )
    video_bitrate = total_bitrate - audio_bitrate - _MUX_OVERHEAD_BITS_PER_SECOND
    if video_bitrate < _MIN_VIDEO_BITRATE:
        output.unlink(missing_ok=True)
        raise MediaTooLargeError("Video is too long to transcode safely below the size limit")
    for _attempt in range(2):
        output.unlink(missing_ok=True)
        command = [*common, "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate)]
        _append_audio_and_output(
            command,
            probe,
            container,
            output,
            audio_bitrate=audio_bitrate,
        )
        logger.info(
            "video_transcode_size_fallback",
            transcode_reason=reason,
            target_codec=target_codec,
            target_crf=None,
            target_bitrate=video_bitrate,
            size_ceiling=fallback_ceiling,
        )
        _run_process(command, is_cancelled)
        actual_size = _validated_output_size(output)
        if actual_size <= fallback_ceiling:
            source.unlink(missing_ok=True)
            logger.info(
                "video_transcode_completed",
                transcode_reason=reason,
                target_codec=target_codec,
                target_crf=None,
                target_bitrate=video_bitrate,
                final_file_size=actual_size,
            )
            return output
        video_bitrate = int(video_bitrate * fallback_ceiling * 0.9 / actual_size)
        if video_bitrate < _MIN_VIDEO_BITRATE:
            break
    output.unlink(missing_ok=True)
    raise MediaTooLargeError("Transcoded video exceeds configured size limit")


def _append_audio_and_output(
    command: list[str],
    probe: VideoProbe,
    container: OutputContainer,
    output: Path,
    *,
    audio_bitrate: int = _MAX_AUDIO_BITRATE,
) -> None:
    if probe.has_audio:
        command.extend(
            [
                "-map",
                "0:a:0?",
                "-c:a",
                "aac" if container is OutputContainer.MP4 else "libopus",
                "-b:a",
                str(audio_bitrate),
            ]
        )
    if container is OutputContainer.MP4:
        command.extend(["-movflags", "+faststart"])
    command.append(str(output))


def _validated_output_size(output: Path) -> int:
    if not output.is_file() or output.stat().st_size <= 0:
        raise PostProcessingError("ffmpeg completed without a transcoded output")
    return output.stat().st_size


def _probe_video(ffprobe: str, source: Path) -> VideoProbe:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        raw = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise PostProcessingError("Unable to inspect downloaded video") from exc
    if not isinstance(raw, Mapping):
        raise PostProcessingError("ffprobe returned invalid video metadata")
    streams = raw.get("streams")
    stream_items = streams if isinstance(streams, list) else []
    video = next(
        (
            item
            for item in stream_items
            if isinstance(item, Mapping) and item.get("codec_type") == "video"
        ),
        None,
    )
    format_info = raw.get("format")
    duration_raw = format_info.get("duration") if isinstance(format_info, Mapping) else None
    height_raw = video.get("height") if isinstance(video, Mapping) else None
    if not isinstance(duration_raw, (str, int, float)) or not isinstance(
        height_raw, (str, int, float)
    ):
        raise PostProcessingError("Video duration or height is unavailable")
    try:
        duration = float(duration_raw)
        height = int(height_raw)
    except (TypeError, ValueError) as exc:
        raise PostProcessingError("Video duration or height is unavailable") from exc
    if duration <= 0 or height <= 0:
        raise PostProcessingError("Video duration or height is invalid")
    audio = next(
        (
            item
            for item in stream_items
            if isinstance(item, Mapping) and item.get("codec_type") == "audio"
        ),
        None,
    )
    video_codec = str(video.get("codec_name") or "") if isinstance(video, Mapping) else ""
    audio_codec = str(audio.get("codec_name") or "") if isinstance(audio, Mapping) else None
    source_container = (
        str(format_info.get("format_name") or "") if isinstance(format_info, Mapping) else ""
    )
    return VideoProbe(
        duration_seconds=duration,
        height=height,
        has_audio=audio is not None,
        video_codec=video_codec,
        audio_codec=audio_codec,
        source_container=source_container,
    )


def _run_process(args: list[str], is_cancelled: Callable[[], bool] | None) -> None:
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise PostProcessingError("Unable to start ffmpeg") from exc
    while process.poll() is None:
        if is_cancelled is not None and is_cancelled():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise JobCancelledError("Video transcoding was cancelled")
        time.sleep(0.2)
    if process.returncode != 0:
        raise PostProcessingError("ffmpeg video transcoding failed")


def _find_executable(name: str) -> str | None:
    return shutil.which(name)
