from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Mapping
from typing import Any

from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlNoImagesError,
    GalleryDlOutputChangedError,
)
from telegram_media_bot.domain.models import MediaAsset, MediaKind
from telegram_media_bot.infrastructure.gallerydl.models import GalleryInspection

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif"}
_VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "mkv"}
_POST_ID_KEYS = {
    "instagram": ("post_shortcode", "shortcode", "post_id", "id"),
    "tiktok": ("id", "post_id", "aweme_id"),
    "twitter": ("tweet_id", "conversation_id", "id"),
    "pinterest": ("pin_id", "id"),
}


def parse_inspection(
    payload: bytes,
    *,
    expected_provider: str,
    max_assets: int,
) -> GalleryInspection:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GalleryDlOutputChangedError("gallery-dl returned invalid JSON") from exc
    if not isinstance(raw, list):
        raise GalleryDlOutputChangedError("gallery-dl JSON root is not an event list")
    metadata_items: list[Mapping[str, Any]] = []
    for event in raw:
        if not isinstance(event, list) or len(event) != 3 or not isinstance(event[2], Mapping):
            continue
        if not isinstance(event[1], str) or not event[1].startswith(("http://", "https://")):
            raise GalleryDlOutputChangedError("gallery-dl asset event has no HTTP(S) URL")
        metadata_items.append(event[2])
    if not metadata_items:
        raise GalleryDlOutputChangedError("gallery-dl emitted no asset events")
    if len(metadata_items) > max_assets:
        raise CollectionTooLargeError("Gallery asset count exceeds the configured limit")
    provider = _provider(metadata_items[0])
    if provider != expected_provider:
        raise GalleryDlOutputChangedError("gallery-dl provider does not match the requested URL")
    post_id = _post_id(metadata_items[0], provider)
    title = _text(metadata_items[0], "description", "content", "caption", "title") or "Media"
    assets = tuple(
        _asset(item, provider=provider, post_id=post_id, index=index)
        for index, item in enumerate(metadata_items, start=1)
    )
    if not any(asset.kind is MediaKind.IMAGE for asset in assets):
        raise GalleryDlNoImagesError("The post contains no image assets")
    return GalleryInspection(provider=provider, post_id=post_id, title=title[:512], assets=assets)


def transient_asset_urls(payload: bytes) -> tuple[str, ...]:
    """Return vendor URLs only for immediate SSRF validation inside the adapter boundary."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GalleryDlOutputChangedError("gallery-dl returned invalid JSON") from exc
    if not isinstance(raw, list):
        raise GalleryDlOutputChangedError("gallery-dl JSON root is not an event list")
    urls: list[str] = []
    for event in raw:
        if (
            isinstance(event, list)
            and len(event) == 3
            and isinstance(event[1], str)
            and event[1].startswith(("http://", "https://"))
        ):
            urls.append(event[1])
    return tuple(urls)


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
    for key in _POST_ID_KEYS[provider]:
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
