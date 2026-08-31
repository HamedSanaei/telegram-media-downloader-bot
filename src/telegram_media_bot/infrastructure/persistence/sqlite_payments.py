"""WAL-backed durable payment order/attempt/provider-transaction store (T015).

The schema is additive and idempotent: existing databases gain payment tables/indexes without
rewrites or deletions. No real gateway is selected and no pricing is invented; the commercial plan
catalog remains operator-owned and empty by default.

Financial rows are durable audit/economic records. They must never be deleted by job/media/workspace
purge or logger retention; this module provides no deletion path for them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.application.ports.payments import PaymentRepository
from telegram_media_bot.domain.errors import (
    InvalidPaymentTransitionError,
    PaymentAmountMismatchError,
    PaymentBackendError,
    PaymentCurrencyMismatchError,
    PaymentOrderExpiredError,
    PaymentOrderMismatchError,
    PaymentOrderNotFoundError,
    PaymentProviderMismatchError,
    PaymentTransactionNotClaimedError,
    PaymentTransactionReplayError,
    PersistenceError,
)
from telegram_media_bot.domain.payments import (
    PaymentAttempt,
    PaymentOrder,
    PaymentOrderId,
    PaymentProviderId,
    PaymentStatus,
    ProviderTransactionReference,
    payment_status_transition,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    PlanId,
    Subscription,
)
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    insert_grant_on_connection,
    load_grants_on_connection,
    mark_grant_reversed_on_connection,
    recompute_subscription_on_connection,
)

# --------------------------------------------------------------------------- #
# Concrete field documentation for the T015 migration (purpose / sensitivity /
# nullability / index / unique / retention / backward compatibility).
#
# payment_orders
#   order_id            PK; stable unique order identity. NOT sensitive.
#   user_id             durable owner. Indexed; never a metric label.
#   plan_id             referenced operator plan; stable. NOT a metric label.
#   duration_months     positive integer calendar months.
#   capabilities_json   JSON list of capability strings snapshotted at creation (typed, NOT secret).
#   amount_minor        integer minor units; financial, NOT a secret. No floating-point money.
#   currency            normalized uppercase 3-letter code; financial.
#   created_at          UTC row timestamp.
#   expires_at          UTC deterministic expiry; NOT config-priced.
#   status              normalized PaymentStatus value.
#   provider_id         nullable bounded provider identity when the order is routed.
#   Indexes: (user_id), (status), (created_at), (expires_at). Retention indefinite.
#
# payment_attempts
#   attempt_id          PK; durable interaction identity.
#   order_id            FK -> payment_orders; NOT a metric label.
#   provider_id         bounded provider identity (nullable when created before routing).
#   status              normalized attempt status.
#   failure_code        nullable safe failure category/code; NEVER raw provider payload.
#   created_at / updated_at  UTC timestamps.
#   Indexes: (order_id), (provider_id, status). Retention indefinite.
#
# provider_transaction_claims
#   provider_id                      bounded provider identity.
#   provider_transaction_reference   durable exactly-once financial identity.
#   order_id                         claimed order.
#   claimed_at                       UTC claim timestamp.
#   PRIMARY KEY (provider_id, provider_transaction_reference) => exactly-once economic identity.
#   Unique provider references are retention-indefinite and never reused.
# --------------------------------------------------------------------------- #


class SqlitePaymentRepository(PaymentRepository):
    """WAL-backed payment store sharing the bot/worker database."""

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
            raise PersistenceError("Payment store operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS payment_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    plan_id TEXT NOT NULL,
                    duration_months INTEGER NOT NULL CHECK (duration_months > 0),
                    capabilities_json TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_id TEXT
                );
                CREATE INDEX IF NOT EXISTS payment_orders_user_idx ON payment_orders(user_id);
                CREATE INDEX IF NOT EXISTS payment_orders_status_idx ON payment_orders(status);
                CREATE INDEX IF NOT EXISTS payment_orders_created_idx ON payment_orders(created_at);
                CREATE INDEX IF NOT EXISTS payment_orders_expires_idx ON payment_orders(expires_at);

                CREATE TABLE IF NOT EXISTS payment_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    provider_id TEXT,
                    status TEXT NOT NULL,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES payment_orders(order_id)
                );
                CREATE INDEX IF NOT EXISTS payment_attempts_order_idx ON payment_attempts(order_id);
                CREATE INDEX IF NOT EXISTS payment_attempts_provider_status_idx
                    ON payment_attempts(provider_id, status);

                CREATE TABLE IF NOT EXISTS provider_transaction_claims (
                    provider_id TEXT NOT NULL,
                    provider_transaction_reference TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (provider_id, provider_transaction_reference),
                    FOREIGN KEY (order_id) REFERENCES payment_orders(order_id)
                );
                """
            )

    # -- read/write primitives ----------------------------------------------

    def save_order(self, order: PaymentOrder) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO payment_orders (
                    order_id, user_id, plan_id, duration_months, capabilities_json,
                    amount_minor, currency, created_at, expires_at, status, provider_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(order.order_id),
                    order.user_id,
                    str(order.plan_id),
                    order.duration_months,
                    _dump_capabilities(order.capabilities),
                    order.amount_minor,
                    order.currency,
                    _dump_datetime(order.created_at),
                    _dump_datetime(order.expires_at),
                    order.status.value,
                    str(order.provider_id) if order.provider_id else None,
                ),
            )
            connection.execute("COMMIT")

    def get_order(self, order_id: PaymentOrderId) -> PaymentOrder | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?", (str(order_id),)
            ).fetchone()
        return _order_from_row(row) if row is not None else None

    def save_attempt(self, attempt: PaymentAttempt) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO payment_attempts (
                    attempt_id, order_id, provider_id, status, failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(attempt.attempt_id),
                    str(attempt.order_id),
                    str(attempt.provider_id) if attempt.provider_id else None,
                    attempt.status.value,
                    attempt.failure_code,
                    _dump_datetime(attempt.created_at),
                    _dump_datetime(attempt.updated_at),
                ),
            )
            connection.execute("COMMIT")

    def list_orders_by_user(self, user_id: int) -> tuple[PaymentOrder, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM payment_orders WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        return tuple(_order_from_row(row) for row in rows)

    def list_pending_orders(self, *, before: datetime) -> tuple[PaymentOrder, ...]:
        """Durable CREATED/PENDING orders created before ``before`` (reconciliation foundation).

        ``before`` bounds by creation time, not expiry; an order awaiting or stuck in checkout is
        returned so a future reconciliation worker (T025) can act on it deterministically.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM payment_orders WHERE status IN (?, ?) AND created_at < ? "
                "ORDER BY created_at ASC",
                (
                    PaymentStatus.CREATED.value,
                    PaymentStatus.PENDING.value,
                    _dump_datetime(before),
                ),
            ).fetchall()
        return tuple(_order_from_row(row) for row in rows)

    def count_orders_by_status(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM payment_orders GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    # -- provider transaction exactly-once identity --------------------------

    def claim_provider_transaction(
        self,
        *,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        order_id: PaymentOrderId,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_transaction_claims (
                    provider_id, provider_transaction_reference, order_id, claimed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    str(provider_id),
                    str(provider_transaction_reference),
                    str(order_id),
                    _dump_datetime(datetime.now(UTC)),
                ),
            )
            connection.execute("COMMIT")

    def get_claim_order(
        self,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
    ) -> PaymentOrderId | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT order_id FROM provider_transaction_claims "
                "WHERE provider_id = ? AND provider_transaction_reference = ?",
                (str(provider_id), str(provider_transaction_reference)),
            ).fetchone()
        return PaymentOrderId(str(row["order_id"])) if row is not None else None

    # -- atomic confirmation / reversal --------------------------------------

    def confirm_order_atomic(
        self,
        *,
        grant: EntitlementGrant,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        expected_amount_minor: int,
        expected_currency: str,
        expected_order_reference: str,
        paid_at: datetime,
        now: datetime,
    ) -> Subscription:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                order = self._get_order_on_connection(connection, order_id)
                if order is None:
                    raise PaymentOrderNotFoundError("Payment order does not exist")
                # Order identity mapping against the verified reference.
                if str(order.order_id) != expected_order_reference:
                    raise PaymentOrderMismatchError("Verified order reference does not match")
                if str(order.provider_id) != str(provider_id):
                    raise PaymentProviderMismatchError("Verified provider does not match order")
                if order.amount_minor != expected_amount_minor:
                    raise PaymentAmountMismatchError(
                        "Verified amount does not match order snapshot"
                    )
                if order.currency != expected_currency:
                    raise PaymentCurrencyMismatchError(
                        "Verified currency does not match order snapshot"
                    )
                if paid_at > order.expires_at:
                    raise PaymentOrderExpiredError("Payment order has expired")
                # Exactly-once transaction identity: a reused provider reference is a replay.
                claim = connection.execute(
                    "SELECT order_id FROM provider_transaction_claims "
                    "WHERE provider_id = ? AND provider_transaction_reference = ?",
                    (str(provider_id), str(provider_transaction_reference)),
                ).fetchone()
                if claim is not None:
                    if str(claim["order_id"]) != str(order_id):
                        raise PaymentTransactionReplayError(
                            "Provider transaction already mapped to another order"
                        )
                    raise PaymentTransactionReplayError(
                        "Provider transaction already processed for this order"
                    )
                payment_status_transition(order.status, PaymentStatus.PAID)
                # Claim + transition + attempt + grant + recompute share this transaction.
                connection.execute(
                    """
                    INSERT INTO provider_transaction_claims (
                        provider_id, provider_transaction_reference, order_id, claimed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(provider_id),
                        str(provider_transaction_reference),
                        str(order_id),
                        _dump_datetime(now),
                    ),
                )
                connection.execute(
                    "UPDATE payment_orders SET status = ?, provider_id = ? WHERE order_id = ?",
                    (PaymentStatus.PAID.value, str(provider_id), str(order_id)),
                )
                connection.execute(
                    """
                    UPDATE payment_attempts
                    SET status = ?, updated_at = ?
                    WHERE order_id = ?
                    """,
                    (PaymentStatus.PAID.value, _dump_datetime(now), str(order_id)),
                )
                insert_grant_on_connection(connection, grant)
                subscription = recompute_subscription_on_connection(
                    connection, user_id=grant.user_id, now=now
                )
                connection.execute("COMMIT")
                return subscription
            except (
                PaymentOrderNotFoundError,
                PaymentOrderExpiredError,
                PaymentAmountMismatchError,
                PaymentCurrencyMismatchError,
                PaymentProviderMismatchError,
                PaymentOrderMismatchError,
                PaymentTransactionReplayError,
                InvalidPaymentTransitionError,
            ) as exc:
                connection.execute("ROLLBACK")
                raise exc
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise PaymentTransactionReplayError(
                    "Provider transaction already processed"
                ) from exc
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise PaymentBackendError("Payment backend is unavailable") from exc

    def reverse_order_atomic(
        self,
        *,
        order_id: PaymentOrderId,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
        reason: str,
        reversed_at: datetime,
        now: datetime,
    ) -> Subscription | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                order = self._get_order_on_connection(connection, order_id)
                if order is None:
                    raise PaymentOrderNotFoundError("Payment order does not exist")
                if order.status is not PaymentStatus.PAID:
                    raise InvalidPaymentTransitionError(
                        f"Cannot refund order in status {order.status.value}"
                    )
                grant = self._get_grant_on_connection(
                    connection,
                    user_id=order.user_id,
                    provider_id=provider_id,
                    provider_transaction_reference=provider_transaction_reference,
                )
                if grant is None:
                    raise PaymentTransactionNotClaimedError(
                        "No entitlement grant for the claimed transaction"
                    )
                mark_grant_reversed_on_connection(
                    connection, grant.grant_id, reason=reason, reversed_at=reversed_at
                )
                connection.execute(
                    "UPDATE payment_orders SET status = ?, provider_id = ? WHERE order_id = ?",
                    (PaymentStatus.REFUNDED.value, str(provider_id), str(order_id)),
                )
                connection.execute(
                    """
                    UPDATE payment_attempts
                    SET status = ?, updated_at = ?
                    WHERE order_id = ?
                    """,
                    (PaymentStatus.REFUNDED.value, _dump_datetime(now), str(order_id)),
                )
                subscription = recompute_subscription_on_connection(
                    connection, user_id=order.user_id, now=now
                )
                connection.execute("COMMIT")
                return subscription
            except (
                PaymentOrderNotFoundError,
                InvalidPaymentTransitionError,
                PaymentTransactionNotClaimedError,
            ) as exc:
                connection.execute("ROLLBACK")
                raise exc
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise PaymentBackendError("Payment backend is unavailable") from exc

    # -- connection-scoped helpers -------------------------------------------

    @staticmethod
    def _get_order_on_connection(
        connection: sqlite3.Connection, order_id: PaymentOrderId
    ) -> PaymentOrder | None:
        row = connection.execute(
            "SELECT * FROM payment_orders WHERE order_id = ?", (str(order_id),)
        ).fetchone()
        return _order_from_row(row) if row is not None else None

    @staticmethod
    def _get_grant_on_connection(
        connection: sqlite3.Connection,
        *,
        user_id: int,
        provider_id: PaymentProviderId,
        provider_transaction_reference: ProviderTransactionReference,
    ) -> EntitlementGrant | None:
        for grant in load_grants_on_connection(connection, user_id):
            if grant.source_type == str(provider_id) and grant.source_reference == str(
                provider_transaction_reference
            ):
                return grant
        return None


