from pathlib import Path

import pytest

from telegram_media_bot.domain.errors import MediaTooLargeError, PostProcessingError
from telegram_media_bot.domain.models import OutputContainer
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

    def fake_run(args: list[str], _is_cancelled: object) -> None:
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

    def fake_run(args: list[str], _is_cancelled: object) -> None:
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

    def fake_run(args: list[str], _is_cancelled: object) -> None:
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
