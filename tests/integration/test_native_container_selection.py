from typing import Any

from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    Mp4NativeFallback,
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
    assert mp4.selection_reason == "native_h264_exact_resolution"
    assert webm is not None
    assert webm.height == 1080
    assert webm.requires_transcode is False
    assert webm.selection_reason == "webm_native"


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
