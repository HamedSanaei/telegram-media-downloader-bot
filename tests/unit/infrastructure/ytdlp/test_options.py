from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import MediaTooLargeError, MediaUnavailableError
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    JobId,
    Mp4NativeFallback,
    OutputContainer,
)
from telegram_media_bot.infrastructure.ytdlp.options import (
    YtDlpOptionsFactory,
    bounded_format_selector,
    final_media_files,
    inspect_format_option,
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
    assert options["noplaylist"] is False


def test_single_youtube_video_inspection_forces_noplaylist(settings: Settings) -> None:
    options = YtDlpOptionsFactory(settings).inspect_options(single_video=True)

    assert options["noplaylist"] is True


def test_single_youtube_video_download_forces_noplaylist(
    settings: Settings,
    tmp_path: Path,
) -> None:
    request = DownloadRequest(
        job_id=JobId("youtube-single"),
        url="https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8",
        mode=DownloadMode.VIDEO_1080,
        output_directory=tmp_path,
    )

    options = YtDlpOptionsFactory(settings).download_options(request)

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


@pytest.mark.parametrize("mode", [DownloadMode.YOUTUBE_THUMBNAIL, DownloadMode.SOUNDCLOUD_ARTWORK])
def test_artwork_mode_writes_only_highest_thumbnail(
    settings: Settings, tmp_path: Path, mode: DownloadMode
) -> None:
    options = YtDlpOptionsFactory(settings).download_options(make_request(tmp_path, mode))

    assert options["skip_download"] is True
    assert options["writethumbnail"] is True
    assert options["write_all_thumbnails"] is False
    assert options["postprocessors"] == []


def test_optional_proxy_cookie_and_user_agent_are_applied(
    settings: Settings, tmp_path: Path
) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = cookie
    raw["yt_dlp"]["proxy_enabled"] = None
    raw["yt_dlp"]["proxy"] = "socks5://localhost:1080"
    raw["yt_dlp"]["user_agent"] = "test-agent"
    configured = type(settings).model_validate(raw)
    options = YtDlpOptionsFactory(configured).inspect_options()
    assert options["cookiefile"] == str(cookie)
    assert options["proxy"] == "socks5://localhost:1080"
    assert options["user_agent"] == "test-agent"


def test_explicit_proxy_disable_wins_over_configured_secret(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["yt_dlp"]["proxy_enabled"] = False
    raw["yt_dlp"]["proxy"] = "http://user:pa" + "ssword@127.0.0.1:8080"
    configured = type(settings).model_validate(raw)

    options = YtDlpOptionsFactory(configured).inspect_options()

    assert "proxy" not in options
    assert "password" not in repr(configured.yt_dlp.proxy)


@pytest.mark.parametrize("container", [OutputContainer.MP4, OutputContainer.WEBM])
def test_guaranteed_container_uses_only_native_compatible_selector(
    settings: Settings,
    tmp_path: Path,
    container: OutputContainer,
) -> None:
    request = DownloadRequest(
        job_id=JobId("container"),
        url="https://example.test/video",
        mode=DownloadMode.VIDEO_1080,
        output_directory=tmp_path,
        container=container,
        container_policy=ContainerPolicy.GUARANTEED,
    )

    options = YtDlpOptionsFactory(settings).download_options(request)

    assert options["format"] == (
        "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=mp4]/b[ext=mp4]"
        if container is OutputContainer.MP4
        else "bv*[ext=webm]+ba[ext=webm]/b[ext=webm]"
    )
    assert settings.media.formats.video_1080 not in options["format"]
    assert options["merge_output_format"] == container.value
    assert "libx264" not in repr(options)
    merger_args = options["postprocessor_args"]["merger+ffmpeg_o"]
    assert merger_args[:4] == ["-c:v", "copy", "-c:a", "copy"]
    if container is OutputContainer.MP4:
        assert merger_args[-2:] == ["-movflags", "+faststart"]


def test_best_original_normalizes_guaranteed_policy_to_native_only(
    settings: Settings,
    tmp_path: Path,
) -> None:
    request = DownloadRequest(
        job_id=JobId("original"),
        url="https://example.test/video",
        mode=DownloadMode.BEST_ORIGINAL,
        output_directory=tmp_path,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
    )

    options = YtDlpOptionsFactory(settings).download_options(request)

    assert request.container_policy is ContainerPolicy.NATIVE_ONLY
    assert options["format"] == ("bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=mp4]/b[ext=mp4]")
    assert settings.media.formats.best_original not in options["format"]


def test_best_original_without_forced_container_preserves_source_selector(
    settings: Settings,
    tmp_path: Path,
) -> None:
    request = DownloadRequest(
        job_id=JobId("native-original"),
        url="https://example.test/video",
        mode=DownloadMode.BEST_ORIGINAL,
        output_directory=tmp_path,
    )

    options = YtDlpOptionsFactory(settings).download_options(request)

    assert options["format"] == settings.media.formats.best_original
    assert "merge_output_format" not in options


def test_final_media_files_ignores_partial_files(tmp_path: Path) -> None:
    (tmp_path / "video.mp4").write_bytes(b"ok")
    (tmp_path / "video.mp4.part").write_bytes(b"partial")
    (tmp_path / ".tmp").mkdir()
    assert final_media_files(tmp_path) == [tmp_path / "video.mp4"]


def test_bounded_selector_prefers_complete_video_with_lower_audio() -> None:
    formats = [
        _format("audio-low", size=10, audio=True),
        _format("audio-high", size=30, audio=True),
        _format("video-low", size=20, video=True, height=1080),
        _format("video-high", size=40, video=True, height=1080),
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
        _format("video", size=40, video=True, height=720),
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


def test_high_resolution_modes_and_original_are_distinct() -> None:
    assert video_target_height(DownloadMode.VIDEO_1440) == 1440
    assert video_target_height(DownloadMode.VIDEO_2160) == 2160
    assert video_target_height(DownloadMode.BEST_ORIGINAL) is None


def test_best_original_keeps_hdr_highest_resolution() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video-sdr", size=40, video=True, height=1440),
        {
            **_format("video-hdr", size=50, video=True, height=2160),
            "dynamic_range": "HDR10",
        },
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.BEST_ORIGINAL,
        max_size_bytes=100,
    )

    selected = list(selector({"formats": formats}))

    assert selected[0]["requested_formats"][0]["format_id"] == "video-hdr"


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


def test_fixed_mode_never_falls_back_to_lower_height() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video-1080", size=40, video=True, height=1080),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_2160,
        max_size_bytes=100,
    )

    with pytest.raises(MediaTooLargeError):
        list(selector({"formats": formats}))


def test_mp4_prefers_h264_over_higher_bitrate_av1_at_same_height() -> None:
    formats = [
        _native_format("audio", ext="m4a", acodec="mp4a.40.2", size=10),
        _native_format("h264", ext="mp4", vcodec="avc1.640028", height=1080, size=40),
        _native_format("av1", ext="mp4", vcodec="av01.0.08M.08", height=1080, size=90),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=200,
        compatible_container=OutputContainer.MP4,
        mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
    )

    selected = list(selector({"formats": formats}))

    assert [item["format_id"] for item in selected[0]["requested_formats"]] == [
        "h264",
        "audio",
    ]


def test_mp4_falls_back_to_lower_native_h264_resolution() -> None:
    formats = [
        _native_format("audio", ext="m4a", acodec="aac", size=10),
        _native_format("h264-720", ext="mp4", vcodec="h264", height=720, size=40),
        _native_format("av1-1080", ext="mp4", vcodec="av1", height=1080, size=90),
    ]
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=200,
        compatible_container=OutputContainer.MP4,
        mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
    )

    selected = list(selector({"formats": formats}))

    assert selected[0]["requested_formats"][0]["format_id"] == "h264-720"