def _order_from_row(row: sqlite3.Row) -> PaymentOrder:
    return PaymentOrder(
        order_id=PaymentOrderId(str(row["order_id"])),
        user_id=int(row["user_id"]),
        plan_id=PlanId(str(row["plan_id"])),
        duration_months=int(row["duration_months"]),
        capabilities=_load_capabilities(str(row["capabilities_json"])),
        amount_minor=int(row["amount_minor"]),
        currency=str(row["currency"]),
        created_at=_load_datetime(str(row["created_at"])),
        expires_at=_load_datetime(str(row["expires_at"])),
        status=PaymentStatus(str(row["status"])),
        provider_id=PaymentProviderId(str(row["provider_id"])) if row["provider_id"] else None,
    )


def _dump_capabilities(capabilities: frozenset[Capability]) -> str:
    import json

    return json.dumps(sorted(item.value for item in capabilities), separators=(",", ":"))


def _load_capabilities(value: str) -> frozenset[Capability]:
    import json

    capabilities: set[Capability] = set()
    try:
        raw = json.loads(value)
    except ValueError, TypeError:
        return frozenset()
    for entry in raw if isinstance(raw, list) else []:
        try:
            capabilities.add(Capability(str(entry)))
        except ValueError:
            continue
    return frozenset(capabilities)


def _dump_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
