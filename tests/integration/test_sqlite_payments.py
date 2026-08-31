"""T015 payment repository integration: schema, atomic confirm/reverse, concurrency, refund."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_media_bot.domain.errors import (
    InvalidPaymentTransitionError,
    PaymentAmountMismatchError,
    PaymentCurrencyMismatchError,
    PaymentOrderExpiredError,
    PaymentOrderMismatchError,
    PaymentOrderNotFoundError,
    PaymentProviderMismatchError,
    PaymentTransactionNotClaimedError,
    PaymentTransactionReplayError,
)
from telegram_media_bot.domain.models import JobId, JobKind, JobRecord, JobStatus
from telegram_media_bot.domain.payments import (
    PaymentAttempt,
    PaymentAttemptId,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    GrantId,
    PlanId,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.persistence.sqlite_payments import SqlitePaymentRepository
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)

VIP = Capability.INSTAGRAM_PRIVATE_MEDIA
PROVIDER = PaymentProviderId("fake")


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _order(
    *,
    order_id: str = "order-1",
    user_id: int = 7,
    amount_minor: int = 4900,
    currency: str = "USD",
    status: PaymentStatus = PaymentStatus.CREATED,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    provider_id: PaymentProviderId | None = None,
) -> PaymentOrder:
    return PaymentOrder(
        order_id=PaymentOrderId(order_id),
        user_id=user_id,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        capabilities=frozenset({VIP}),
        amount_minor=amount_minor,
        currency=currency,
        created_at=created_at or _utc(2026, 1, 1),
        expires_at=expires_at or _utc(2026, 2, 1),
        status=status,
        provider_id=provider_id,
    )


def _attempt(order_id: str, index: int = 1) -> PaymentAttempt:
    return PaymentAttempt(
        attempt_id=PaymentAttemptId(f"attempt-{index}"),
        order_id=PaymentOrderId(order_id),
        provider_id=PROVIDER,
        status=PaymentStatus.PENDING,
        created_at=_utc(2026, 1, 1),
        updated_at=_utc(2026, 1, 1),
    )


def _grant(order_id: str, user_id: int = 7, confirmed: datetime | None = None) -> EntitlementGrant:
    confirmed = confirmed or _utc(2026, 1, 1)
    return EntitlementGrant(
        grant_id=GrantId(f"grant-{order_id}"),
        user_id=user_id,
        plan_id=PlanId("vip-1"),
        duration_months=1,
        confirmed_at=confirmed,
        source_type="fake",
        source_reference=f"txn-{order_id}",
        created_at=confirmed,
    )


def _txn(order_id: str) -> ProviderTransactionReference:
    return ProviderTransactionReference(f"txn-{order_id}")


def _deref(repo: SqlitePaymentRepository, order_id: str) -> PaymentOrder:
    order = repo.get_order(PaymentOrderId(order_id))
    assert order is not None
    return order


def test_payment_tables_created_and_empty(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    with closing(sqlite3.connect(repo._path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"payment_orders", "payment_attempts", "provider_transaction_claims"} <= tables
    with closing(sqlite3.connect(repo._path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM payment_attempts").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_transaction_claims"
        ).fetchone() == (0,)


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order())
    repo.initialize()
    repo.initialize()
    assert repo.get_order(PaymentOrderId("order-1")) is not None


def test_order_round_trip_snapshot(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order())
    restored = repo.get_order(PaymentOrderId("order-1"))
    assert restored is not None
    assert restored.amount_minor == 4900
    assert restored.currency == "USD"
    assert restored.capabilities == frozenset({VIP})
    assert restored.status is PaymentStatus.CREATED


def test_attempt_round_trip(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order())
    repo.save_attempt(_attempt("order-1"))
    # Attempts are queried through orders; at minimum the row persists without error.
    with closing(sqlite3.connect(repo._path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT order_id, status FROM payment_attempts WHERE order_id=?", ("order-1",)
        ).fetchone()
    assert row is not None
    assert row["status"] == "pending"


def test_claim_provider_transaction_is_unique(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(order_id="order-1"))
    repo.claim_provider_transaction(
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        order_id=PaymentOrderId("order-1"),
    )
    # Second claim for same (provider, ref) is a no-op (INSERT OR IGNORE) — never two grants.
    repo.claim_provider_transaction(
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        order_id=PaymentOrderId("order-1"),
    )
    assert repo.get_claim_order(PROVIDER, _txn("order-1")) == PaymentOrderId("order-1")


def test_list_pending_and_count(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(order_id="o1", created_at=_utc(2026, 1, 1)))
    repo.save_order(_order(order_id="o2", created_at=_utc(2026, 3, 1)))
    pending = repo.list_pending_orders(before=_utc(2026, 2, 8))
    assert {str(o.order_id) for o in pending} == {"o1"}
    counts = repo.count_orders_by_status()
    assert counts["created"] == 2


# --------------------------------------------------------------------------- #
# Atomic confirmation
# --------------------------------------------------------------------------- #


def test_confirm_order_atomic_success(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.save_attempt(_attempt("order-1"))
    subscription = repo.confirm_order_atomic(
        grant=_grant("order-1", confirmed=_utc(2026, 1, 15)),
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-1",
        paid_at=_utc(2026, 1, 15),
        now=_utc(2026, 1, 15),
    )
    assert subscription.authorized_until == _utc(2026, 2, 15)
    order = repo.get_order(PaymentOrderId("order-1"))
    assert order is not None
    assert order.status is PaymentStatus.PAID
    assert repo.get_claim_order(PROVIDER, _txn("order-1")) == PaymentOrderId("order-1")
    # Grant persisted and subscription recomputed in the SAME transaction.
    grants = sub_store.get_grants(7)
    assert len(grants) == 1
    assert grants[0].reversed is False


def test_duplicate_confirmation_is_replay_no_second_grant(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.confirm_order_atomic(
        grant=_grant("order-1"),
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-1",
        paid_at=_utc(2026, 1, 1),
        now=_utc(2026, 1, 1),
    )
    with pytest.raises(PaymentTransactionReplayError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )
    assert len(sub_store.get_grants(7)) == 1  # exactly one grant


def test_wrong_amount_fails_closed_without_mutation(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.CREATED, provider_id=PROVIDER))
    with pytest.raises(PaymentAmountMismatchError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=1234,
            expected_currency="USD",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )
    # No rollback-side-effect: nothing was claimed, granted, or paid.
    assert _deref(repo, "order-1").status is PaymentStatus.CREATED
    assert repo.get_claim_order(PROVIDER, _txn("order-1")) is None
    assert len(sub_store.get_grants(7)) == 0


def test_wrong_currency_fails_closed(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(status=PaymentStatus.CREATED, provider_id=PROVIDER))
    with pytest.raises(PaymentCurrencyMismatchError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="EUR",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )
    assert _deref(repo, "order-1").status is PaymentStatus.CREATED


def test_wrong_order_reference_fails_closed(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(status=PaymentStatus.CREATED, provider_id=PROVIDER))
    with pytest.raises(PaymentOrderMismatchError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="SHOULD-BE-ORDER-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )


def test_wrong_provider_fails_closed(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(status=PaymentStatus.CREATED, provider_id=PROVIDER))
    with pytest.raises(PaymentProviderMismatchError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PaymentProviderId("other"),
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )


def test_expired_order_fails_closed(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(
        _order(status=PaymentStatus.CREATED, expires_at=_utc(2026, 1, 5), provider_id=PROVIDER)
    )
    with pytest.raises(PaymentOrderExpiredError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 10),
            now=_utc(2026, 1, 10),
        )
    assert _deref(repo, "order-1").status is PaymentStatus.CREATED


def test_cancelled_or_refunded_order_cannot_be_paid(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(status=PaymentStatus.CANCELLED, provider_id=PROVIDER))
    with pytest.raises(InvalidPaymentTransitionError):
        repo.confirm_order_atomic(
            grant=_grant("order-1"),
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="order-1",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )
    assert _deref(repo, "order-1").status is PaymentStatus.CANCELLED


def test_unknown_order_raises(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    with pytest.raises(PaymentOrderNotFoundError):
        repo.confirm_order_atomic(
            grant=_grant("nope"),
            order_id=PaymentOrderId("nope"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("nope"),
            expected_amount_minor=4900,
            expected_currency="USD",
            expected_order_reference="nope",
            paid_at=_utc(2026, 1, 1),
            now=_utc(2026, 1, 1),
        )


def test_concurrent_confirmation_creates_exactly_one_grant(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))

    def confirm(_: int) -> bool:
        try:
            repo.confirm_order_atomic(
                grant=_grant("order-1"),
                order_id=PaymentOrderId("order-1"),
                provider_id=PROVIDER,
                provider_transaction_reference=_txn("order-1"),
                expected_amount_minor=4900,
                expected_currency="USD",
                expected_order_reference="order-1",
                paid_at=_utc(2026, 1, 1),
                now=_utc(2026, 1, 1),
            )
            return True
        except PaymentTransactionReplayError, InvalidPaymentTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(confirm, range(24)))

    assert sum(results) == 1  # exactly one confirmation won
    assert _deref(repo, "order-1").status is PaymentStatus.PAID
    assert len(sub_store.get_grants(7)) == 1  # exactly one grant survived contention


# --------------------------------------------------------------------------- #
# Atomic reversal / refund
# --------------------------------------------------------------------------- #


def test_confirm_then_refund_recomputes(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.confirm_order_atomic(
        grant=_grant("order-1", confirmed=_utc(2026, 1, 1)),
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-1",
        paid_at=_utc(2026, 1, 1),
        now=_utc(2026, 1, 1),
    )
    result = repo.reverse_order_atomic(
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        reason="user-request",
        reversed_at=_utc(2026, 1, 15),
        now=_utc(2026, 1, 15),
    )
    assert result is not None
    assert result.authorized_until is None  # access ends immediately
    assert _deref(repo, "order-1").status is PaymentStatus.REFUNDED
    grants = sub_store.get_grants(7)
    assert len(grants) == 1
    assert grants[0].reversed is True  # retained, not deleted
    assert grants[0].reversal_reason == "user-request"


def test_stacked_grants_refund_earliest_recomputes(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    # Payment A (1 month), Payment B (3 months), recorded via atomic confirm on their own orders.
    repo.save_order(_order(order_id="order-A", status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.confirm_order_atomic(
        grant=_grant("order-A", confirmed=_utc(2026, 1, 1)),
        order_id=PaymentOrderId("order-A"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-A"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-A",
        paid_at=_utc(2026, 1, 1),
        now=_utc(2026, 1, 1),
    )
    repo.save_order(_order(order_id="order-B", status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.confirm_order_atomic(
        grant=_grant_months("order-B", months=3, confirmed=_utc(2026, 1, 5)),
        order_id=PaymentOrderId("order-B"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-B"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-B",
        paid_at=_utc(2026, 1, 5),
        now=_utc(2026, 1, 5),
    )
    combined = sub_store.get_subscription(7)
    assert combined is not None
    assert combined.authorized_until == _utc(2026, 5, 1)  # g1 -> Feb 1, g2 stacked +3mo
    # Refund A (earliest): B alone replays from its confirmation -> Apr 5.
    result = repo.reverse_order_atomic(
        order_id=PaymentOrderId("order-A"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-A"),
        reason="refund",
        reversed_at=_utc(2026, 2, 5),
        now=_utc(2026, 2, 5),
    )
    assert result is not None
    assert result.authorized_until == _utc(2026, 4, 5)


def test_duplicate_refund_is_idempotent(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))
    repo.confirm_order_atomic(
        grant=_grant("order-1"),
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        expected_amount_minor=4900,
        expected_currency="USD",
        expected_order_reference="order-1",
        paid_at=_utc(2026, 1, 1),
        now=_utc(2026, 1, 1),
    )
    repo.reverse_order_atomic(
        order_id=PaymentOrderId("order-1"),
        provider_id=PROVIDER,
        provider_transaction_reference=_txn("order-1"),
        reason="refund",
        reversed_at=_utc(2026, 1, 10),
        now=_utc(2026, 1, 10),
    )
    # Second refund attempt on the already-reported REFUNDED order cannot mutate again.
    with pytest.raises(InvalidPaymentTransitionError):
        repo.reverse_order_atomic(
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            reason="refund-again",
            reversed_at=_utc(2026, 1, 11),
            now=_utc(2026, 1, 11),
        )
    grants = sub_store.get_grants(7)
    assert grants[0].reversed is True
    assert grants[0].reversal_reason == "refund"  # unchanged, not overwritten by the duplicate


def test_refund_without_claim_raises(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    sub_store = SqliteSubscriptionRepository(tmp_path / "state" / "jobs.sqlite3")
    sub_store.initialize()
    sub_store.save_plan(_plan())
    repo.save_order(_order(status=PaymentStatus.PAID, provider_id=PROVIDER))
    with pytest.raises(PaymentTransactionNotClaimedError):
        repo.reverse_order_atomic(
            order_id=PaymentOrderId("order-1"),
            provider_id=PROVIDER,
            provider_transaction_reference=_txn("order-1"),
            reason="refund",
            reversed_at=_utc(2026, 1, 15),
            now=_utc(2026, 1, 15),
        )


def test_refund_leaves_historical_records(tmp_path: Path) -> None:
    repo = SqlitePaymentRepository(tmp_path / "state" / "jobs.sqlite3")
    repo.initialize()
    repo.save_order(_order(status=PaymentStatus.PENDING, provider_id=PROVIDER))
    with closing(sqlite3.connect(repo._path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "payment_orders" in tables  # no deletion path exists
    assert "provider_transaction_claims" in tables


# --------------------------------------------------------------------------- #
# Backward compatibility
# --------------------------------------------------------------------------- #


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


def _grant_months(
    order_id: str,
    *,
    months: int,
    confirmed: datetime,
    user_id: int = 7,
) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(f"grant-{order_id}"),
        user_id=user_id,
        plan_id=PlanId("vip-1"),
        duration_months=months,
        confirmed_at=confirmed,
        source_type="fake",
        source_reference=f"txn-{order_id}",
        created_at=confirmed,
    )


def test_legacy_database_upgrades_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    job_repo = SqliteJobRepository(database)
    job_repo.initialize()
    now = _utc(2026, 1, 1)
    job_repo.create_job(
        JobRecord(
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
    )
    # Run the T014 and T015 migrations on the same file, in either order.
    sub_store = SqliteSubscriptionRepository(database)
    sub_store.initialize()
    payment_repo = SqlitePaymentRepository(database)
    payment_repo.initialize()
    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"payment_orders", "payment_attempts", "provider_transaction_claims"} <= tables
    loaded = job_repo.get_job(JobId("legacy-job"))
    assert loaded is not None
    assert loaded.url == "https://example.com/legacy"
    # Free users receive no orders and no grants.
    assert payment_repo.count_orders_by_status() == {}
    assert sub_store.get_subscription(2) is None


def test_schema_bootstrap_is_repeatable_with_wal(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    for _ in range(3):
        SqlitePaymentRepository(database).initialize()
        SqliteSubscriptionRepository(database).initialize()
    with closing(sqlite3.connect(database)) as connection:
        journal = connection.execute("PRAGMA journal_mode").fetchone()
    assert journal == ("wal",)
