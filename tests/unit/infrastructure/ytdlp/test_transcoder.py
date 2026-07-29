import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from telegram_media_bot.domain.errors import (
    JobCancelledError,
    MediaTooLargeError,
    PostProcessingError,
    TranscodeRejectedError,
)
from telegram_media_bot.domain.models import NativeVideoCodec, OutputContainer
from telegram_media_bot.infrastructure.ytdlp import transcoder


def test_transcode_replaces_source_below_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"source")
    calls: list[list[str]] = []
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(60.0, 1080, True),
    )

    def fake_run(args: list[str], _is_cancelled: object, **_kwargs: object) -> None:
        calls.append(args)
        Path(args[-1]).write_bytes(b"bounded")

    monkeypatch.setattr(transcoder, "_run_process", fake_run)

    output = transcoder.transcode_video_to_limit(
        source,
        target_height=720,
        max_size_bytes=10 * 1024 * 1024,
    )

    assert len(calls) == 1
    assert not source.exists()
    assert output.read_bytes() == b"bounded"
    assert "scale=-2:720" in calls[0][calls[0].index("-vf") + 1]
    assert calls[0][calls[0].index("-crf") + 1] == "20"
    assert calls[0][calls[0].index("-threads") + 1] == "2"
    assert "-b:v" not in calls[0]


def test_transcode_rejects_duration_that_cannot_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"source")
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(100_000.0, 1080, True),
    )

    with pytest.raises(MediaTooLargeError):
        transcoder.transcode_video_to_limit(
            source,
            target_height=1080,
            max_size_bytes=1024 * 1024,
        )


def test_transcode_retries_once_when_first_output_is_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"x" * 5_000_000)
    calls = 0
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(60.0, 720, True),
    )

    def fake_run(args: list[str], _is_cancelled: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        Path(args[-1]).write_bytes(b"x" * (10_000_001 if calls == 1 else 9_000_000))

    monkeypatch.setattr(transcoder, "_run_process", fake_run)

    output = transcoder.transcode_video_to_limit(
        source,
        target_height=720,
        max_size_bytes=10_000_000,
    )

    assert calls == 2
    assert output.stat().st_size == 9_000_000


def test_transcode_requires_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"source")
    monkeypatch.setattr(transcoder, "_find_executable", lambda _name: None)

    with pytest.raises(PostProcessingError):
        transcoder.transcode_video_to_limit(
            source,
            target_height=1080,
            max_size_bytes=10 * 1024 * 1024,
        )


def test_webm_transcode_uses_vp9_and_opus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands: list[list[str]] = []
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(60.0, 1080, True),
    )

    def fake_run(args: list[str], _is_cancelled: object, **_kwargs: object) -> None:
        commands.append(args)
        Path(args[-1]).write_bytes(b"webm")

    monkeypatch.setattr(transcoder, "_run_process", fake_run)

    output = transcoder.transcode_video_to_container(
        source,
        target_height=1080,
        max_size_bytes=10 * 1024 * 1024,
        container=OutputContainer.WEBM,
    )

    assert output.suffix == ".webm"
    assert "libvpx-vp9" in commands[0]
    assert "libopus" in commands[0]


def test_vp9_mp4_is_native_but_not_inline_streamable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "native.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(
            30.0,
            1920,
            True,
            video_codec="vp9",
            audio_codec="aac",
            source_container="mov,mp4,m4a,3gp,3g2,mj2",
        ),
    )

    assert transcoder.is_native_container_compatible(source, OutputContainer.MP4)
    assert not transcoder.is_inline_video_streamable(source)
    assert not transcoder.is_guaranteed_container_compatible(source, OutputContainer.MP4)


def test_av1_mp4_satisfies_selected_native_plan_but_not_inline_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "native-av1.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(
            30.0,
            2160,
            True,
            video_codec="av1",
            audio_codec="aac",
            source_container="mov,mp4,m4a,3gp,3g2,mj2",
        ),
    )

    assert transcoder.is_native_container_compatible(source, OutputContainer.MP4)
    assert transcoder.is_guaranteed_container_compatible(
        source,
        OutputContainer.MP4,
        native_video_codec=NativeVideoCodec.AV1,
    )
    assert not transcoder.is_inline_video_streamable(source)


def test_transcode_gate_limits_concurrent_encodes() -> None:
    gate = transcoder.TranscodeGate(1)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def work() -> None:
        nonlocal active, maximum
        with gate.slot(None):
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _index: work(), range(4)))

    assert maximum == 1


def test_operator_can_disable_heavy_transcoding(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    with pytest.raises(PostProcessingError, match="disabled"):
        transcoder.transcode_video_to_container(
            source,
            target_height=1080,
            max_size_bytes=10 * 1024 * 1024,
            container=OutputContainer.MP4,
            enabled=False,
        )


def test_timeout_estimate_rejects_long_av1_without_spawning_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    spawned = False
    monkeypatch.setattr(transcoder, "_find_executable", lambda name: name)
    monkeypatch.setattr(transcoder, "_effective_cpu_capacity", lambda: 1.5)
    monkeypatch.setattr(
        transcoder,
        "_probe_video",
        lambda _ffprobe, _source: transcoder.VideoProbe(
            2970.0,
            1080,
            True,
            video_codec="av1",
            audio_codec="aac",
            source_container="mov,mp4",
            width=1920,
            fps=60.0,
        ),
    )

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(transcoder, "_run_process", unexpected_run)

    with pytest.raises(TranscodeRejectedError):
        transcoder.transcode_video_to_container(
            source,
            target_height=1080,
            max_size_bytes=1024 * 1024 * 1024,
            container=OutputContainer.MP4,
            timeout_seconds=1500,
            threads=2,
        )

    assert not spawned
    assert not (tmp_path / "source.telegram.mp4").exists()


def test_ffmpeg_process_is_terminated_when_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 123
            self.stdout: list[str] = []
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    process = FakeProcess()
    terminated: list[int] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate(candidate: FakeProcess) -> None:
        if candidate.poll() is None:
            candidate.returncode = -15
            terminated.append(candidate.pid)

    monkeypatch.setattr(transcoder, "_terminate_process_tree", terminate)
    incomplete = tmp_path / "output.mp4"
    incomplete.write_bytes(b"partial")

    with pytest.raises(JobCancelledError):
        transcoder._run_process(
            ["ffmpeg", "-i", "input", str(incomplete)],
            lambda: True,
            output=incomplete,
            duration_seconds=30,
            threads=2,
            timeout_seconds=60,
            progress_interval_seconds=10,
        )

    assert terminated == [123]
    assert not incomplete.exists()
