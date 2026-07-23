from typing import Protocol

from telegram_media_bot.domain.models import DownloadRequest, DownloadResult, MediaInfo


class DownloadEngine(Protocol):
    def inspect(self, url: str) -> MediaInfo:
        """Return normalized metadata without downloading the media."""
        ...

    def download(self, request: DownloadRequest) -> DownloadResult:
        """Download and return one normalized final file."""
        ...
