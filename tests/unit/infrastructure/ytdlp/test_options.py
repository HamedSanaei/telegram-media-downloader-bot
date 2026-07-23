from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import MediaTooLargeError
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest, JobId
from telegram_media_bot.infrastructure.ytdlp.options import (
    YtDlpOptionsFactory,
    bounded_format_selector,
    final_media_files,
    video_target_height,
)


def make_request(tmp_path: Path, mode: DownloadMode) -> DownloadRequest:
    return DownloadRequest(
        job_id=JobId("job"),
        url="https://example.test/video",
        mode=mode,
        output_directory=tmp_path,
    )


def test_inspect_options_do_not_download(settings: Settings) -> None:
    options = YtDlpOptionsFactory(settings).inspect_options()
    assert options["skip_download"] is True
    assert options["noplaylist"] is True


def test_semantic_mode_maps_to_configured_selector(settings: Settings, tmp_path: Path) -> None:
    factory = YtDlpOptionsFactory(settings)
    options = factory.download_options(make_request(tmp_path, DownloadMode.VIDEO_720))
    assert options["format"] == settings.media.formats.video_720
    assert options["outtmpl"]["default"] == "%(id)s.%(ext)s"
    assert options["paths"]["home"] == str(tmp_path)
    assert "max_filesize" not in options
    assert "exec" not in options
    assert "external_downloader" not in options


def test_audio_mp3_adds_audio_postprocessor(settings: Settings, tmp_path: Path) -> None:
    options = YtDlpOptionsFactory(settings).download_options(
        make_request(tmp_path, DownloadMode.AUDIO_MP3)
    )
    assert options["postprocessors"][0]["key"] == "FFmpegExtractAudio"


def test_optional_proxy_cookie_and_user_agent_are_applied(
    settings: Settings, tmp_path: Path
) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = cookie
    raw["yt_dlp"]["proxy"] = "socks5://localhost:1080"
    raw["yt_dlp"]["user_agent"] = "test-agent"
    configured = type(settings).model_validate(raw)
    options = YtDlpOptionsFactory(configured).inspect_options()
    assert options["cookiefile"] == str(cookie)
    assert options["proxy"] == "socks5://localhost:1080"
    assert options["user_agent"] == "test-agent"


def test_final_media_files_ignores_partial_files(tmp_path: Path) -> None:
    (tmp_path / "video.mp4").write_bytes(b"ok")
    (tmp_path / "video.mp4.part").write_bytes(b"partial")
    (tmp_path / ".tmp").mkdir()
    assert final_media_files(tmp_path) == [tmp_path / "video.mp4"]


def test_bounded_selector_prefers_complete_video_with_lower_audio() -> None:
    formats = [
        _format("audio-low", size=10, audio=True),
        _format("audio-high", size=30, audio=True),
        _format("video-low", size=20, video=True),
        _format("video-high", size=40, video=True),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=50,
    )

    selected = list(selector({"formats": formats}))

    assert [item["format_id"] for item in selected[0]["requested_formats"]] == [
        "video-high",
        "audio-low",
    ]


def test_bounded_selector_rejects_when_no_complete_selection_fits() -> None:
    formats = [
        _format("audio", size=30, audio=True),
        _format("video", size=40, video=True),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_720,
        max_size_bytes=50,
    )

    with pytest.raises(MediaTooLargeError):
        list(selector({"formats": formats}))


def test_best_mode_caps_source_selection_at_1080p() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video-1080", size=40, video=True, height=1080),
        _format("video-1440", size=50, video=True, height=1440),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.BEST,
        max_size_bytes=100,
    )

    selected = list(selector({"formats": formats}))

    assert selected[0]["requested_formats"][0]["format_id"] == "video-1080"
    assert video_target_height(DownloadMode.BEST) == 1080
    assert video_target_height(DownloadMode.AUDIO_MP3) is None


def test_bounded_selector_prefers_sdr_at_same_resolution() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video-sdr", size=40, video=True, height=720),
        {
            **_format("video-hdr", size=50, video=True, height=720),
            "dynamic_range": "HDR10",
        },
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_720,
        max_size_bytes=100,
    )

    selected = list(selector({"formats": formats}))

    assert selected[0]["requested_formats"][0]["format_id"] == "video-sdr"


def _format(
    format_id: str,
    *,
    size: int,
    video: bool = False,
    audio: bool = False,
    height: int | None = None,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "filesize": size,
        "vcodec": "av1" if video else "none",
        "acodec": "opus" if audio else "none",
        "height": height,
    }


def _best_video_audio_selector(context: dict[str, Any]) -> list[dict[str, Any]]:
    formats = context["formats"]
    videos = [item for item in formats if item["vcodec"] != "none"]
    audios = [item for item in formats if item["acodec"] != "none"]
    if not videos or not audios:
        return []
    video = videos[-1]
    audio = audios[-1]
    return [
        {
            "format_id": f"{video['format_id']}+{audio['format_id']}",
            "requested_formats": [video, audio],
            "vcodec": video["vcodec"],
            "acodec": audio["acodec"],
        }
    ]
