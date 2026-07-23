from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from telegram_media_bot.domain.models import MediaInfo, MediaKind


def normalize_source(info: Mapping[str, Any]) -> str:
    raw = info.get("extractor_key") or info.get("extractor") or "unknown"
    source = str(raw).split(":", maxsplit=1)[0].casefold()
    for prefix, normalized in (
        ("youtube", "youtube"),
        ("twitter", "twitter"),
        ("instagram", "instagram"),
        ("tiktok", "tiktok"),
        ("soundcloud", "soundcloud"),
        ("pinterest", "pinterest"),
    ):
        if source.startswith(prefix):
            return normalized
    return source


def detect_kind(info: Mapping[str, Any]) -> MediaKind:
    if info.get("entries") is not None or info.get("_type") in {"playlist", "multi_video"}:
        return MediaKind.PLAYLIST
    video_codec = info.get("vcodec")
    audio_codec = info.get("acodec")
    if video_codec and video_codec != "none":
        return MediaKind.VIDEO
    if audio_codec and audio_codec != "none":
        return MediaKind.AUDIO
    ext = str(info.get("ext") or "").casefold()
    if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def map_media_info(info: Mapping[str, Any], *, original_url: str) -> MediaInfo:
    entries = info.get("entries")
    item_count: int | None = None
    if isinstance(entries, list):
        item_count = len(entries)

    duration_raw = info.get("duration")
    duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
    if duration is None and isinstance(entries, list):
        durations: list[float] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            entry_duration = entry.get("duration")
            if isinstance(entry_duration, (int, float)):
                durations.append(float(entry_duration))
        if durations:
            duration = int(sum(durations))

    size_raw = info.get("filesize") or info.get("filesize_approx")
    estimated_size = int(size_raw) if isinstance(size_raw, (int, float)) else None
    if estimated_size is None and isinstance(entries, list):
        sizes: list[float] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            entry_size = entry.get("filesize") or entry.get("filesize_approx")
            if isinstance(entry_size, (int, float)):
                sizes.append(float(entry_size))
        if sizes:
            estimated_size = int(sum(sizes))

    return MediaInfo(
        media_id=str(info.get("id") or ""),
        title=str(info.get("title") or "Untitled"),
        source=normalize_source(info),
        kind=detect_kind(info),
        webpage_url=str(info.get("webpage_url") or original_url),
        uploader=str(info["uploader"]) if info.get("uploader") is not None else None,
        duration_seconds=duration,
        thumbnail_url=(str(info["thumbnail"]) if info.get("thumbnail") is not None else None),
        item_count=item_count,
        estimated_size_bytes=estimated_size,
    )
