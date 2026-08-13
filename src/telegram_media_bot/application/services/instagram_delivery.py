from telegram_media_bot.domain.models import (
    DownloadMode,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
)


def instagram_default_bundle_option(info: MediaInfo) -> MediaFormatOption:
    """Return the complete, lossless source-media plan for an Instagram image post."""
    if info.source.casefold() != "instagram":
        raise ValueError("Image delivery confirmation is Instagram-only")
    images = tuple(asset for asset in info.assets if asset.kind is MediaKind.IMAGE)
    videos = tuple(asset for asset in info.assets if asset.kind is MediaKind.VIDEO)
    if not images:
        raise ValueError("Instagram item has no image assets")
    mode = (
        DownloadMode.ALL_ORIGINAL_MEDIA
        if videos
        else DownloadMode.IMAGE_ORIGINAL
        if len(images) == 1
        else DownloadMode.IMAGES_ORIGINAL
    )
    option = next((item for item in info.format_options if item.mode is mode), None)
    if option is None:
        raise ValueError("Instagram complete-media plan is unavailable")
    return option


def requires_instagram_image_confirmation(info: MediaInfo) -> bool:
    return info.source.casefold() == "instagram" and any(
        asset.kind is MediaKind.IMAGE for asset in info.assets
    )
