from __future__ import annotations

import asyncio

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.constants import (
    abort_jobs_ss,
    in_progress_key_prefix,
    job_key_prefix,
    result_key_prefix,
    retry_key_prefix,
)
from arq.jobs import Job
from arq.jobs import JobStatus as ArqJobStatus

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    JobAbortResult,
    JobId,
    NativeVideoCodec,
    OutputContainer,
    QueueJobStatus,
    normalize_container_policy,
)


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
        container: OutputContainer | None = None,
        container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
        native_video_codec: NativeVideoCodec | None = None,
    ) -> JobId:
        container_policy = normalize_container_policy(mode, container_policy)
        await self._redis.enqueue_job(
            "process_download_job",
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=mode.value,
            container=container.value if container else None,
            container_policy=container_policy.value,
            native_video_codec=native_video_codec.value if native_video_codec else None,
            _job_id=str(job_id),
            _queue_name=self._queue_name,
        )
        return job_id

    async def queue_depth(self) -> int:
        return int(await self._redis.zcard(self._queue_name))

    async def abort_job(
        self,
        job_id: JobId,
        *,
        timeout_seconds: float = 2,
        finalize_stale: bool = False,
    ) -> JobAbortResult:
        raw_id = str(job_id)
        job = Job(raw_id, self._redis, _queue_name=self._queue_name)
        try:
            previous = _queue_status(await job.status())
        except Exception:
            previous = QueueJobStatus.UNKNOWN
        abort_requested = False
        if previous not in {QueueJobStatus.COMPLETE, QueueJobStatus.NOT_FOUND}:
            try:
                abort_requested = await job.abort(timeout=timeout_seconds, poll_delay=0.1)
            except TimeoutError:
                abort_requested = True
            except asyncio.CancelledError:
                raise
            except Exception:
                abort_requested = False
        try:
            final = _queue_status(await job.status())
        except Exception:
            final = QueueJobStatus.UNKNOWN
        removed = 0
        safe_to_finalize = final is not QueueJobStatus.IN_PROGRESS or finalize_stale
        if safe_to_finalize:
            keys = (
                job_key_prefix + raw_id,
                in_progress_key_prefix + raw_id,
                retry_key_prefix + raw_id,
                result_key_prefix + raw_id,
            )
            async with self._redis.pipeline(transaction=True) as transaction:
                transaction.delete(*keys)
                transaction.zrem(self._queue_name, raw_id)
                transaction.zrem(abort_jobs_ss, raw_id)
                responses = await transaction.execute()
            removed = sum(int(value or 0) for value in responses)
            final = QueueJobStatus.NOT_FOUND
        return JobAbortResult(
            previous_status=previous,
            final_status=final,
            abort_requested=abort_requested,
            redis_keys_removed=removed,
        )

    async def healthy(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


def _queue_status(status: ArqJobStatus) -> QueueJobStatus:
    try:
        return QueueJobStatus(status.value)
    except ValueError:
        return QueueJobStatus.UNKNOWN
