from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import structlog

from telegram_media_bot.application.ports.download_engine import (
    CancellationCheck,
    DownloadEngine,
    InstagramVideoDownloadEngine,
    ProgressSink,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.domain.errors import (
    GalleryDlOutputChangedError,
    GalleryDlUnsupportedUrlError,
    MediaBotError,
)
from telegram_media_bot.domain.models import (
    COLLECTION_MODES,
    ComponentHealth,
    ContainerPolicy,
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    MediaAsset,
    MediaInfo,
    MediaKind,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine

logger = structlog.get_logger(__name__)


class RoutedMediaEngine(DownloadEngine):
    """Choose an engine from normalized inspection outcome, never from Telegram handlers."""

    def __init__(self, gallery: GalleryDlEngine, ytdlp: InstagramVideoDownloadEngine) -> None:
        self._gallery = gallery
        self._ytdlp = ytdlp

    def inspect(self, url: str) -> MediaInfo:
        url = canonicalize_media_url(url).canonical_url
        try:
            return self._gallery.inspect(url)
        except GalleryDlUnsupportedUrlError as exc:
            if self._gallery.is_gallery_social_url(url):
                raise
            logger.info(
                "media_engine_fallback",
                from_adapter="gallery-dl",
                to_adapter="yt-dlp",
                reason=type(exc).__name__,
            )
            try:
                return self._ytdlp.inspect(url)
            except Exception as ytdlp_exc:
                _attach_fallback(ytdlp_exc, ("gallery-dl", "yt-dlp"), type(exc).__name__)
                raise

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> DownloadResult:
        if not self._gallery.owns_mode(request.mode):
            return self._ytdlp.download(
                request,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        info = self._gallery.inspect(request.url, max_assets=request.max_assets)
        if request.mode in COLLECTION_MODES:
            # Bulk Stories/Highlights stay gallery-dl-owned end to end: gallery-dl is the
            # primary engine and its native ordering is preserved.
            return self._gallery.download_inspected(
                request,
                info,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        images = tuple(asset for asset in info.assets if asset.kind is MediaKind.IMAGE)
        videos = tuple(asset for asset in info.assets if asset.kind is MediaKind.VIDEO)
        if info.source.casefold() != "instagram" or not videos:
            return self._gallery.download_inspected(
                request,
                info,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        if request.mode is DownloadMode.VIDEOS_ONLY:
            return self._download_instagram_videos(
                request,
                info,
                videos=videos,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        if request.mode is not DownloadMode.ALL_ORIGINAL_MEDIA:
            return self._gallery.download_inspected(
                request,
                info,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        return self._download_instagram_mixed(
            request,
            info,
            images=images,
            videos=videos,
            progress=progress,
            is_cancelled=is_cancelled,
        )

    def _download_instagram_videos(
        self,
        request: DownloadRequest,
        info: MediaInfo,
        *,
        videos: tuple[MediaAsset, ...],
        progress: ProgressSink | None,
        is_cancelled: CancellationCheck | None,
    ) -> DownloadResult:
        video_directory = request.output_directory / "videos"
        video_temp_directory = (
            request.temp_directory / "videos" if request.temp_directory is not None else None
        )
        try:
            result = self._ytdlp.download_instagram_video_children(
                replace(
                    request,
                    mode=DownloadMode.BEST_ORIGINAL,
                    output_directory=video_directory,
                    temp_directory=video_temp_directory,
                    container=None,
                    container_policy=ContainerPolicy.NATIVE_ONLY,
                    native_video_codec=None,
                    selected_format_ids=(),
                    allow_collection=False,
                ),
                expected_parent_media_id=info.media_id,
                expected_total_slots=len(info.assets),
                expected_video_indices=tuple(asset.index for asset in videos),
                progress=progress,
                is_cancelled=is_cancelled,
            )
            downloaded_videos = result.delivery_artifacts
            if (
                len(downloaded_videos) != len(videos)
                or any(artifact.kind is not MediaKind.VIDEO for artifact in downloaded_videos)
                or tuple(artifact.source_index for artifact in downloaded_videos)
                != tuple(asset.index for asset in videos)
            ):
                raise GalleryDlOutputChangedError("yt-dlp Instagram video count changed")
        except BaseException:
            _cleanup_split_directory(video_directory)
            if video_temp_directory is not None:
                _cleanup_split_directory(video_temp_directory)
            raise
        return result

    def _download_instagram_mixed(
        self,
        request: DownloadRequest,
        info: MediaInfo,
        *,
        images: tuple[MediaAsset, ...],
        videos: tuple[MediaAsset, ...],
        progress: ProgressSink | None,
        is_cancelled: CancellationCheck | None,
    ) -> DownloadResult:
        image_assets = tuple(asset for asset in info.assets if asset.kind is MediaKind.IMAGE)
        video_assets = tuple(asset for asset in info.assets if asset.kind is MediaKind.VIDEO)
        if len(image_assets) != len(images) or len(video_assets) != len(videos):
            raise GalleryDlOutputChangedError("Instagram media plan changed during routing")
        image_directory = request.output_directory / "images"
        video_directory = request.output_directory / "videos"
        video_temp_directory = (
            request.temp_directory / "videos" if request.temp_directory is not None else None
        )
        try:
            video_result = self._download_instagram_videos(
                request,
                info,
                videos=video_assets,
                progress=progress,
                is_cancelled=is_cancelled,
            )
            image_result = self._gallery.download_inspected(
                replace(
                    request,
                    mode=DownloadMode.IMAGES_ONLY,
                    output_directory=image_directory,
                    selected_format_ids=tuple(asset.asset_id for asset in image_assets),
                ),
                info,
                is_cancelled=is_cancelled,
            )
            downloaded_images = image_result.delivery_artifacts
            downloaded_videos = video_result.delivery_artifacts
            if len(downloaded_images) != len(image_assets):
                raise GalleryDlOutputChangedError("Instagram image count changed during download")
            if (
                len(downloaded_videos) != len(video_assets)
                or any(artifact.kind is not MediaKind.VIDEO for artifact in downloaded_videos)
                or tuple(artifact.source_index for artifact in downloaded_videos)
                != tuple(asset.index for asset in video_assets)
            ):
                raise GalleryDlOutputChangedError("yt-dlp Instagram video count changed")
            ordered: list[DownloadArtifact] = []
            ordered.extend(downloaded_images)
            ordered.extend(downloaded_videos)
            if any(artifact.source_index is None for artifact in ordered):
                raise GalleryDlOutputChangedError("Instagram source ordinal is missing")
            ordered.sort(key=lambda artifact: artifact.source_index or 0)
        except BaseException:
            _cleanup_split_directory(image_directory)
            _cleanup_split_directory(video_directory)
            if video_temp_directory is not None:
                _cleanup_split_directory(video_temp_directory)
            raise
        first = ordered[0]
        return DownloadResult(
            job_id=request.job_id,
            media_id=info.media_id,
            title=info.title,
            source="instagram",
            kind=MediaKind.PLAYLIST,
            file_path=first.file_path,
            file_size_bytes=sum(artifact.file_size_bytes for artifact in ordered),
            mime_type=first.mime_type,
            artifacts=tuple(ordered),
            inline_video_streamable=first.inline_video_streamable,
            image_delivery_mode=request.image_delivery_mode,
        )

    def health(self) -> ComponentHealth:
        gallery = self._gallery.health()
        ytdlp = self._ytdlp.health()
        healthy = ytdlp.healthy and gallery.healthy
        return ComponentHealth(
            "media_engines",
            healthy,
            f"yt-dlp={ytdlp.detail}; gallery-dl={gallery.detail}",
        )


def _cleanup_split_directory(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _attach_fallback(exc: BaseException, chain: tuple[str, ...], reason: str) -> None:
    if isinstance(exc, MediaBotError):
        if exc.fallback_chain is None:
            exc.fallback_chain = chain
        if exc.fallback_reason is None:
            exc.fallback_reason = reason
        if exc.adapter is None:
            exc.adapter = chain[-1]
