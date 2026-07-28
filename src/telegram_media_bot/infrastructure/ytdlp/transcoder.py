from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import structlog

from telegram_media_bot.domain.errors import (
    JobCancelledError,
    MediaTooLargeError,
    PostProcessingError,
    TranscodeRejectedError,
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
    width: int = 1920
    fps: float = 30.0


class TranscodeGate:
    """Process-local bound for CPU-heavy encodes in the worker."""

    def __init__(self, maximum: int) -> None:
        self._semaphore = threading.BoundedSemaphore(maximum)

    @contextmanager
    def slot(self, is_cancelled: Callable[[], bool] | None) -> Iterator[None]:
        while not self._semaphore.acquire(timeout=0.2):
            if is_cancelled is not None and is_cancelled():
                raise JobCancelledError("Video transcoding was cancelled while waiting")
        try:
            yield
        finally:
            self._semaphore.release()


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
    threads: int = 2,
    timeout_seconds: int = 1500,
    progress_interval_seconds: int = 10,
    gate: TranscodeGate | None = None,
    enabled: bool = True,
) -> Path:
    """Encode H.264 at the selected resolution below the delivery ceiling."""
    return transcode_video_to_container(
        source,
        target_height=target_height,
        max_size_bytes=max_size_bytes,
        container=OutputContainer.MP4,
        is_cancelled=is_cancelled,
        reason="size_limit",
        threads=threads,
        timeout_seconds=timeout_seconds,
        progress_interval_seconds=progress_interval_seconds,
        gate=gate,
        enabled=enabled,
    )


def transcode_video_to_container(
    source: Path,
    *,
    target_height: int,
    max_size_bytes: int,
    container: OutputContainer,
    is_cancelled: Callable[[], bool] | None = None,
    reason: str = "container_contract",
    threads: int = 2,
    timeout_seconds: int = 1500,
    progress_interval_seconds: int = 10,
    gate: TranscodeGate | None = None,
    enabled: bool = True,
) -> Path:
    """Encode for quality first, then constrain bitrate only when a ceiling requires it."""

    if not enabled:
        raise PostProcessingError("Video transcoding is disabled by operator policy")
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
    estimated_seconds = estimate_transcode_seconds(probe, threads=threads)
    if estimated_seconds > timeout_seconds:
        output.unlink(missing_ok=True)
        logger.warning(
            "transcode_rejected_timeout_estimate",
            source_container=probe.source_container or source.suffix.casefold().lstrip("."),
            source_video_codec=probe.video_codec,
            source_audio_codec=probe.audio_codec,
            source_file_size=source_size,
            target_codec="h264" if container is OutputContainer.MP4 else "vp9",
            target_bitrate=None,
            target_crf=_MP4_CRF if container is OutputContainer.MP4 else _WEBM_CRF,
            estimated_transcode_seconds=round(estimated_seconds),
            transcode_timeout_seconds=timeout_seconds,
            fallback_reason="transcode_timeout_estimate_exceeded",
        )
        raise TranscodeRejectedError(
            "Estimated codec conversion time exceeds the configured transcode timeout"
        )
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
        "-filter_threads",
        str(threads),
        "-vf",
        video_filter,
    ]
    if container is OutputContainer.MP4:
        target_codec = "h264"
        target_crf = _MP4_CRF
        common.extend(["-c:v", "libx264", "-preset", "medium", "-threads", str(threads)])
    else:
        target_codec = "vp9"
        target_crf = _WEBM_CRF
        common.extend(
            [
                "-c:v",
                "libvpx-vp9",
                "-deadline",
                "good",
                "-cpu-used",
                "2",
                "-threads",
                str(threads),
            ]
        )
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
        transcode_threads=threads,
        estimated_transcode_seconds=round(estimated_seconds),
        transcode_timeout_seconds=timeout_seconds,
    )
    quality_command = [*common, "-crf", str(target_crf)]
    if container is OutputContainer.WEBM:
        quality_command.extend(["-b:v", "0"])
    _append_audio_and_output(quality_command, probe, container, output)
    output.unlink(missing_ok=True)
    with gate.slot(is_cancelled) if gate is not None else _unbounded_slot():
        _run_process(
            quality_command,
            is_cancelled,
            output=output,
            duration_seconds=probe.duration_seconds,
            threads=threads,
            timeout_seconds=timeout_seconds,
            progress_interval_seconds=progress_interval_seconds,
        )
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
        with gate.slot(is_cancelled) if gate is not None else _unbounded_slot():
            _run_process(
                command,
                is_cancelled,
                output=output,
                duration_seconds=probe.duration_seconds,
                threads=threads,
                timeout_seconds=timeout_seconds,
                progress_interval_seconds=progress_interval_seconds,
            )
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
    width_raw = video.get("width") if isinstance(video, Mapping) else None
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
    width = (
        int(width_raw)
        if isinstance(width_raw, (str, int, float)) and int(float(width_raw)) > 0
        else max(2, round(height * 16 / 9))
    )
    fps_raw = video.get("avg_frame_rate") if isinstance(video, Mapping) else None
    fps = _parse_frame_rate(fps_raw)
    return VideoProbe(
        duration_seconds=duration,
        height=height,
        has_audio=audio is not None,
        width=width,
        fps=fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        source_container=source_container,
    )


@contextmanager
def _unbounded_slot() -> Iterator[None]:
    yield


