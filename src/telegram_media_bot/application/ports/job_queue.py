from typing import Protocol

from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ImageDeliveryMode,
    JobAbortResult,
    JobId,
    NativeVideoCodec,
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

    async def enqueue_highlight_tray(
        self,
        *,
        job_id: JobId,
        chat_id: int,
        user_id: int,
        url: str,
        username: str,
    ) -> JobId:
        """Enqueue an Instagram highlight-tray fetch for the profile highlight browser."""
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
        native_video_codec: NativeVideoCodec | None = None,
        selected_format_ids: tuple[str, ...] = (),
        image_delivery_mode: ImageDeliveryMode | None = None,
    ) -> JobId:
        """Enqueue a download and return its opaque project job ID."""
        ...

    async def queue_depth(self) -> int:
        """Return the configured queue depth."""
        ...

    async def abort_job(
        self,
        job_id: JobId,
        *,
        timeout_seconds: float = 2,
        finalize_stale: bool = False,
    ) -> JobAbortResult:
        """Abort and finalize transient queue state for one durable job."""
        ...

    async def healthy(self) -> bool:
        """Return whether Redis responds."""
        ...
