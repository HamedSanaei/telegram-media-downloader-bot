"""WAL-backed durable subscription, plan, and entitlement-grant store (T014).

The schema is additive and idempotent: existing databases gain four empty tables plus a nullable
job snapshot field without rewrites or deletions. The commercial plan catalog is empty by default
and no price is invented. Subscription state lives only in SQLite; Redis is never authoritative for
entitlements.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.application.ports.subscriptions import SubscriptionRepository
from telegram_media_bot.domain.errors import (
    DuplicateEntitlementGrantError,
    EntitlementGrantNotFoundError,
    PersistenceError,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    GrantId,
    PlanId,
    Subscription,
    SubscriptionPlan,
)

# Concrete field documentation for the T014 migration (purpose / sensitivity / nullability /
# index / unique / retention / backward compatibility). Tables are created only when
# ``SqliteSubscriptionRepository.initialize()`` runs; the jobs snapshot column is a nullable
# additive field served from the JobRepository bootstrap.
#
# subscription_plans
#   plan_id           PK; stable/unique immutable operator-owned plan identity. NOT sensitive.
#   name              display caption; operator-owned; NOT sensitive.
#   duration_months   positive integer calendar months; financial, NOT secret.
#   price_minor       integer minor units; financial, NOT secret. No floating-point money.
#   currency          normalized uppercase 3-letter code; financial, NOT secret.
#   enabled           operator switch; NOT sensitive.
#   created_at        UTC row timestamp. Index: PK(plan_id). Retention: indefinite; table starts
#                     empty and no commercial row is seeded. Backward compatible (new table only).
#
# subscription_plan_capabilities
#   plan_id / capability  typed grant-to-capability link; NOT sensitive. Retained with the plan.
#   PRIMARY KEY (plan_id, capability) => one row per (plan, capability); FK cascades with the plan.
#
# subscriptions
#   user_id           PK; durable account projection; NOT sensitive.
#   authorized_until  recomputed UTC expiry from valid grants; nullable when no paid time remains.
#   cancelled_at      nullable UTC timestamp of operator/user cancellation.
#   updated_at        UTC row timestamp. Index: (authorized_until). Retention: indefinite audit.
#                     Free users require no row at all.
#
# entitlement_grants
#   grant_id          PK; stable immutable ledger identity; NOT sensitive.
#   user_id           durable owner; NOT a metric label.
#   plan_id           referenced plan; stable; NOT a metric label.
#   duration_months   positive integer calendar months.
#   confirmed_at      UTC payment-confirmation instant (deterministic chain anchor).
#   source_type       provider-neutral economic-source type (future provider/order namespace).
#   source_reference  provider-neutral economic-order reference.
#   created_at        UTC row timestamp.
#   reversed_at       UTC nullable; a reversed grant is retained, never deleted.
#   reversal_reason   nullable audit reason; no secret by policy.
#   Indexes: UNIQUE (user_id, source_type, source_reference) enforces exactly-once creation for a
#     future billing provider without redesign. (user_id, confirmed_at) serves confirmation-order
#     reads. Retention: indefinite immutable economic/audit rows; never enters media cleanup.


class SqliteSubscriptionRepository(SubscriptionRepository):
    """WAL-backed subscription/entitlement store sharing the bot/worker database."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("Subscription store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscription_plans (
                    plan_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    duration_months INTEGER NOT NULL CHECK (duration_months > 0),
                    price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                    currency TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscription_plan_capabilities (
                    plan_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    PRIMARY KEY (plan_id, capability),
                    FOREIGN KEY (plan_id) REFERENCES subscription_plans(plan_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    authorized_until TEXT,
                    cancelled_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS subscriptions_authorized_until_idx
                    ON subscriptions(authorized_until);

                CREATE TABLE IF NOT EXISTS entitlement_grants (
                    grant_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    plan_id TEXT NOT NULL,
                    duration_months INTEGER NOT NULL CHECK (duration_months > 0),
                    confirmed_at TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reversed_at TEXT,
                    reversal_reason TEXT,
                    FOREIGN KEY (plan_id) REFERENCES subscription_plans(plan_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS entitlement_grants_source_uidx
                    ON entitlement_grants(user_id, source_type, source_reference);
                CREATE INDEX IF NOT EXISTS entitlement_grants_user_confirmed_idx
                    ON entitlement_grants(user_id, confirmed_at);
                """
            )

    def save_plan(self, plan: SubscriptionPlan) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO subscription_plans (
                    plan_id, name, duration_months, price_minor, currency, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(plan.plan_id),
                    plan.name,
                    plan.duration_months,
                    plan.price_minor,
                    plan.currency,
                    int(plan.enabled),
                    _now_text(),
                ),
            )
            connection.execute(
                "DELETE FROM subscription_plan_capabilities WHERE plan_id = ?",
                (str(plan.plan_id),),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO subscription_plan_capabilities (plan_id, capability) "
                "VALUES (?, ?)",
                [(str(plan.plan_id), capability.value) for capability in plan.capabilities],
            )
            connection.execute("COMMIT")

    def get_plan(self, plan_id: PlanId) -> SubscriptionPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_plans WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
            if row is None:
                return None
            capabilities = _load_plan_capabilities(connection, plan_id)
        return _plan_from_row(row, capabilities)

    def get_grants(self, user_id: int) -> tuple[EntitlementGrant, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entitlement_grants WHERE user_id = ? ORDER BY confirmed_at ASC",
                (user_id,),
            ).fetchall()
        return tuple(_grant_from_row(row) for row in rows)

    def get_grant_by_source(
        self,
        user_id: int,
        source_type: str,
        source_reference: str,
    ) -> EntitlementGrant | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM entitlement_grants
                WHERE user_id = ? AND source_type = ? AND source_reference = ?
                """,
                (user_id, source_type, source_reference),
            ).fetchone()
        return _grant_from_row(row) if row is not None else None

    def get_grant(self, grant_id: GrantId) -> EntitlementGrant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entitlement_grants WHERE grant_id = ?", (str(grant_id),)
            ).fetchone()
        return _grant_from_row(row) if row is not None else None

    def create_grant_with_subscription(
        self,
        grant: EntitlementGrant,
        subscription: Subscription,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO entitlement_grants (
                        grant_id, user_id, plan_id, duration_months, confirmed_at,
                        source_type, source_reference, created_at, reversed_at, reversal_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(grant.grant_id),
                        grant.user_id,
                        str(grant.plan_id),
                        grant.duration_months,
                        _dump_datetime(grant.confirmed_at),
                        grant.source_type,
                        grant.source_reference,
                        _dump_datetime(grant.created_at),
                        None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                if "UNIQUE constraint failed" in str(exc):
                    raise DuplicateEntitlementGrantError(
                        "A grant for this economic source already exists"
                    ) from exc
                raise PersistenceError("Subscription store operation failed") from exc
            _save_subscription(connection, subscription)
            connection.execute("COMMIT")

    def reverse_grant_with_subscription(
        self,
        grant_id: GrantId,
        *,
        reason: str,
        reversed_at: datetime,
        subscription: Subscription,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE entitlement_grants SET reversed_at = ?, reversal_reason = ?
                WHERE grant_id = ? AND reversed_at IS NULL
                """,
                (_dump_datetime(reversed_at), reason, str(grant_id)),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT 1 FROM entitlement_grants WHERE grant_id = ?", (str(grant_id),)
                ).fetchone()
                if existing is None:
                    connection.execute("ROLLBACK")
                    raise EntitlementGrantNotFoundError("Grant does not exist")
                # Already reversed; storing the recomputed projection is idempotent.
            _save_subscription(connection, subscription)
            connection.execute("COMMIT")

    def get_subscription(self, user_id: int) -> Subscription | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return Subscription(
            user_id=int(row["user_id"]),
            authorized_until=(
                _load_datetime(str(row["authorized_until"])) if row["authorized_until"] else None
            ),
            cancelled_at=(
                _load_datetime(str(row["cancelled_at"])) if row["cancelled_at"] else None
            ),
            updated_at=_load_datetime(str(row["updated_at"])),
        )

    def cancel_subscription(self, user_id: int, *, cancelled_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO subscriptions (user_id, authorized_until, cancelled_at, updated_at)
                VALUES (?, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    cancelled_at = excluded.cancelled_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, _dump_datetime(cancelled_at), _dump_datetime(cancelled_at)),
            )
            connection.execute("COMMIT")


def _load_plan_capabilities(
    connection: sqlite3.Connection, plan_id: PlanId
) -> frozenset[Capability]:
    rows = connection.execute(
        "SELECT capability FROM subscription_plan_capabilities WHERE plan_id = ?",
        (str(plan_id),),
    ).fetchall()
    capabilities: set[Capability] = set()
    for row in rows:
        try:
            capabilities.add(Capability(str(row["capability"])))
        except ValueError:
            continue
    return frozenset(capabilities)


def _save_subscription(connection: sqlite3.Connection, subscription: Subscription) -> None:
    connection.execute(
        """
        INSERT INTO subscriptions (user_id, authorized_until, cancelled_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            authorized_until = excluded.authorized_until,
            cancelled_at = excluded.cancelled_at,
            updated_at = excluded.updated_at
        """,
        (
            subscription.user_id,
            _dump_datetime(subscription.authorized_until)
            if subscription.authorized_until
            else None,
            _dump_datetime(subscription.cancelled_at) if subscription.cancelled_at else None,
            _dump_datetime(subscription.updated_at),
        ),
    )


def _plan_from_row(row: sqlite3.Row, capabilities: frozenset[Capability]) -> SubscriptionPlan:
    return SubscriptionPlan(
        plan_id=PlanId(str(row["plan_id"])),
        name=str(row["name"]),
        duration_months=int(row["duration_months"]),
        price_minor=int(row["price_minor"]),
        currency=str(row["currency"]),
        enabled=bool(row["enabled"]),
        capabilities=capabilities,
    )


def _grant_from_row(row: sqlite3.Row) -> EntitlementGrant:
    return EntitlementGrant(
        grant_id=GrantId(str(row["grant_id"])),
        user_id=int(row["user_id"]),
        plan_id=PlanId(str(row["plan_id"])),
        duration_months=int(row["duration_months"]),
        confirmed_at=_load_datetime(str(row["confirmed_at"])),
        source_type=str(row["source_type"]),
        source_reference=str(row["source_reference"]),
        created_at=_load_datetime(str(row["created_at"])),
        reversed_at=_load_datetime(str(row["reversed_at"])) if row["reversed_at"] else None,
        reversal_reason=str(row["reversal_reason"]) if row["reversal_reason"] else None,
    )


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _now_text() -> str:
    return _dump_datetime(datetime.now(UTC))


# --------------------------------------------------------------------------- #
# Connection-scoped helpers for the T015 atomic billing transaction
# --------------------------------------------------------------------------- #
#
# T015 confirmation/refund must update payment rows AND create/reverse an entitlement grant AND
# rewrite the subscription projection in ONE ``BEGIN IMMEDIATE`` transaction. These helpers operate
# on a caller-supplied open ``sqlite3.Connection`` (the payment repository's transaction) so T014's
# recomputation rules stay authoritative and are not duplicated in the billing layer.


def load_grants_on_connection(
    connection: sqlite3.Connection,
    user_id: int,
) -> tuple[EntitlementGrant, ...]:
    rows = connection.execute(
        "SELECT * FROM entitlement_grants WHERE user_id = ? ORDER BY confirmed_at ASC",
        (user_id,),
    ).fetchall()
    return tuple(_grant_from_row(row) for row in rows)


def load_all_grants_on_connection(
    connection: sqlite3.Connection,
) -> tuple[EntitlementGrant, ...]:
    rows = connection.execute(
        "SELECT * FROM entitlement_grants ORDER BY confirmed_at ASC"
    ).fetchall()
    return tuple(_grant_from_row(row) for row in rows)


def insert_grant_on_connection(
    connection: sqlite3.Connection,
    grant: EntitlementGrant,
) -> None:
    connection.execute(
        """
        INSERT INTO entitlement_grants (
            grant_id, user_id, plan_id, duration_months, confirmed_at,
            source_type, source_reference, created_at, reversed_at, reversal_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(grant.grant_id),
            grant.user_id,
            str(grant.plan_id),
            grant.duration_months,
            _dump_datetime(grant.confirmed_at),
            grant.source_type,
            grant.source_reference,
            _dump_datetime(grant.created_at),
            None,
            None,
        ),
    )


def recompute_subscription_on_connection(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    now: datetime,
) -> Subscription:
    """Recompute and persist the subscription projection from all stored grants.

    Uses T014's calendar-month recomputation rules and always returns the derived projection (a
    user with no valid paid time simply has an authorized window of ``None`` on their retained
    account row).
    """
    grants = load_grants_on_connection(connection, user_id)
    from telegram_media_bot.domain.subscriptions import compute_authorized_until

    authorized_until = compute_authorized_until(grants)
    subscription = Subscription(
        user_id=user_id,
        authorized_until=authorized_until,
        cancelled_at=None,
        updated_at=now,
    )
    # Persist the derived projection. The account row, once it exists, is retained for audit; a
    # user with no grants and no paid time simply has an empty authorized window. (Free users who
    # were never granted anything still have no subscription row created here: a paid flow only
    # reaches this path after a grant insert.)
    _save_subscription(connection, subscription)
    return subscription


def mark_grant_reversed_on_connection(
    connection: sqlite3.Connection,
    grant_id: GrantId,
    *,
    reason: str,
    reversed_at: datetime,
) -> int:
    """Mark one grant reversed without deleting its row; returns the number of rows changed."""
    cursor = connection.execute(
        "UPDATE entitlement_grants SET reversed_at = ?, reversal_reason = ? "
        "WHERE grant_id = ? AND reversed_at IS NULL",
        (_dump_datetime(reversed_at), reason, str(grant_id)),
    )
    return cursor.rowcount
