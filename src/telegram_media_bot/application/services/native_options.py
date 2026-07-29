from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from telegram_media_bot.domain.models import (
    DownloadMode,
    MediaFormatOption,
    MediaInfo,
    NativeOptionView,
    NativeVideoCodec,
    OutputContainer,
    SizeConfidence,
)

_VIDEO_CONTAINERS = {OutputContainer.MP4, OutputContainer.WEBM}
_TARGET_HEIGHTS = {
    DownloadMode.BEST: 1080,
    DownloadMode.VIDEO_2160: 2160,
    DownloadMode.VIDEO_1440: 1440,
    DownloadMode.VIDEO_1080: 1080,
    DownloadMode.VIDEO_720: 720,
    DownloadMode.VIDEO_480: 480,
}


@dataclass(frozen=True, slots=True)
class NativeOptionCatalog:
    options: tuple[NativeOptionView, ...]
    raw_candidate_count: int
    planned_option_count: int
    deduplicated_option_count: int
    hidden_transcode_option_count: int
    unknown_size_option_count: int

    def for_container(self, container: OutputContainer) -> tuple[NativeOptionView, ...]:
        return tuple(option for option in self.options if option.container is container)

    def resolve(self, option_id: str) -> NativeOptionView | None:
        return next((option for option in self.options if option.option_id == option_id), None)

    def best_original(self) -> NativeOptionView | None:
        candidates = tuple(
            option for option in self.options if option.container in _VIDEO_CONTAINERS
        )
        return max(candidates, key=_best_original_sort_key, default=None)


def build_native_option_catalog(info: MediaInfo) -> NativeOptionCatalog:
    candidates: list[MediaFormatOption] = []
    hidden = 0
    for option in info.format_options:
        if option.container is OutputContainer.MP3:
            if option.mode is DownloadMode.AUDIO_MP3:
                candidates.append(option)
            continue
        if option.container not in _VIDEO_CONTAINERS:
            continue
        if not is_native_video_option(option):
            hidden += 1
            continue
        candidates.append(option)

    grouped: dict[tuple[object, ...], list[MediaFormatOption]] = {}
    for option in candidates:
        grouped.setdefault(_identity_key(option), []).append(option)

    selected = tuple(_preferred_option(group) for group in grouped.values())
    views = tuple(sorted((_to_view(option) for option in selected), key=_view_sort_key))
    return NativeOptionCatalog(
        options=views,
        raw_candidate_count=len(info.format_options),
        planned_option_count=len(candidates),
        deduplicated_option_count=len(views),
        hidden_transcode_option_count=hidden,
        unknown_size_option_count=sum(option.size_bytes is None for option in views),
    )


def is_native_video_option(option: MediaFormatOption | NativeOptionView) -> bool:
    container = option.container
    requires_transcode = (
        option.requires_transcode
        if isinstance(option, MediaFormatOption)
        else option.transcode_required
    )
    if container not in _VIDEO_CONTAINERS or requires_transcode:
        return False
    video_codec = option.video_codec
    audio_codec = option.audio_codec
    if container is OutputContainer.MP4:
        return native_video_codec(video_codec) in {
            NativeVideoCodec.AV1,
            NativeVideoCodec.H264,
        } and _is_aac(audio_codec)
    return _is_vp9(video_codec) and _is_opus(audio_codec)


def native_video_codec(codec: str | None) -> NativeVideoCodec | None:
    value = _normalized_codec(codec)
    if value == "av1" or value.startswith("av01"):
        return NativeVideoCodec.AV1
    if value == "h264" or value.startswith("avc1"):
        return NativeVideoCodec.H264
    if value == "vp9" or value.startswith("vp09"):
        return NativeVideoCodec.VP9
    return None


def display_video_codec(codec: str | None) -> str:
    family = native_video_codec(codec)
    if family is NativeVideoCodec.AV1:
        return "AV1"
    if family is NativeVideoCodec.H264:
        return "H.264"
    if family is NativeVideoCodec.VP9:
        return "VP9"
    return (codec or "نامشخص").strip()


