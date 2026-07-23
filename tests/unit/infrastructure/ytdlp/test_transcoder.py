from pathlib import Path

import pytest

from telegram_media_bot.domain.errors import MediaTooLargeError, PostProcessingError
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
    source.write_bytes(b"source")
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
