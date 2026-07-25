from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class VideoProbe:
    duration_seconds: float
    height: int
    has_audio: bool
    video_codec: str = "h264"
    audio_codec: str | None = "aac"


def is_compatible_video(source: Path, container: OutputContainer) -> bool:
    ffprobe = _find_executable("ffprobe")
    if ffprobe is None:
        raise PostProcessingError("ffprobe is required for container validation")
    probe = _probe_video(ffprobe, source)
    if container is OutputContainer.MP4:
        return probe.video_codec == "h264" and (not probe.has_audio or probe.audio_codec == "aac")
    if container is OutputContainer.WEBM:
        return probe.video_codec == "vp9" and (not probe.has_audio or probe.audio_codec == "opus")
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
    )


def transcode_video_to_container(
    source: Path,
    *,
    target_height: int,
    max_size_bytes: int,
    container: OutputContainer,
    is_cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Encode a bounded MP4 or WebM while preserving height and at most 60 FPS."""

    ffmpeg = _find_executable("ffmpeg")
    ffprobe = _find_executable("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise PostProcessingError("ffmpeg and ffprobe are required for bounded video transcoding")
    probe = _probe_video(ffprobe, source)
    output_height = min(target_height, probe.height)
    total_bitrate = int(max_size_bytes * 8 * _SIZE_MARGIN / probe.duration_seconds)
    audio_bitrate = (
        min(_MAX_AUDIO_BITRATE, max(_MIN_AUDIO_BITRATE, total_bitrate // 5))
        if probe.has_audio
        else 0
    )
    video_bitrate = total_bitrate - audio_bitrate - _MUX_OVERHEAD_BITS_PER_SECOND
    if video_bitrate < _MIN_VIDEO_BITRATE:
        raise MediaTooLargeError("Video is too long to transcode safely below the size limit")

    if container not in {OutputContainer.MP4, OutputContainer.WEBM}:
        raise PostProcessingError("Unsupported video output container")
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
        common.extend(["-c:v", "libx264", "-preset", "veryfast"])
    else:
        common.extend(["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "2"])
    for _attempt in range(2):
        output.unlink(missing_ok=True)
        command = [*common, "-b:v", str(video_bitrate)]
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
        _run_process(command, is_cancelled)
        if not output.is_file() or output.stat().st_size <= 0:
            raise PostProcessingError("ffmpeg completed without a transcoded output")
        actual_size = output.stat().st_size
        if actual_size <= max_size_bytes:
            source.unlink(missing_ok=True)
            return output
        video_bitrate = int(video_bitrate * max_size_bytes * 0.9 / actual_size)
        if video_bitrate < _MIN_VIDEO_BITRATE:
            break
    output.unlink(missing_ok=True)
    raise MediaTooLargeError("Transcoded video exceeds configured size limit")


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
    return VideoProbe(
        duration_seconds=duration,
        height=height,
        has_audio=audio is not None,
        video_codec=video_codec,
        audio_codec=audio_codec,
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
