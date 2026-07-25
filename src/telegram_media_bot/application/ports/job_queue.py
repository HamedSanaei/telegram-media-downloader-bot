from typing import Protocol

from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    JobId,
    OutputContainer,
)


class JobQueue(Protocol):
    async def enqueue_inspection(
        self,
        *,
        job_id: JobId,
        chat_id: int,
        user_id: int,
        url: str,
    ) -> JobId:
        """Enqueue metadata inspection without blocking the bot process."""
        ...

    async def enqueue_download(
        self,
        *,
        job_id: JobId,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode,
        container: OutputContainer | None = None,
        container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
    ) -> JobId:
        """Enqueue a download and return its opaque project job ID."""
        ...

    async def queue_depth(self) -> int:
        """Return the configured queue depth."""
        ...

    async def healthy(self) -> bool:
        """Return whether Redis responds."""
        ...
