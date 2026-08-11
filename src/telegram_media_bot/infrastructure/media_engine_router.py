from __future__ import annotations

import structlog

from telegram_media_bot.application.ports.download_engine import (
    CancellationCheck,
    DownloadEngine,
    ProgressSink,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.domain.errors import (
    GalleryDlNoImagesError,
    GalleryDlUnsupportedUrlError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadRequest,
    DownloadResult,
    MediaInfo,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine

logger = structlog.get_logger(__name__)


class RoutedMediaEngine(DownloadEngine):
    """Choose an engine from normalized inspection outcome, never from Telegram handlers."""

    def __init__(self, gallery: GalleryDlEngine, ytdlp: DownloadEngine) -> None:
        self._gallery = gallery
        self._ytdlp = ytdlp

    def inspect(self, url: str) -> MediaInfo:
        url = canonicalize_media_url(url).canonical_url
        try:
            return self._gallery.inspect(url)
        except GalleryDlNoImagesError as exc:
            logger.info(
                "media_engine_fallback",
                from_adapter="gallery-dl",
                to_adapter="yt-dlp",
                reason=type(exc).__name__,
            )
            return self._ytdlp.inspect(url)
        except GalleryDlUnsupportedUrlError as exc:
            if self._gallery.is_gallery_social_url(url):
                raise
            logger.info(
                "media_engine_fallback",
                from_adapter="gallery-dl",
                to_adapter="yt-dlp",
                reason=type(exc).__name__,
            )
            return self._ytdlp.inspect(url)

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> DownloadResult:
        engine: DownloadEngine = (
            self._gallery if self._gallery.owns_mode(request.mode) else self._ytdlp
        )
        return engine.download(request, progress=progress, is_cancelled=is_cancelled)

    def health(self) -> ComponentHealth:
        gallery = self._gallery.health()
        ytdlp = self._ytdlp.health()
        healthy = ytdlp.healthy and gallery.healthy
        return ComponentHealth(
            "media_engines",
            healthy,
            f"yt-dlp={ytdlp.detail}; gallery-dl={gallery.detail}",
        )