def test_mp4_av1_is_not_native_compatible() -> None:
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=200,
        compatible_container=OutputContainer.MP4,
        mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
    )

    with pytest.raises(MediaUnavailableError):
        list(
            selector(
                {
                    "formats": [
                        _native_format("audio", ext="m4a", acodec="aac", size=10),
                        _native_format(
                            "av1", ext="mp4", vcodec="av01.0.08M.08", height=1080, size=90
                        ),
                    ]
                }
            )
        )


def test_mp4_fail_fallback_does_not_select_lower_h264() -> None:
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=200,
        compatible_container=OutputContainer.MP4,
        mp4_native_fallback=Mp4NativeFallback.FAIL,
    )

    with pytest.raises(MediaUnavailableError):
        list(
            selector(
                {
                    "formats": [
                        _native_format("audio", ext="m4a", acodec="aac", size=10),
                        _native_format("h264-720", ext="mp4", vcodec="h264", height=720, size=40),
                    ]
                }
            )
        )


def test_webm_vp9_opus_remains_native() -> None:
    selector = bounded_format_selector(
        _best_video_audio_selector,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=200,
        compatible_container=OutputContainer.WEBM,
    )
    selected = list(
        selector(
            {
                "formats": [
                    _native_format("opus", ext="webm", acodec="opus", size=10),
                    _native_format("vp9", ext="webm", vcodec="vp09.00.40.08", height=1080, size=90),
                ]
            }
        )
    )

    assert [item["format_id"] for item in selected[0]["requested_formats"]] == ["vp9", "opus"]


