from __future__ import annotations

from typing import Any, cast

from arq.jobs import JobStatus as ArqJobStatus

from telegram_media_bot.domain.models import DownloadMode, JobId, QueueJobStatus
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


async def test_enqueue_payloads_use_canonical_youtube_video_url() -> None:
    class EnqueueRedis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def enqueue_job(self, function: str, **kwargs: object) -> None:
            self.calls.append((function, kwargs))

    redis = EnqueueRedis()
    queue = ArqJobQueue(cast(Any, redis), "media-downloads", owns_pool=False)
    raw = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"

    await queue.enqueue_inspection(
        job_id=JobId("inspect"),
        chat_id=1,
        user_id=2,
        url=raw,
    )
    await queue.enqueue_download(
        job_id=JobId("download"),
        chat_id=1,
        user_id=2,
        url=raw,
        mode=DownloadMode.VIDEO_1080,
    )

    assert [call[1]["url"] for call in redis.calls] == [
        "https://www.youtube.com/watch?v=DGbwtVtthu8",
        "https://www.youtube.com/watch?v=DGbwtVtthu8",
    ]
