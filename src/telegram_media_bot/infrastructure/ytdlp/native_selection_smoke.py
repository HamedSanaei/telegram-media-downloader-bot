from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    JobId,
    Mp4NativeFallback,
    NativeVideoCodec,
    OutputContainer,
)
from telegram_media_bot.infrastructure.ytdlp.options import (
    bounded_format_selector,
    inspect_format_option,
    native_merge_output_args,
)


def main() -> None:
    formats = [
        _format("140", "m4a", "none", "mp4a.40.2", None, 30, 70),
        _format("251", "webm", "none", "opus", None, 30, 130),
        _format("137", "mp4", "avc1.640028", "none", 1080, 30, 1800),
        _format("401", "mp4", "av01.0.12M.08", "none", 2160, 30, 5600),
        _format("248", "webm", "vp09.00.40.08", "none", 1080, 30, 2500),
    ]
    context = {"duration": 2970, "formats": formats}
    mp4_candidate = next(
        iter(
            bounded_format_selector(
                _best_components,
                mode=DownloadMode.VIDEO_1080,
                max_size_bytes=1024 * 1024 * 1024,
                compatible_container=OutputContainer.MP4,
                native_video_codec=NativeVideoCodec.H264,
                mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
            )(context)
        )
    )
    av1_candidate = next(
        iter(
            bounded_format_selector(
                _best_components,
                mode=DownloadMode.VIDEO_2160,
                max_size_bytes=4 * 1024 * 1024 * 1024,
                compatible_container=OutputContainer.MP4,
                native_video_codec=NativeVideoCodec.AV1,
                mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
            )(context)
        )
    )
    webm_candidate = next(
        iter(
            bounded_format_selector(
                _best_components,
                mode=DownloadMode.VIDEO_1080,
                max_size_bytes=1024 * 1024 * 1024,
                compatible_container=OutputContainer.WEBM,
            )(context)
        )
    )
    mp4_ids = _selected_ids(mp4_candidate)
    av1_ids = _selected_ids(av1_candidate)
    webm_ids = _selected_ids(webm_candidate)
    if mp4_ids != ("137", "140") or "401" in mp4_ids:
        raise RuntimeError(f"Unexpected fast MP4 selection: {mp4_ids}")
    if av1_ids != ("401", "140"):
        raise RuntimeError(f"Unexpected native AV1 MP4 selection: {av1_ids}")
    if webm_ids != ("248", "251"):
        raise RuntimeError(f"Unexpected native WebM selection: {webm_ids}")

    mp4_option = inspect_format_option(
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
    if mp4_option is None or mp4_option.requires_transcode:
        raise RuntimeError("Fast MP4 unexpectedly requires transcoding")
    av1_option = inspect_format_option(
        _best_components,
        context,
        mode=DownloadMode.VIDEO_2160,
        max_size_bytes=4 * 1024 * 1024 * 1024,
        duration_seconds=2970,
        mp3_bitrate_kbps=192,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
        compatible_container=OutputContainer.MP4,
        native_video_codec=NativeVideoCodec.AV1,
        mp4_native_fallback=Mp4NativeFallback.LOWER_RESOLUTION,
    )
    if av1_option is None or av1_option.requires_transcode:
        raise RuntimeError("Native AV1 MP4 unexpectedly requires transcoding")

    merger_args = native_merge_output_args(OutputContainer.MP4)
    required = ("-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart")
    if tuple(merger_args) != required:
        raise RuntimeError(f"Unexpected MP4 merger arguments: {merger_args}")
    forbidden = ("libx264", "-crf", "scale=", "fps=", "format=yuv420p")
    rendered_args = " ".join(merger_args)
    if any(value in rendered_args for value in forbidden):
        raise RuntimeError(f"Native MP4 merger contains encode arguments: {rendered_args}")

    original = DownloadRequest(
        job_id=JobId("best-original-smoke"),
        url="https://example.invalid/video",
        mode=DownloadMode.BEST_ORIGINAL,
        output_directory=Path("/tmp/best-original-smoke"),
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
    )
    if original.container_policy is not ContainerPolicy.NATIVE_ONLY:
        raise RuntimeError("Best Original did not normalize to native-only")

    print(
        json.dumps(
            {
                "mp4_format_ids": mp4_ids,
                "mp4_transcode_required": mp4_option.requires_transcode,
                "mp4_av1_format_ids": av1_ids,
                "mp4_av1_visible": True,
                "mp4_av1_transcode_required": av1_option.requires_transcode,
                "mp4_h264_visible": True,
                "libx264_invoked": False,
                "stream_copy": True,
                "mp4_merger_args": merger_args,
                "webm_format_ids": webm_ids,
                "best_original_policy": original.container_policy.value,
            },
            sort_keys=True,
        )
    )


def _format(
    format_id: str,
    ext: str,
    vcodec: str,
    acodec: str,
    height: int | None,
    fps: int,
    bitrate: int,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "height": height,
        "fps": fps,
        "tbr": bitrate,
        "filesize_approx": bitrate * 2970 * 1000 // 8,
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


def _selected_ids(candidate: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(item["format_id"]) for item in candidate["requested_formats"])


if __name__ == "__main__":
    main()