def _run_process(
    args: list[str],
    is_cancelled: Callable[[], bool] | None,
    *,
    output: Path,
    duration_seconds: float,
    threads: int,
    timeout_seconds: int,
    progress_interval_seconds: int,
) -> None:
    progress_args = [*args[:-1], "-progress", "pipe:1", "-nostats", args[-1]]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            progress_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        raise PostProcessingError("Unable to start ffmpeg") from exc
    started = time.monotonic()
    progress_values: queue.SimpleQueue[tuple[float | None, str | None]] = queue.SimpleQueue()
    reader = threading.Thread(
        target=_read_ffmpeg_progress,
        args=(process, progress_values),
        name=f"ffmpeg-progress-{process.pid}",
        daemon=True,
    )
    reader.start()
    next_log = started
    processed: float | None = None
    speed: str | None = None
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - started
            latest_progress = _latest_progress(progress_values)
            if latest_progress is not None:
                processed, speed = latest_progress
            if time.monotonic() >= next_log:
                percent = (
                    min(100.0, max(0.0, processed * 100 / duration_seconds))
                    if processed is not None
                    else None
                )
                logger.info(
                    "video_transcode_progress",
                    ffmpeg_pid=process.pid,
                    ffmpeg_exit_code=None,
                    transcode_threads=threads,
                    transcode_elapsed=round(elapsed, 1),
                    processed_duration=processed,
                    speed=speed,
                    percent=round(percent, 1) if percent is not None else None,
                    current_output_size=output.stat().st_size if output.exists() else 0,
                )
                next_log = time.monotonic() + progress_interval_seconds
            if is_cancelled is not None and is_cancelled():
                _terminate_process_tree(process)
                reader.join(timeout=1)
                logger.info(
                    "video_transcode_stopped",
                    ffmpeg_pid=process.pid,
                    ffmpeg_exit_code=process.returncode,
                    transcode_threads=threads,
                    transcode_elapsed=round(time.monotonic() - started, 1),
                    cancel_source="user",
                )
                raise JobCancelledError("Video transcoding was cancelled")
            if elapsed >= timeout_seconds:
                _terminate_process_tree(process)
                reader.join(timeout=1)
                logger.warning(
                    "video_transcode_stopped",
                    ffmpeg_pid=process.pid,
                    ffmpeg_exit_code=process.returncode,
                    transcode_threads=threads,
                    transcode_elapsed=round(time.monotonic() - started, 1),
                    cancel_source="timeout",
                )
                raise PostProcessingError("ffmpeg video transcoding timed out")
            time.sleep(0.2)
    except BaseException:
        _terminate_process_tree(process)
        reader.join(timeout=1)
        output.unlink(missing_ok=True)
        raise
    reader.join(timeout=1)
    logger.info(
        "video_transcode_process_exited",
        ffmpeg_pid=process.pid,
        ffmpeg_exit_code=process.returncode,
        transcode_threads=threads,
        transcode_elapsed=round(time.monotonic() - started, 1),
    )
    if process.returncode != 0:
        raise PostProcessingError("ffmpeg video transcoding failed")


def _read_ffmpeg_progress(
    process: subprocess.Popen[str],
    values: queue.SimpleQueue[tuple[float | None, str | None]],
) -> None:
    if process.stdout is None:
        return
    processed: float | None = None
    speed: str | None = None
    for raw_line in process.stdout:
        key, separator, value = raw_line.strip().partition("=")
        if not separator:
            continue
        if key in {"out_time_us", "out_time_ms"}:
            with suppress(ValueError):
                processed = float(value) / 1_000_000
        elif key == "speed":
            speed = value
        elif key == "progress":
            values.put((processed, speed))


def _latest_progress(
    values: queue.SimpleQueue[tuple[float | None, str | None]],
) -> tuple[float | None, str | None] | None:
    latest: tuple[float | None, str | None] | None = None
    while not values.empty():
        latest = values.get()
    return latest


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            kill_process_group = getattr(os, "killpg")  # noqa: B009
            kill_process_group(process.pid, signal.SIGTERM)
        else:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        process.wait(timeout=5)
    except OSError, subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                kill_process_group = getattr(os, "killpg")  # noqa: B009
                kill_process_group(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                if process.poll() is None:
                    process.kill()
        except OSError, subprocess.TimeoutExpired:
            pass
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _find_executable(name: str) -> str | None:
    return shutil.which(name)


def estimate_transcode_seconds(probe: VideoProbe, *, threads: int) -> float:
    """Conservatively estimate a quality encode before reserving the FFmpeg gate."""
    pixels = max(1, probe.width * probe.height)
    pixel_factor = max(0.25, pixels / (1920 * 1080))
    codec = probe.video_codec.casefold()
    decode_factor = (
        1.5
        if codec == "av1" or codec.startswith("av01")
        else 1.3
        if codec == "vp9" or codec.startswith("vp09")
        else 1.0
    )
    effective_cpus = max(0.25, min(float(threads), _effective_cpu_capacity()))
    estimated_fps = 12.0 * effective_cpus / pixel_factor / decode_factor
    return probe.duration_seconds * max(1.0, probe.fps) / estimated_fps * 1.25


def _effective_cpu_capacity() -> float:
    host_cpus = float(os.cpu_count() or 1)
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota_text, period_text = cpu_max.read_text(encoding="utf-8").split()[:2]
        if quota_text != "max":
            quota = int(quota_text)
            period = int(period_text)
            if quota > 0 and period > 0:
                return min(host_cpus, quota / period)
    except OSError, ValueError:
        pass
    return host_cpus


def _parse_frame_rate(value: object) -> float:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        numerator, separator, denominator = value.partition("/")
        try:
            parsed = float(numerator) / float(denominator) if separator else float(value)
        except ValueError, ZeroDivisionError:
            return 30.0
        if parsed > 0:
            return parsed
    return 30.0
