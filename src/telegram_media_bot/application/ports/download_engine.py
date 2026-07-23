from collections.abc import Callable
from typing import Protocol

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
    def inspect(self, url: str) -> MediaInfo:
        """Return normalized metadata without downloading the media."""
        ...

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> DownloadResult:
        """Download and return one normalized final file."""
        ...

    def health(self) -> ComponentHealth:
        """Return a local, network-free engine health check."""
        ...
