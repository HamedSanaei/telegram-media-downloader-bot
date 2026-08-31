from collections.abc import Callable
from typing import Protocol

from telegram_media_bot.domain.credential_resolution import ResolvedCredential
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadRequest,
    DownloadResult,
    MediaInfo,
    ProgressEvent,
)

ProgressSink = Callable[[ProgressEvent], None]
CancellationCheck = Callable[[], bool]


class DownloadEngine(Protocol):
    def inspect(
        self,
        url: str,
        *,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> MediaInfo:
        """Return normalized metadata without downloading the media.

        ``cookie_file`` is the explicit per-attempt credential context (a materialized
        Netscape cookie path leased to this attempt, or None). Engines/adapters never branch
        on subscription/VIP policy; the caller chooses which credential, if any, applies.
        """
        ...

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> DownloadResult:
        """Download and return one normalized final file."""
        ...

    def health(self) -> ComponentHealth:
        """Return a local, network-free engine health check."""
        ...


class InstagramVideoDownloadEngine(DownloadEngine, Protocol):
    def download_instagram_video_children(
        self,
        request: DownloadRequest,
        *,
        expected_parent_media_id: str,
        expected_total_slots: int,
        expected_video_indices: tuple[int, ...],
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> DownloadResult:
        """Resolve and strictly download every expected Instagram video child."""
        ...
