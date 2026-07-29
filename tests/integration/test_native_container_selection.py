from typing import Any

from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    Mp4NativeFallback,
    NativeVideoCodec,
    OutputContainer,
)
from telegram_media_bot.infrastructure.ytdlp.options import inspect_format_option


def test_production_like_metadata_selects_native_mp4_and_webm_without_transcode() -> None:
    context = {
        "duration": 2970,
        "formats": [
            _format("140", "m4a", "none", "mp4a.40.2", 70, None, None),
            _format("251", "webm", "none", "opus", 130, None, None),
            _format("136", "mp4", "avc1.4d401f", "none", 900, 720, 30),
            _format("137", "mp4", "avc1.640028", "none", 1800, 1080, 30),
            _format("399", "mp4", "av01.0.08M.08", "none", 2200, 1080, 60),
            _format("248", "webm", "vp09.00.40.08", "none", 2500, 1080, 30),
        ],
    }
    mp4 = inspect_format_option(
        _best_components,
        context,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=1024 * 1024 * 1024,
        duration_seconds=2970,
        mp3_bitrate_kbps=192,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
        compatible_container=OutputContainer.MP4,
        native_video_codec=NativeVideoCodec.H264,
        mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
    )
    webm = inspect_format_option(
        _best_components,
        context,
        mode=DownloadMode.VIDEO_1080,
        max_size_bytes=1024 * 1024 * 1024,
        duration_seconds=2970,
        mp3_bitrate_kbps=192,
        container=OutputContainer.WEBM,
        container_policy=ContainerPolicy.GUARANTEED,
        compatible_container=OutputContainer.WEBM,
    )

    assert mp4 is not None
    assert mp4.height == 1080
    assert mp4.requires_transcode is False
    assert mp4.selection_reason == "native_mp4_exact_resolution"
    assert webm is not None
    assert webm.height == 1080
    assert webm.requires_transcode is False
    assert webm.selection_reason == "webm_native"


def test_native_catalog_exposes_only_real_unique_mp4_and_webm_resolutions() -> None:
    context = {
        "duration": 300,
        "formats": [
            _format("140", "m4a", "none", "mp4a.40.2", 70, None, None),
            _format("251", "webm", "none", "opus", 130, None, None),
            _format("134", "mp4", "avc1.4d401e", "none", 400, 480, 30),
            _format("136", "mp4", "avc1.4d401f", "none", 900, 720, 30),
            _format("137", "mp4", "avc1.640028", "none", 1800, 1080, 30),
            _format("400", "mp4", "av01.0.12M.08", "none", 4000, 1440, 30),
            _format("401", "mp4", "av01.0.12M.08", "none", 8000, 2160, 30),
            _format("244", "webm", "vp9", "none", 500, 480, 30),
            _format("247", "webm", "vp9", "none", 1000, 720, 30),
            _format("248", "webm", "vp9", "none", 2200, 1080, 30),
            _format("271", "webm", "vp9", "none", 4500, 1440, 30),
            _format("313", "webm", "vp9", "none", 9000, 2160, 30),
        ],
    }
    modes = (
        DownloadMode.VIDEO_2160,
        DownloadMode.VIDEO_1440,
        DownloadMode.VIDEO_1080,
        DownloadMode.VIDEO_720,
        DownloadMode.VIDEO_480,
    )
    planned: list[MediaFormatOption] = []
    for mode in modes:
        for container in (OutputContainer.MP4, OutputContainer.WEBM):
            codec_families = (
                (NativeVideoCodec.AV1, NativeVideoCodec.H264)
                if container is OutputContainer.MP4
                else (NativeVideoCodec.VP9,)
            )
            for codec_family in codec_families:
                option = inspect_format_option(
                    _best_components,
                    context,
                    mode=mode,
                    max_size_bytes=1024 * 1024 * 1024,
                    duration_seconds=300,
                    mp3_bitrate_kbps=192,
                    container=container,
                    container_policy=ContainerPolicy.GUARANTEED,
                    compatible_container=container,
                    native_video_codec=codec_family,
                    mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
                )
                if option is not None:
                    planned.append(option)
    catalog = build_native_option_catalog(
        MediaInfo(
            media_id="fixture",
            title="fixture",
            source="youtube",
            kind=MediaKind.VIDEO,
            webpage_url="https://example.test/video",
            format_options=tuple(planned),
        )
    )

    mp4 = catalog.for_container(OutputContainer.MP4)
    webm = catalog.for_container(OutputContainer.WEBM)

    assert [option.actual_height for option in mp4] == [2160, 1440, 1080, 720, 480]
    assert [option.actual_height for option in webm] == [2160, 1440, 1080, 720, 480]
    assert [option.video_codec for option in mp4[:2]] == [
        "av01.0.12M.08",
        "av01.0.12M.08",
    ]
    assert all(option.video_codec and option.video_codec.startswith("avc1") for option in mp4[2:])
    assert all(option.audio_codec and option.audio_codec.startswith("mp4a") for option in mp4)
    assert all(option.video_codec == "vp9" for option in webm)
    assert all(option.audio_codec == "opus" for option in webm)
    assert all(not option.transcode_required for option in (*mp4, *webm))


def _format(
    format_id: str,
    ext: str,
    vcodec: str,
    acodec: str,
    bitrate: int,
    height: int | None,
    fps: int | None,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "height": height,
        "width": round(height * 16 / 9) if height else None,
        "fps": fps,
        "tbr": bitrate,
        "protocol": "https",
    }


def _best_components(context: dict[str, Any]) -> list[dict[str, Any]]:
    formats = context["formats"]
    videos = [item for item in formats if item["vcodec"] != "none"]
    audios = [item for item in formats if item["acodec"] != "none"]
    if not videos or not audios:
        return []
    video = max(
        videos,
        key=lambda item: (
            int(item.get("height") or 0),
            float(item.get("fps") or 0),
            float(item.get("tbr") or 0),
        ),
    )
    audio = max(audios, key=lambda item: float(item.get("tbr") or 0))
    return [
        {
            "format_id": f"{video['format_id']}+{audio['format_id']}",
            "requested_formats": [video, audio],
            "vcodec": video["vcodec"],
            "acodec": audio["acodec"],
        }
    ]
