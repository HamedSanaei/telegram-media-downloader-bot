from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlOutputChangedError,
)
from telegram_media_bot.domain.models import HighlightItem, MediaAsset, MediaKind
from telegram_media_bot.infrastructure.gallerydl.models import GalleryInspection

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif"}
_VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "mkv"}
_DIRECTORY_MESSAGE = 2
_URL_MESSAGE = 3
_POST_ID_KEYS = {
    "instagram": ("post_shortcode", "shortcode", "post_id", "id"),
    "tiktok": ("id", "post_id", "aweme_id"),
    "twitter": ("tweet_id", "conversation_id", "id"),
    "pinterest": ("pin_id", "id"),
}


@dataclass(frozen=True, slots=True)
class _DirectoryEvent:
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _UrlEvent:
    url: str
    metadata: Mapping[str, Any]


type _GalleryEvent = _DirectoryEvent | _UrlEvent


def parse_inspection(
    payload: bytes,
    *,
    expected_provider: str,
    max_assets: int,
) -> GalleryInspection:
    events = _parse_jsonl_events(payload)
    url_events = tuple(event for event in events if isinstance(event, _UrlEvent))
    metadata_items = [event.metadata for event in url_events]
    if not metadata_items:
        raise GalleryDlOutputChangedError("gallery-dl emitted no asset events")
    if len(metadata_items) > max_assets:
        raise CollectionTooLargeError("Gallery asset count exceeds the configured limit")
    provider = _provider(metadata_items[0])
    if provider != expected_provider:
        raise GalleryDlOutputChangedError("gallery-dl provider does not match the requested URL")
    post_id = _post_id(metadata_items[0], provider)
    directory_metadata = next(
        (event.metadata for event in events if isinstance(event, _DirectoryEvent)),
        None,
    )
    title_metadata = directory_metadata or metadata_items[0]
    title = _text(title_metadata, "description", "content", "caption", "title") or "Media"
    assets_list: list[MediaAsset] = []
    for index, event in enumerate(url_events, start=1):
        asset = _asset(event.metadata, provider=provider, post_id=post_id, index=index)
        if event.url.startswith("ytdl:") and asset.kind is not MediaKind.VIDEO:
            raise GalleryDlOutputChangedError("gallery-dl ytdl event is not video media")
        assets_list.append(asset)
    assets = tuple(assets_list)
    # A valid gallery-dl extraction may be image-only, video-only (story video, Reel, video post),
    # or mixed. "No image entries" is never "no downloadable media": the typed collection below
    # carries every IMAGE/VIDEO asset the extractor returned.
    return GalleryInspection(provider=provider, post_id=post_id, title=title[:512], assets=assets)


def transient_asset_urls(payload: bytes) -> tuple[str, ...]:
    """Return vendor URLs only for immediate SSRF validation inside the adapter boundary."""
    return tuple(
        _public_url(event.url)
        for event in _parse_jsonl_events(payload)
        if isinstance(event, _UrlEvent)
    )


def parse_highlight_tray(
    payload: bytes,
    *,
    expected_provider: str,
    max_highlights: int,
) -> tuple[HighlightItem, ...]:
    """Parse an Instagram highlight-tray inspection into stable per-highlight entries.

    Directory/URL metadata carries the highlight reel id (``highlight:<digits>``) and title;
    the returned id is the numeric routing id used by ``/stories/highlights/<id>/``.
    """
    events = _parse_jsonl_events(payload)
    provider = _provider(events[0].metadata)
    if provider != expected_provider:
        raise GalleryDlOutputChangedError("gallery-dl provider does not match the highlight tray")
    entries: dict[str, HighlightItem] = {}
    counts: dict[str, int] = {}
    for event in events:
        metadata = event.metadata
        raw_id = str(metadata.get("id") or metadata.get("highlight_id") or "").strip()
        routing_id = _highlight_routing_id(raw_id)
        if routing_id is None:
            continue
        title = _text(metadata, "title", "description", "content", "caption") or "بدون عنوان"
        item = HighlightItem(highlight_id=routing_id, title=title[:128], item_count=0)
        entries.setdefault(routing_id, item)
        if isinstance(event, _UrlEvent):
            counts[routing_id] = counts.get(routing_id, 0) + 1
    if not entries:
        raise GalleryDlOutputChangedError("gallery-dl emitted no highlight entries")
    ordered = tuple(
        HighlightItem(
            highlight_id=entry.highlight_id,
            title=entry.title,
            item_count=counts.get(entry.highlight_id, 0),
        )
        for entry in entries.values()
    )
    if len(ordered) > max_highlights:
        raise CollectionTooLargeError("Instagram highlight tray exceeds the configured limit")
    return ordered


