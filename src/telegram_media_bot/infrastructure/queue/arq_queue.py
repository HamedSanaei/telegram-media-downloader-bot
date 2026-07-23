from __future__ import annotations

from arq.connections import ArqRedis, RedisSettings, create_pool

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import DownloadMode, JobId


class ArqJobQueue(JobQueue):
    def __init__(self, redis: ArqRedis, queue_name: str, *, owns_pool: bool = True) -> None:
        self._redis = redis
        self._queue_name = queue_name
        self._owns_pool = owns_pool

    @classmethod
    async def create(cls, settings: Settings) -> ArqJobQueue:
        redis = await create_pool(RedisSettings.from_dsn(settings.redis.url))
        return cls(redis=redis, queue_name=settings.redis.queue_name)

    async def close(self) -> None:
        if self._owns_pool:
            await self._redis.close(close_connection_pool=True)

    async def enqueue_inspection(
        self,
        *,
        job_id: JobId,
        chat_id: int,
        user_id: int,
        url: str,
    ) -> JobId:
        await self._redis.enqueue_job(
            "process_inspection_job",
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            _job_id=str(job_id),
            _queue_name=self._queue_name,
        )
        return job_id

    async def enqueue_download(
        self,
        *,
        job_id: JobId,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode,
    ) -> JobId:
        await self._redis.enqueue_job(
            "process_download_job",
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=mode.value,
            _job_id=str(job_id),
            _queue_name=self._queue_name,
        )
        return job_id

    async def queue_depth(self) -> int:
        return int(await self._redis.zcard(self._queue_name))

    async def healthy(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False