def _identity_key(option: MediaFormatOption) -> tuple[object, ...]:
    return (
        option.selected_format_ids,
        option.width,
        option.height,
        option.fps,
        _normalized_codec(option.video_codec),
        _normalized_codec(option.audio_codec),
        option.container,
        (option.dynamic_range or ("HDR" if option.is_hdr else "SDR")).casefold(),
    )


def _preferred_option(options: list[MediaFormatOption]) -> MediaFormatOption:
    return max(
        options,
        key=lambda option: (
            int(option.height is not None and _TARGET_HEIGHTS.get(option.mode) == option.height),
            int(option.fallback_reason is None),
            int(option.size_confidence is SizeConfidence.EXACT),
            option.quality_score or 0.0,
            option.size_bytes or 0,
        ),
    )


def _to_view(option: MediaFormatOption) -> NativeOptionView:
    dynamic_range = option.dynamic_range or ("HDR" if option.is_hdr else "SDR")
    return NativeOptionView(
        option_id=_opaque_option_id(option),
        mode=option.mode,
        container=option.container or OutputContainer.MP3,
        actual_width=option.width,
        actual_height=option.height,
        actual_fps=option.fps,
        video_codec=option.video_codec,
        audio_codec=option.audio_codec,
        dynamic_range=dynamic_range,
        size_bytes=option.size_bytes,
        size_is_approximate=option.size_confidence is SizeConfidence.ESTIMATED,
        quality_score=option.quality_score,
        selected_format_ids=option.selected_format_ids,
        transcode_required=option.requires_transcode,
        display_label=_display_label(option, dynamic_range),
    )


def _opaque_option_id(option: MediaFormatOption) -> str:
    identity = "\x1f".join(
        (
            ",".join(option.selected_format_ids),
            option.container.value if option.container else "",
            str(option.width or ""),
            str(option.height or ""),
            f"{option.fps:g}" if option.fps is not None else "",
            _normalized_codec(option.video_codec),
            _normalized_codec(option.audio_codec),
            (option.dynamic_range or ("HDR" if option.is_hdr else "SDR")).casefold(),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()[:16]


def _display_label(option: MediaFormatOption, dynamic_range: str) -> str:
    details: list[str] = []
    if option.height is not None:
        details.append(f"{option.height}p")
    elif option.width is not None:
        details.append(f"{option.width}px")
    elif option.container is OutputContainer.MP3:
        details.append("MP3")
    if option.fps is not None:
        details.append(f"{option.fps:g}fps")
    if option.height is not None:
        details.append(display_video_codec(option.video_codec))
    if option.size_bytes is None or option.size_confidence is SizeConfidence.UNKNOWN:
        details.append("حجم نامشخص")
    else:
        size = _size(option.size_bytes)
        details.append(
            f"حدود {size}" if option.size_confidence is SizeConfidence.ESTIMATED else size
        )
    return " · ".join(details)


def _view_sort_key(option: NativeOptionView) -> tuple[object, ...]:
    container_order = {
        OutputContainer.MP4: 0,
        OutputContainer.WEBM: 1,
        OutputContainer.MP3: 2,
    }
    return (
        container_order[option.container],
        -(option.actual_height or 0),
        -(option.actual_fps or 0.0),
        option.dynamic_range != "SDR",
        -(option.quality_score or 0.0),
        -(option.size_bytes or 0),
        option.option_id,
    )


def _best_original_sort_key(option: NativeOptionView) -> tuple[object, ...]:
    return (
        option.actual_height or 0,
        option.actual_width or 0,
        option.actual_fps or 0.0,
        option.dynamic_range != "SDR",
        option.quality_score or 0.0,
        option.size_bytes or 0,
    )


def _normalized_codec(codec: str | None) -> str:
    return (codec or "").strip().casefold()


def _is_aac(codec: str | None) -> bool:
    value = _normalized_codec(codec)
    return value == "aac" or value.startswith("mp4a")


def _is_vp9(codec: str | None) -> bool:
    value = _normalized_codec(codec)
    return value == "vp9" or value.startswith("vp09")


def _is_opus(codec: str | None) -> bool:
    return _normalized_codec(codec) == "opus"


def _size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
