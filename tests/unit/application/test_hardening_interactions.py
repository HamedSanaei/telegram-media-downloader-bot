"""Cross-hardening interaction tests (Scenarios A-E).

These compose the real SQLite-backed stores (inbox, job repository, effect ledger) with the
recovery service to prove the hardenings work together: retention never purges replayable work,
recovery drains gradually across restarts without loss or duplication, and replayed updates reuse
both the durable job and the status effect.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.services.durable_update_inbox import DurableUpdateInbox
from telegram_media_bot.application.services.effect_ledger import EffectLedgerService
from telegram_media_bot.application.services.job_recovery_service import JobRecoveryService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.effects import EffectState
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ErrorCategory,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    StoryDeliveryMode,
)
from telegram_media_bot.infrastructure.persistence.sqlite_effects import SqliteEffectLedger
from telegram_media_bot.infrastructure.persistence.sqlite_inbound_updates import (
    SqliteInboundUpdateRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository


def _job(job_id: str, url: str, *, user_id: int = 2, age_days: float = 0.0) -> JobRecord:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return JobRecord(
        job_id=JobId(job_id),
        kind=JobKind.DOWNLOAD,
        status=JobStatus.FAILED,
        chat_id=1,
        user_id=user_id,
        url=url,
        mode=DownloadMode.BEST,
        idempotency_key=f"key-{job_id}",
        created_at=created,
        updated_at=created,
        container=None,
        container_policy=ContainerPolicy.NATIVE_ONLY,
        selected_format_ids=(),
    )


class FakeQueue:
    def __init__(self, *, depth: int = 0) -> None:
        self.downloads: list[dict[str, object]] = []
        self._depth = depth

    async def queue_depth(self) -> int:
        return self._depth

    async def enqueue_download(self, **kwargs: object) -> JobId:
        self.downloads.append(kwargs)
        return JobId(str(kwargs.get("job_id", "")))

    async def enqueue_inspection(self, **kwargs: object) -> JobId:
        return JobId(str(kwargs.get("job_id", "")))


# --- Scenario A: retention + replay --------------------------------------------


def test_scenario_a_purge_keeps_active_replay(tmp_path: Path) -> None:
    repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    inbox = DurableUpdateInbox(repo)
    active = inbox.record(1, "message", '{"a": 1}')
    done = inbox.record(2, "message", '{"a": 2}')
    assert active is not None and done is not None
    inbox.start_processing(active)  # crash leaves it PROCESSING
    inbox.mark_completed(inbox.start_processing(done))
    # Backdate the completed row so it is eligible for purge, and the stuck row so it surfaces.
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="microseconds")
    recently = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(tmp_path / "state" / "jobs.sqlite3")) as connection:
        connection.execute(
            "UPDATE inbound_updates SET completed_at = ? WHERE update_id = 2", (old,)
        )
        connection.execute(
            "UPDATE inbound_updates SET received_at = ? WHERE update_id = 1", (recently,)
        )
        connection.commit()
    purged = repo.purge_retention(
        datetime.now(UTC),
        completed_retention_days=14,
        terminal_failure_retention_days=30,
        batch_size=500,
    )
    assert purged == 1
    # The PROCESSING update is untouched and still replays.
    pending = repo.pending_updates()
    assert [p.update_id for p in pending] == [1]
    assert repo.stuck_count(datetime.now(UTC) - timedelta(hours=1)) == 1


# --- Scenario B + E: gradual drain, restart-safe -------------------------------


def test_scenario_b_drain_is_gradual_and_restart_safe(tmp_path: Path) -> None:
    repository = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    repository.initialize()
    for index in range(60):
        job_id = f"ig-{index}"
        repository.create_job(_job(job_id, "https://www.instagram.com/p/AB/"))
        repository.record_recoverable_failure(JobId(job_id), ErrorCategory.AUTHENTICATION, "1.3.6")

    queue = cast(JobQueue, FakeQueue())
    first = JobRecoveryService(
        repository, queue, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    summary = asyncio.run(first.remediate_cookies(CookieService.INSTAGRAM))
    assert summary.requeued == 20
    assert len(cast(FakeQueue, queue).downloads) == 20

    # "Redis restarts": a fresh service instance on the same durable store keeps draining.
    queue2 = cast(JobQueue, FakeQueue())
    restarted = JobRecoveryService(
        repository, queue2, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    drain1 = asyncio.run(restarted.recover_maintenance_batch())
    drain2 = asyncio.run(restarted.recover_maintenance_batch())
    assert drain1.requeued == 20
    assert drain2.requeued == 20
    assert len(cast(FakeQueue, queue2).downloads) == 40
    assert repository.pending_recoverable_count() == 0
    # The marker is cleared on the first pass that finds an empty backlog; later passes are no-ops.
    final = asyncio.run(restarted.recover_maintenance_batch())
    assert final.requeued == 0
    assert repository.active_cookie_remediation_providers() == ()


def test_scenario_e_redis_loss_does_not_lose_or_duplicate(tmp_path: Path) -> None:
    repository = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    repository.initialize()
    for index in range(5):
        job_id = f"ig-{index}"
        repository.create_job(_job(job_id, "https://www.instagram.com/p/AB/"))
        repository.record_recoverable_failure(JobId(job_id), ErrorCategory.AUTHENTICATION, "1.3.6")

    class FlakyQueue(FakeQueue):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def enqueue_download(self, **kwargs: object) -> JobId:
            if self.fail:
                raise RuntimeError("redis down")
            return await super().enqueue_download(**kwargs)

    flaky = cast(JobQueue, FlakyQueue())
    service = JobRecoveryService(
        repository, flaky, max_attempts=2, max_age_days=7, remediation_batch_size=20
    )
    summary = asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    # SQLite state transitioned even though Redis was down; nothing is lost.
    assert summary.requeued == 5
    for i in range(5):
        loaded = repository.get_job(JobId(f"ig-{i}"))
        assert loaded is not None and loaded.status is JobStatus.QUEUED

    # Redis returns; startup reconciliation re-enqueues exactly the missing jobs once.
    healthy = cast(JobQueue, FakeQueue())
    reconciler = JobRecoveryService(repository, healthy, max_attempts=2, max_age_days=7)
    cast(FlakyQueue, flaky).fail = False

    async def missing_in_arq(job_id: JobId) -> bool:
        return True

    assert asyncio.run(reconciler.reconcile_recovery_requeues(missing_in_arq)) == 5

    # Repeated reconciliation finds the jobs already live in Redis (probe False) and does nothing.
    async def present_in_arq(job_id: JobId) -> bool:
        return False

    assert asyncio.run(reconciler.reconcile_recovery_requeues(present_in_arq)) == 0
    assert asyncio.run(reconciler.reconcile_recovery_requeues(present_in_arq)) == 0
    # Only five enqueues ever happened — no duplicate delivery.
    assert len(cast(FakeQueue, healthy).downloads) == 5


# --- Scenario C: replayed update reuses job + status effect --------------------


def test_scenario_c_replay_reuses_job_and_effect(tmp_path: Path) -> None:
    job_repo = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    job_repo.initialize()
    inbox_repo = SqliteInboundUpdateRepository(tmp_path / "state" / "jobs.sqlite3")
    inbox_repo.initialize()
    effect_repo = SqliteEffectLedger(tmp_path / "state" / "jobs.sqlite3")
    effect_repo.initialize()
    inbox = DurableUpdateInbox(inbox_repo)
    effects = EffectLedgerService(effect_repo)
    service = JobService(job_repo)

    url = "https://www.instagram.com/p/AB/"
    update_id = 42
    record = inbox.record(update_id, "message", '{"x": 1}')
    assert record is not None
    inbox.start_processing(record)

    # First execution: create the job and send the status effect.
    download, created = service.create_download(
        chat_id=7, user_id=3, url=url, mode=DownloadMode.BEST
    )
    assert created is True

    sent: list[int] = []

    async def send() -> int:
        sent.append(1000)
        return 1000

    async def first_pass() -> None:
        outcome = await effects.send_or_reuse(
            effect_key=f"update:{update_id}:inspection_status",
            effect_type="inspection_status",
            update_id=update_id,
            chat_id=7,
            send=send,
        )
        assert outcome.sent is True

    asyncio.run(first_pass())
    # Crash before the inbound update is marked completed.

    # Replay of the same update: the job is reused (no duplicate) and the status effect is
    # reused (no duplicate message).
    inbox.recovered(record)
    again, created_again = service.create_download(
        chat_id=7, user_id=3, url=url, mode=DownloadMode.BEST
    )
    assert created_again is False
    assert again.job_id == download.job_id

    edited: list[int] = []

    async def edit(message_id: int) -> None:
        edited.append(message_id)

    async def replay() -> None:
        outcome = await effects.send_or_reuse(
            effect_key=f"update:{update_id}:inspection_status",
            effect_type="inspection_status",
            update_id=update_id,
            chat_id=7,
            send=send,
            edit=edit,
        )
        assert outcome.sent is False
        assert outcome.message_id == 1000

    asyncio.run(replay())
    assert sent == [1000]
    assert edited == [1000]
    assert job_repo.counts().queued == 1


# --- Scenario D: story FILE mode survives remediation --------------------------


def test_scenario_d_story_file_mode_survives_cookie_remediation(tmp_path: Path) -> None:
    repository = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    repository.initialize()
    record = JobRecord(
        job_id=JobId("story-1"),
        kind=JobKind.DOWNLOAD,
        status=JobStatus.FAILED,
        chat_id=1,
        user_id=2,
        url="https://www.instagram.com/stories/exampleuser/",
        mode=DownloadMode.INSTAGRAM_ALL_STORIES,
        story_delivery_mode=StoryDeliveryMode.FILE,
        idempotency_key="key-story-1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        container=None,
        container_policy=ContainerPolicy.NATIVE_ONLY,
        selected_format_ids=(),
    )
    repository.create_job(record)
    repository.record_recoverable_failure(JobId("story-1"), ErrorCategory.AUTHENTICATION, "1.3.6")

    queue = cast(JobQueue, FakeQueue())
    service = JobRecoveryService(repository, queue, max_attempts=2, max_age_days=7)
    asyncio.run(service.remediate_cookies(CookieService.INSTAGRAM))
    enqueued = cast(FakeQueue, queue).downloads[0]
    assert enqueued["story_delivery_mode"] is StoryDeliveryMode.FILE
    assert enqueued["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES
    loaded = repository.get_job(JobId("story-1"))
    assert loaded is not None and loaded.story_delivery_mode is StoryDeliveryMode.FILE
    # FILE mode survives even if the effect ledger state is unchanged (pending prompt row).
    effect_repo = SqliteEffectLedger(tmp_path / "state" / "jobs.sqlite3")
    effect_repo.initialize()
    prompt = effect_repo.reserve(
        "update:7:story_delivery_mode_prompt",
        update_id=7,
        effect_type="story_delivery_mode_prompt",
        chat_id=1,
    )
    assert prompt.state is EffectState.PENDING


def test_scenario_d_prompt_effect_is_idempotent(tmp_path: Path) -> None:
    ledger = SqliteEffectLedger(tmp_path / "state" / "jobs.sqlite3")
    ledger.initialize()
    effects = EffectLedgerService(ledger)
    sends = 0
    edits = 0

    async def send() -> int:
        nonlocal sends
        sends += 1
        return 55

    async def edit(message_id: int) -> None:
        nonlocal edits
        edits += 1

    async def prompt() -> None:
        await effects.send_or_reuse(
            effect_key="update:9:story_delivery_mode_prompt",
            effect_type="story_delivery_mode_prompt",
            update_id=9,
            chat_id=1,
            send=send,
            edit=edit,
        )

    asyncio.run(prompt())
    # Crash before the update is completed; the callback is replayed.
    asyncio.run(prompt())
    assert sends == 1
    assert edits == 1
    # The pending effect survived the crash (the second pass reused, not duplicated).
    record = ledger.get("update:9:story_delivery_mode_prompt")
    assert record is not None and record.state is EffectState.COMPLETED
