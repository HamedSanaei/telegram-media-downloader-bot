from __future__ import annotations

from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
)
from telegram_media_bot.infrastructure.gallerydl.models import GalleryInspection


def map_gallery_info(inspection: GalleryInspection, canonical_url: str) -> MediaInfo:
    images = tuple(asset for asset in inspection.assets if asset.kind is MediaKind.IMAGE)
    videos = tuple(asset for asset in inspection.assets if asset.kind is MediaKind.VIDEO)
    options: list[MediaFormatOption] = []
    if len(inspection.assets) == 1 and images:
        options.append(_option(DownloadMode.IMAGE_ORIGINAL, images))
    elif images and not videos:
        options.extend(
            (
                _option(DownloadMode.IMAGES_ORIGINAL, images),
                _option(DownloadMode.IMAGES_ZIP, images),
            )
        )
    elif images and videos:
        options.extend(
            (
                _option(DownloadMode.ALL_ORIGINAL_MEDIA, inspection.assets),
                _option(DownloadMode.IMAGES_ONLY, images),
                _option(DownloadMode.VIDEOS_ONLY, videos),
                _option(DownloadMode.IMAGES_ZIP, images),
            )
        )
    return MediaInfo(
        media_id=inspection.post_id,
        title=inspection.title,
        source=inspection.provider,
        kind=MediaKind.IMAGE if len(inspection.assets) == 1 else MediaKind.PLAYLIST,
        webpage_url=canonical_url,
        item_count=len(inspection.assets),
        estimated_size_bytes=(
            sum(asset.size_bytes or 0 for asset in inspection.assets)
            if all(asset.size_bytes is not None for asset in inspection.assets)
            else None
        ),
        format_options=tuple(options),
        assets=inspection.assets,
    )


def _option(mode: DownloadMode, assets: tuple[MediaAsset, ...]) -> MediaFormatOption:
    return MediaFormatOption(
        mode=mode,
        container_policy=ContainerPolicy.NATIVE_ONLY,
        selected_format_ids=tuple(asset.asset_id for asset in assets),
    )