def test_bounded_selector_supplies_complete_ytdlp_selector_context() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video", size=40, video=True, height=720),
    ]

    def context_aware_selector(context: dict[str, Any]) -> list[dict[str, Any]]:
        assert context["has_merged_format"] is False
        assert context["incomplete_formats"] is False
        return _best_video_audio_selector(context)

    selector = bounded_format_selector(
        context_aware_selector,
        mode=DownloadMode.VIDEO_720,
        max_size_bytes=100,
    )

    assert list(selector({"formats": formats}))


def test_inspected_option_sums_exact_video_and_audio_size() -> None:
    formats = [
        _format("audio", size=10, audio=True),
        _format("video", size=40, video=True, height=2160),
    ]

    option = inspect_format_option(
        _best_video_audio_selector,
        {"formats": formats},
        mode=DownloadMode.VIDEO_2160,
        max_size_bytes=100,
        duration_seconds=60,
        mp3_bitrate_kbps=192,
    )

    assert option is not None
    assert option.height == 2160
    assert option.size_bytes == 50
    assert option.size_confidence.value == "exact"


def test_inspected_option_estimates_missing_size_from_bitrate() -> None:
    formats = [
        {
            **_format("audio", size=0, audio=True),
            "filesize": None,
            "abr": 128,
        },
        {
            **_format("video", size=0, video=True, height=1440),
            "filesize": None,
            "vbr": 2000,
        },
    ]

    option = inspect_format_option(
        _best_video_audio_selector,
        {"formats": formats},
        mode=DownloadMode.VIDEO_1440,
        max_size_bytes=100_000_000,
        duration_seconds=60,
        mp3_bitrate_kbps=192,
    )

    assert option is not None
    assert option.size_bytes == int(60 * (128 + 2000) * 1000 / 8)
    assert option.size_confidence.value == "estimated"


def test_inspected_option_uses_selected_component_sizes_and_exact_precedence() -> None:
    audio = {
        **_native_format(
            "140",
            ext="m4a",
            size=10,
            acodec="mp4a.40.2",
        ),
        "filesize_approx": 999,
    }
    video = {
        **_native_format(
            "137",
            ext="mp4",
            size=40,
            vcodec="avc1.640028",
            height=1080,
        ),
        "width": 1920,
        "fps": 30,
        "dynamic_range": "SDR",
        "filesize_approx": 999,
    }

    option = inspect_format_option(
        _best_video_audio_selector,
        {"formats": [audio, video]},
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=100,
        duration_seconds=60,
        mp3_bitrate_kbps=192,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
        compatible_container=OutputContainer.MP4,
    )

    assert option is not None
    assert option.size_bytes == 50
    assert option.size_confidence.value == "exact"
    assert option.video_size_bytes == 40
    assert option.audio_size_bytes == 10
    assert option.selected_format_ids == ("137", "140")
    assert option.video_codec == "avc1.640028"
    assert option.audio_codec == "mp4a.40.2"
    assert option.dynamic_range == "SDR"


def test_mp3_size_uses_configured_bitrate() -> None:
    option = inspect_format_option(
        _best_video_audio_selector,
        {
            "formats": [
                _format("audio", size=10, audio=True),
                _format("video", size=40, video=True, height=1080),
            ]
        },
        mode=DownloadMode.AUDIO_MP3,
        max_size_bytes=100,
        duration_seconds=120,
        mp3_bitrate_kbps=192,
    )

    assert option is not None
    assert option.size_bytes == 2_880_000
    assert option.size_confidence.value == "estimated"


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


def _native_format(
    format_id: str,
    *,
    ext: str,
    size: int,
    vcodec: str = "none",
    acodec: str = "none",
    height: int | None = None,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": ext,
        "filesize": size,
        "vcodec": vcodec,
        "acodec": acodec,
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
