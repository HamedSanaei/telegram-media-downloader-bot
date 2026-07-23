from typing import Protocol

from telegram_media_bot.domain.models import DownloadMode, JobId


class JobQueue(Protocol):
    async def enqueue_download(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode,
    ) -> JobId:
        """Enqueue a download and return its opaque project job ID."""
        ...
