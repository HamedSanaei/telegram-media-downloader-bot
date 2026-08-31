"""T014 persistence integration: subscription/grant tables, migration, snapshot round-trip."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_media_bot.domain.models import JobId, JobKind, JobRecord, JobStatus
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    EntitlementSnapshot,
    GrantId,
    PlanId,
    Subscription,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)

VIP = Capability.INSTAGRAM_PRIVATE_MEDIA


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _plan(plan_id: str = "vip-1") -> SubscriptionPlan:
    return SubscriptionPlan(
        plan_id=PlanId(plan_id),
        name="VIP",
        duration_months=1,
        price_minor=4900,
        currency="USD",
        enabled=True,
        capabilities=frozenset({VIP}),
    )


def _grant(grant_id: str, user_id: int, confirmed: datetime) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(grant_id),
        user_id=user_id,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        confirmed_at=confirmed,
        source_type="test",
        source_reference=f"order-{grant_id}",
        created_at=confirmed,
    )


def _sub(user_id: int, authorized_until: datetime | None) -> Subscription:
    return Subscription(
        user_id=user_id,
        authorized_until=authorized_until,
        cancelled_at=None,
        updated_at=_utc(2026, 1, 1),
    )


def test_plan_catalog_is_empty_by_default(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    with closing(sqlite3.connect(repo._path)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM subscription_plans").fetchone()
    assert count == (0,)


def test_empty_tables_are_valid(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    with closing(sqlite3.connect(repo._path)) as connection:
        for table in (
            "subscription_plans",
            "subscription_plan_capabilities",
            "subscriptions",
            "entitlement_grants",
        ):
            row = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row == (1,)
    assert repo.get_subscription(1) is None  # free user needs no row


def test_reinitialize_is_idempotent(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    plan = repo.get_plan(PlanId("vip-1"))
    assert plan is not None
    repo.initialize()  # second run must be a no-op without errors
    repo.initialize()
    assert repo.get_plan(PlanId("vip-1")) == plan


def test_plan_round_trip_with_capabilities(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    plan = _plan()
    repo.save_plan(plan)
    restored = repo.get_plan(PlanId("vip-1"))
    assert restored is not None
    assert restored.plan_id == plan.plan_id
    assert restored.duration_months == 1
    assert restored.price_minor == 4900
    assert restored.currency == "USD"
    assert restored.capabilities == frozenset({VIP})


def test_grant_and_subscription_round_trip(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    grant = _grant("g1", 7, _utc(2026, 1, 1))
    sub = _sub(7, _utc(2026, 2, 1))
    repo.create_grant_with_subscription(grant, sub)
    grants = repo.get_grants(7)
    assert len(grants) == 1
    assert grants[0].grant_id == GrantId("g1")
    assert grants[0].reversed is False
    restored = repo.get_subscription(7)
    assert restored is not None
    assert restored.authorized_until == _utc(2026, 2, 1)


def test_reversal_persistence_retains_row(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    grant = _grant("g1", 7, _utc(2026, 1, 1))
    repo.create_grant_with_subscription(grant, _sub(7, _utc(2026, 2, 1)))
    repo.reverse_grant_with_subscription(
        GrantId("g1"),
        reason="refund",
        reversed_at=_utc(2026, 1, 15),
        subscription=_sub(7, None),
    )
    grants = repo.get_grants(7)
    assert len(grants) == 1  # retained, not deleted
    assert grants[0].reversed is True
    assert grants[0].reversal_reason == "refund"
    sub = repo.get_subscription(7)
    assert sub is not None
    assert sub.authorized_until is None


def test_reversal_recomputed_subscription_persisted(tmp_path: Path) -> None:
    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    repo.create_grant_with_subscription(
        _grant("g1", 1, _utc(2026, 1, 1)), _sub(1, _utc(2026, 2, 1))
    )
    repo.create_grant_with_subscription(
        _grant("g2", 1, _utc(2026, 2, 1)), _sub(1, _utc(2026, 3, 1))
    )
    repo.reverse_grant_with_subscription(
        GrantId("g1"),
        reason="refund",
        reversed_at=_utc(2026, 2, 5),
        subscription=_sub(1, _utc(2026, 3, 1)),
    )
    recomputed = repo.get_subscription(1)
    assert recomputed is not None
    assert recomputed.authorized_until == _utc(2026, 3, 1)


def test_unique_source_reference_enforced_directly(tmp_path: Path) -> None:
    from telegram_media_bot.domain.errors import DuplicateEntitlementGrantError

    repo = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_plan(_plan())
    repo.create_grant_with_subscription(
        _grant("g1", 1, _utc(2026, 1, 1)), _sub(1, _utc(2026, 2, 1))
    )
    duplicate = EntitlementGrant(
        grant_id=GrantId("g2"),
        user_id=1,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        confirmed_at=_utc(2026, 1, 2),
        source_type="test",
        source_reference="order-g1",  # same as the existing grant's source
        created_at=_utc(2026, 1, 2),
    )
    with pytest.raises(DuplicateEntitlementGrantError):
        repo.create_grant_with_subscription(duplicate, _sub(1, _utc(2026, 3, 1)))


def test_legacy_database_upgrades_without_losing_jobs(tmp_path: Path) -> None:
    # Build a "legacy" database using only the job repository (simulating a pre-T014 install).
    database = tmp_path / "legacy.sqlite3"
    job_repo = SqliteJobRepository(database)
    job_repo.initialize()
    now = _utc(2026, 1, 1)
    legacy = JobRecord(
        job_id=JobId("legacy-job"),
        kind=JobKind.DOWNLOAD,
        status=JobStatus.QUEUED,
        chat_id=1,
        user_id=2,
        url="https://example.com/legacy",
        mode=None,
        idempotency_key="legacy-key",
        created_at=now,
        updated_at=now,
    )
    job_repo.create_job(legacy)

    # Run the entitlement migration on the same file.
    sub_repo = SqliteSubscriptionRepository(database)
    sub_repo.initialize()

    with closing(sqlite3.connect(database)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert "entitlement_snapshot_json" in columns
    assert {
        "subscription_plans",
        "subscription_plan_capabilities",
        "subscriptions",
        "entitlement_grants",
    } <= tables
    loaded = job_repo.get_job(JobId("legacy-job"))
    assert loaded is not None
    assert loaded.url == "https://example.com/legacy"
    assert loaded.entitlement_snapshot is None  # legacy job has no snapshot


def test_job_entitlement_snapshot_round_trip(tmp_path: Path) -> None:
    job_repo = SqliteJobRepository(tmp_path / "state" / "jobs.sqlite3")
    job_repo.initialize()
    now = _utc(2026, 1, 1)
    snapshot = EntitlementSnapshot(
        capability=VIP,
        accepted_at=_utc(2026, 1, 1, 10, 0),
        authorized_until=_utc(2026, 2, 1),
        plan_id=PlanId("vip-1"),
        grant_id=GrantId("g1"),
    )
    record = JobRecord(
        job_id=JobId("vip-job"),
        kind=JobKind.DOWNLOAD,
        status=JobStatus.QUEUED,
        chat_id=1,
        user_id=2,
        url="https://example.com/vip",
        mode=None,
        idempotency_key="vip-key",
        created_at=now,
        updated_at=now,
        entitlement_snapshot=snapshot,
    )
    job_repo.create_job(record)
    loaded = job_repo.get_job(JobId("vip-job"))
    assert loaded is not None
    assert loaded.entitlement_snapshot == snapshot
    # JSON-serializable representation is safe and replayable.
    assert loaded.entitlement_snapshot.capability is VIP
    assert loaded.entitlement_snapshot.accepted_at == _utc(2026, 1, 1, 10, 0)


def test_wal_and_concurrent_exact_once_grants(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    repo = SqliteSubscriptionRepository(database)
    repo.initialize()
    repo.save_plan(_plan())

    def create(index: int) -> bool:
        existing = repo.get_grant_by_source(1, "test", "order-shared")
        if existing is not None:
            return False
        grant = EntitlementGrant(
            grant_id=GrantId(f"g-{index}"),
            user_id=1,
            plan_id=PlanId("vip-1"),
            duration_months=1,
            confirmed_at=_utc(2026, 1, 1),
            source_type="test",
            source_reference="order-shared",
            created_at=_utc(2026, 1, 1),
        )
        try:
            repo.create_grant_with_subscription(grant, _sub(1, _utc(2026, 2, 1)))
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(32)))

    assert sum(results) == 1  # exactly one grant survived contention
    assert len(repo.get_grants(1)) == 1
    with closing(sqlite3.connect(database)) as connection:
        journal = connection.execute("PRAGMA journal_mode").fetchone()
    assert journal == ("wal",)


def test_no_existing_rows_rewritten_when_adding_tables(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job_repo = SqliteJobRepository(database)
    job_repo.initialize()
    now = _utc(2026, 1, 1)
    job_repo.create_job(
        JobRecord(
            job_id=JobId("keep-me"),
            kind=JobKind.DOWNLOAD,
            status=JobStatus.QUEUED,
            chat_id=1,
            user_id=2,
            url="https://example.com/keep",
            mode=None,
            idempotency_key="keep-key",
            created_at=now,
            updated_at=now,
        )
    )
    sub_repo = SqliteSubscriptionRepository(database)
    sub_repo.initialize()
    loaded = job_repo.get_job(JobId("keep-me"))
    assert loaded is not None
    assert loaded.idempotency_key == "keep-key"
    assert loaded.url == "https://example.com/keep"