def _highlight_routing_id(raw_id: str) -> str | None:
    candidate = raw_id.removeprefix("highlight:")
    if not candidate or not candidate.isascii() or not candidate.isdigit():
        return None
    return candidate[:128]


def _parse_jsonl_events(payload: bytes) -> tuple[_GalleryEvent, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GalleryDlOutputChangedError("gallery-dl returned invalid UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise GalleryDlOutputChangedError("gallery-dl emitted no JSON Lines events")
    events: list[_GalleryEvent] = []
    for line in lines:
        try:
            raw: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GalleryDlOutputChangedError("gallery-dl returned invalid JSON Lines") from exc
        if not isinstance(raw, list) or not raw:
            raise GalleryDlOutputChangedError("gallery-dl emitted a malformed message tuple")
        message_type = raw[0]
        if message_type == _DIRECTORY_MESSAGE:
            if len(raw) != 2:
                raise GalleryDlOutputChangedError("gallery-dl directory message shape changed")
            events.append(_DirectoryEvent(_metadata(raw[1])))
        elif message_type == _URL_MESSAGE:
            if len(raw) != 3 or not isinstance(raw[1], str):
                raise GalleryDlOutputChangedError("gallery-dl URL message shape changed")
            _public_url(raw[1])
            events.append(_UrlEvent(raw[1], _metadata(raw[2])))
        else:
            raise GalleryDlOutputChangedError("gallery-dl emitted an unexpected message type")
    return tuple(events)


def _metadata(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GalleryDlOutputChangedError("gallery-dl message metadata shape changed")
    return value


def _public_url(url: str) -> str:
    candidate = url.removeprefix("ytdl:")
    if not candidate.startswith(("http://", "https://")):
        raise GalleryDlOutputChangedError("gallery-dl asset event has no HTTP(S) URL")
    return candidate


def _provider(item: Mapping[str, Any]) -> str:
    category = str(item.get("category") or "").casefold()
    aliases = {
        "twitter": "twitter",
        "instagram": "instagram",
        "tiktok": "tiktok",
        "pinterest": "pinterest",
    }
    provider = aliases.get(category)
    if provider is None:
        raise GalleryDlOutputChangedError("gallery-dl emitted an unsupported category")
    return provider


def _post_id(item: Mapping[str, Any], provider: str) -> str:
    # For Instagram Stories the directory-level post identity is the account; the exact story
    # item keeps its own media identity, so prefer the per-item media id/shortcode there.
    keys = _POST_ID_KEYS[provider]
    if provider == "instagram" and str(item.get("subcategory") or "").casefold() == "stories":
        keys = ("media_id", "shortcode", "post_shortcode", "id")
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:128]
    raise GalleryDlOutputChangedError("gallery-dl asset has no stable post identity")


def _asset(item: Mapping[str, Any], *, provider: str, post_id: str, index: int) -> MediaAsset:
    extension = str(item.get("extension") or "").casefold().lstrip(".")
    declared = str(item.get("type") or "").casefold()
    kind = (
        MediaKind.IMAGE
        if declared in {"image", "photo"} or extension in _IMAGE_EXTENSIONS
        else MediaKind.VIDEO
        if declared == "video" or extension in _VIDEO_EXTENSIONS
        else MediaKind.UNKNOWN
    )
    if kind is MediaKind.UNKNOWN or not extension:
        raise GalleryDlOutputChangedError("gallery-dl asset type is unsupported")
    identity = f"{provider}\x1f{post_id}\x1f{index}\x1f{kind.value}\x1f{extension}"
    asset_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return MediaAsset(
        index=index,
        asset_id=asset_id,
        kind=kind,
        extension=extension,
        mime_type=mimetypes.guess_type(f"asset.{extension}")[0],
        source_post_id=post_id,
        provider=provider,
        width=_positive_int(item.get("width")),
        height=_positive_int(item.get("height")),
        duration_seconds=_positive_int(item.get("duration")),
        size_bytes=_positive_int(item.get("filesize") or item.get("file_size")),
        title=(_text(item, "description", "content", "caption", "title") or None),
    )


def _positive_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def _text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""
