from __future__ import annotations

from typing import Any, cast

from arq.jobs import JobStatus as ArqJobStatus

from telegram_media_bot.domain.models import JobId, QueueJobStatus
from telegram_media_bot.infrastructure.queue import arq_queue
from telegram_media_bot.infrastructure.queue.arq_queue import ArqJobQueue


class FakePipeline:
    def __init__(self) -> None:
        self.operations: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def delete(self, *keys: object) -> FakePipeline:
        self.operations.append(("delete", keys))
        return self

    def zrem(self, *values: object) -> FakePipeline:
        self.operations.append(("zrem", values))
        return self

    async def execute(self) -> list[int]:
        return [4, 1, 1]


class FakeRedis:
    def __init__(self) -> None:
        self.transaction = FakePipeline()

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction
        return self.transaction


async def test_abort_job_uses_arq_abort_and_cleans_transient_keys(
    monkeypatch: Any,
) -> None:
    statuses = iter((ArqJobStatus.queued, ArqJobStatus.not_found))

    class FakeJob:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def status(self) -> ArqJobStatus:
            return next(statuses)

        async def abort(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(arq_queue, "Job", FakeJob)
    redis = FakeRedis()
    queue = ArqJobQueue(cast(Any, redis), "media-downloads", owns_pool=False)

    result = await queue.abort_job(JobId("cancel-me"))

    assert result.previous_status is QueueJobStatus.QUEUED
    assert result.final_status is QueueJobStatus.NOT_FOUND
    assert result.abort_requested
    assert result.redis_keys_removed == 6
    deleted = redis.transaction.operations[0][1]
    assert "arq:result:cancel-me" in deleted
    assert "arq:in-progress:cancel-me" in deleted
    assert ("zrem", ("media-downloads", "cancel-me")) in redis.transaction.operations
