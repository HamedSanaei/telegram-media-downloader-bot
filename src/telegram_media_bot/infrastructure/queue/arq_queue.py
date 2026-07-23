from __future__ import annotations

from arq.connections import ArqRedis, RedisSettings, create_pool

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import DownloadMode, JobId


class ArqJobQueue(JobQueue):
    def __init__(self, redis: ArqRedis, queue_name: str) -> None:
        self._redis = redis
        self._queue_name = queue_name

    @classmethod
    async def create(cls, settings: Settings) -> "ArqJobQueue":
        redis = await create_pool(RedisSettings.from_dsn(settings.redis.url))
        return cls(redis=redis, queue_name=settings.redis.queue_name)

    async def close(self) -> None:
        await self._redis.close(close_connection_pool=True)

    async def enqueue_download(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode,
    ) -> JobId:
        job = await self._redis.enqueue_job(
            "process_download_job",
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=mode.value,
            _queue_name=self._queue_name,
        )
        if job is None:
            raise RuntimeError("ARQ did not create a job")
        return JobId(job.job_id)
