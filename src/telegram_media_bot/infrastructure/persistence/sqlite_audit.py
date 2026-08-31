"""SQLite/WAL destinations and external-send-aware logger outbox (T027)."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_media_bot.application.services.audit_sanitizer import sanitize_audit_message
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    LoggerDestination,
    LoggerDestinationHealth,
    LoggerDestinationSource,
    LoggerHealthSnapshot,
    LoggerOutboxItem,
    LoggerOutboxState,
    TelegramSourceReference,
)
from telegram_media_bot.domain.errors import PersistenceError

_MAX_ATTEMPTS = 6
_MEDIA_GROUP_SETTLE_SECONDS = 2


class SqliteAuditRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("logger durable state operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS logger_destinations (
                    chat_id INTEGER PRIMARY KEY,
                    config_owned INTEGER NOT NULL DEFAULT 0,
                    runtime_owned INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    health TEXT NOT NULL DEFAULT 'active',
                    last_failure_class TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS logger_outbox (
                    event_id TEXT NOT NULL,
                    destination_chat_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_token TEXT,
                    lease_until TEXT,
                    send_started_at TEXT,
                    completed_at TEXT,
                    uncertain_at TEXT,
                    failed_at TEXT,
                    last_failure_class TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, destination_chat_id),
                    FOREIGN KEY (event_id) REFERENCES audit_events(event_id),
                    FOREIGN KEY (destination_chat_id) REFERENCES logger_destinations(chat_id)
                );
                CREATE INDEX IF NOT EXISTS logger_outbox_ready_idx
                    ON logger_outbox(state, next_attempt_at, lease_until);
                CREATE INDEX IF NOT EXISTS logger_destination_health_idx
                    ON logger_destinations(health, enabled);
                CREATE TABLE IF NOT EXISTS audit_submission_groups (
                    source_chat_id INTEGER NOT NULL,
                    media_group_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (source_chat_id, media_group_id),
                    FOREIGN KEY (event_id) REFERENCES audit_events(event_id)
                );
                CREATE TABLE IF NOT EXISTS logger_privacy_acknowledgements (
                    telegram_user_id INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_user_id, policy_version)
                );
                """
            )

    def reconcile_config(self, chat_ids: tuple[int, ...]) -> None:
        desired = set(chat_ids)
        if len(desired) != len(chat_ids):
            raise ValueError("configured logger channel IDs must be unique")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE logger_destinations SET config_owned=0, updated_at=?", (now,)
            )
            for chat_id in sorted(desired):
                _validate_chat_id(chat_id)
                connection.execute(
                    """INSERT INTO logger_destinations
                    (chat_id,config_owned,runtime_owned,enabled,health,created_at,updated_at)
                    VALUES (?,1,0,1,'active',?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                    config_owned=1, enabled=1,
                    health=CASE WHEN logger_destinations.health='disabled'
                        THEN 'active' ELSE logger_destinations.health END,
                    updated_at=excluded.updated_at""",
                    (chat_id, now, now),
                )
            connection.execute(
                """UPDATE logger_destinations SET enabled=0, health='disabled', updated_at=?
                WHERE config_owned=0 AND runtime_owned=0""",
                (now,),
            )
            _terminalize_inactive_effects(
                connection,
                failure_class="DestinationRemoved",
                where="config_owned=0 AND runtime_owned=0",
            )
            connection.execute("COMMIT")

    def list_destinations(self) -> tuple[LoggerDestination, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM logger_destinations
                WHERE config_owned=1 OR runtime_owned=1 ORDER BY chat_id"""
            ).fetchall()
        return tuple(_destination(row) for row in rows)

    def add_runtime_destination(self, chat_id: int) -> LoggerDestination:
        _validate_chat_id(chat_id)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO logger_destinations
                (chat_id,config_owned,runtime_owned,enabled,health,created_at,updated_at)
                VALUES (?,0,1,1,'active',?,?)
                ON CONFLICT(chat_id) DO UPDATE SET runtime_owned=1,enabled=1,health='active',
                last_failure_class=NULL,updated_at=excluded.updated_at""",
                (chat_id, now, now),
            )
            row = connection.execute(
                "SELECT * FROM logger_destinations WHERE chat_id=?", (chat_id,)
            ).fetchone()
        assert row is not None
        return _destination(row)

    def remove_runtime_destination(self, chat_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT config_owned,runtime_owned FROM logger_destinations WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if row is None or not bool(row["runtime_owned"]):
                connection.execute("COMMIT")
                return False
            config_owned = bool(row["config_owned"])
            connection.execute(
                """UPDATE logger_destinations SET runtime_owned=0,enabled=?,health=?,updated_at=?
                WHERE chat_id=?""",
                (int(config_owned), "active" if config_owned else "disabled", _now(), chat_id),
            )
            if not config_owned:
                _terminalize_inactive_effects(
                    connection,
                    failure_class="DestinationRemoved",
                    where="chat_id=?",
                    parameters=(chat_id,),
                )
            connection.execute("COMMIT")
        return True

    def set_destination_enabled(self, chat_id: int, enabled: bool) -> LoggerDestination:
        health = LoggerDestinationHealth.ACTIVE if enabled else LoggerDestinationHealth.DISABLED
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE logger_destinations SET enabled=?,health=?,last_failure_class=NULL,
                updated_at=? WHERE chat_id=? AND (config_owned=1 OR runtime_owned=1)""",
                (int(enabled), health.value, _now(), chat_id),
            ).rowcount
            if not changed:
                connection.execute("ROLLBACK")
                raise PersistenceError("logger destination does not exist")
            if not enabled:
                _terminalize_inactive_effects(
                    connection,
                    failure_class="DestinationDisabled",
                    where="chat_id=?",
                    parameters=(chat_id,),
                )
            row = connection.execute(
                "SELECT * FROM logger_destinations WHERE chat_id=?", (chat_id,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return _destination(row)

    def record_probe_health(
        self,
        chat_id: int,
        health: LoggerDestinationHealth,
        failure_class: str | None = None,
    ) -> LoggerDestination | None:
        """Record the outcome of an operator probe for one destination (T028)."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE logger_destinations SET health=?,last_failure_class=?,updated_at=?
                WHERE chat_id=? AND (config_owned=1 OR runtime_owned=1)""",
                (health.value, _failure(failure_class), now, chat_id),
            ).rowcount
            if not changed:
                connection.execute("COMMIT")
                return None
            if health in {LoggerDestinationHealth.FORBIDDEN, LoggerDestinationHealth.DISABLED}:
                _terminalize_inactive_effects(
                    connection,
                    failure_class=failure_class or "DestinationUnavailable",
                    where="chat_id=?",
                    parameters=(chat_id,),
                )
            row = connection.execute(
                "SELECT * FROM logger_destinations WHERE chat_id=?", (chat_id,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return _destination(row)

    def enqueue(self, event: AuditEvent) -> int:
        if sanitize_audit_message(event.message) != event.message:
            raise ValueError("audit event must be sanitized before persistence")
        payload = serialize_event(event)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_json FROM audit_events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if existing is not None:
                persisted = deserialize_event(str(existing["event_json"]))
                if persisted != event and not _compatible_submission_replay(persisted, event):
                    raise PersistenceError("audit event identity collision")
                connection.execute("COMMIT")
                return 0
            event_created = connection.execute(
                "INSERT OR IGNORE INTO audit_events(event_id,event_json,created_at) VALUES (?,?,?)",
                (event.event_id, payload, now),
            ).rowcount
            if not event_created:
                connection.execute("COMMIT")
                return 0
            next_attempt_at = now
            if event.source is not None and event.source.media_group_id is not None:
                connection.execute(
                    """INSERT INTO audit_submission_groups
                    (source_chat_id,media_group_id,event_id) VALUES (?,?,?)""",
                    (event.source.chat_id, event.source.media_group_id, event.event_id),
                )
                next_attempt_at = (
                    datetime.now(UTC) + timedelta(seconds=_MEDIA_GROUP_SETTLE_SECONDS)
                ).isoformat()
            destinations = connection.execute(
                """SELECT chat_id FROM logger_destinations WHERE enabled=1
                AND health IN ('active','unreachable')
                AND (config_owned=1 OR runtime_owned=1)"""
            ).fetchall()
            created = 0
            for destination in destinations:
                created += connection.execute(
                    """INSERT OR IGNORE INTO logger_outbox
                    (event_id,destination_chat_id,state,next_attempt_at,created_at,updated_at)
                    VALUES (?,?,'pending',?,?,?)""",
                    (event.event_id, int(destination["chat_id"]), next_attempt_at, now, now),
                ).rowcount
            connection.execute("COMMIT")
        return created

    def extend_submission_source(self, source: TelegramSourceReference) -> int:
        """Merge one album member before delivery; a sent/leased album remains immutable."""
        if source.media_group_id is None:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mapping = connection.execute(
                """SELECT event_id FROM audit_submission_groups
                WHERE source_chat_id=? AND media_group_id=?""",
                (source.chat_id, source.media_group_id),
            ).fetchone()
            if mapping is None:
                connection.execute("COMMIT")
                return 0
            event_id = str(mapping["event_id"])
            immutable = connection.execute(
                """SELECT COUNT(*) FROM logger_outbox WHERE event_id=?
                AND state NOT IN ('pending','retryable')""",
                (event_id,),
            ).fetchone()
            if immutable is not None and int(immutable[0]) > 0:
                connection.execute("COMMIT")
                return 0
            row = connection.execute(
                "SELECT event_json FROM audit_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return 0
            event = deserialize_event(str(row["event_json"]))
            if event.source is None:
                connection.execute("COMMIT")
                return 0
            message_ids = tuple(sorted({*event.source.message_ids, *source.message_ids}))
            if message_ids == event.source.message_ids:
                connection.execute("COMMIT")
                return len(message_ids)
            merged = replace(event, source=replace(event.source, message_ids=message_ids))
            now = datetime.now(UTC)
            connection.execute(
                "UPDATE audit_events SET event_json=? WHERE event_id=?",
                (serialize_event(merged), event_id),
            )
            connection.execute(
                """UPDATE logger_outbox SET next_attempt_at=?,updated_at=?
                WHERE event_id=? AND state IN ('pending','retryable')""",
                (
                    (now + timedelta(seconds=_MEDIA_GROUP_SETTLE_SECONDS)).isoformat(),
                    now.isoformat(),
                    event_id,
                ),
            )
            connection.execute("COMMIT")
        return len(message_ids)

    def has_privacy_acknowledgement(self, user_id: int, policy_version: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM logger_privacy_acknowledgements
                WHERE telegram_user_id=? AND policy_version=?""",
                (user_id, policy_version),
            ).fetchone()
        return row is not None

    def acknowledge_privacy(self, user_id: int, policy_version: str) -> bool:
        with self._connect() as connection:
            created = connection.execute(
                """INSERT OR IGNORE INTO logger_privacy_acknowledgements
                (telegram_user_id,policy_version,acknowledged_at) VALUES (?,?,?)""",
                (user_id, policy_version, _now()),
            ).rowcount
        return bool(created)

    def recover_expired_leases(self) -> tuple[int, int]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            safe = connection.execute(
                """UPDATE logger_outbox SET state='retryable',lease_token=NULL,lease_until=NULL,
                next_attempt_at=?,updated_at=? WHERE state='leased' AND lease_until<=?""",
                (now, now, now),
            ).rowcount
            uncertain = connection.execute(
                """UPDATE logger_outbox SET state='uncertain',lease_token=NULL,lease_until=NULL,
                uncertain_at=?,last_failure_class='LeaseExpiredAfterSendStarted',updated_at=?
                WHERE state='sending' AND lease_until<=?""",
                (now, now, now),
            ).rowcount
            connection.execute("COMMIT")
        return safe, uncertain

    def claim_pending(self, *, limit: int = 20) -> tuple[LoggerOutboxItem, ...]:
        now = datetime.now(UTC)
        lease_until = (now + timedelta(minutes=2)).isoformat()
        claimed: list[LoggerOutboxItem] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT o.*,e.event_json FROM logger_outbox o
                JOIN audit_events e USING(event_id)
                JOIN logger_destinations d ON d.chat_id=o.destination_chat_id
                WHERE o.state IN ('pending','retryable') AND o.next_attempt_at<=?
                AND d.enabled=1 AND d.health IN ('active','unreachable')
                AND (d.config_owned=1 OR d.runtime_owned=1)
                ORDER BY o.next_attempt_at,o.event_id,o.destination_chat_id LIMIT ?""",
                (now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(24)
                changed = connection.execute(
                    """UPDATE logger_outbox SET state='leased',attempt_count=attempt_count+1,
                    lease_token=?,lease_until=?,updated_at=?
                    WHERE event_id=? AND destination_chat_id=? AND state IN ('pending','retryable')""",
                    (
                        token,
                        lease_until,
                        now.isoformat(),
                        row["event_id"],
                        row["destination_chat_id"],
                    ),
                ).rowcount
                if changed:
                    claimed.append(
                        LoggerOutboxItem(
                            event=deserialize_event(str(row["event_json"])),
                            destination_chat_id=int(row["destination_chat_id"]),
                            state=LoggerOutboxState.LEASED,
                            attempt_count=int(row["attempt_count"]) + 1,
                            lease_token=token,
                        )
                    )
            connection.execute("COMMIT")
        return tuple(claimed)

    def mark_send_started(self, item: LoggerOutboxItem) -> bool:
        now = _now()
        with self._connect() as connection:
            return bool(
                connection.execute(
                    """UPDATE logger_outbox SET state='sending',send_started_at=?,updated_at=?
                    WHERE event_id=? AND destination_chat_id=? AND state='leased'
                    AND lease_token=?""",
                    (now, now, item.event.event_id, item.destination_chat_id, item.lease_token),
                ).rowcount
            )

    def mark_succeeded(self, item: LoggerOutboxItem) -> None:
        self._finish(item, LoggerOutboxState.SUCCEEDED, None)

    def mark_retryable(self, item: LoggerOutboxItem, failure_class: str) -> None:
        now = datetime.now(UTC)
        if item.attempt_count >= _MAX_ATTEMPTS:
            self.mark_terminal(item, "RetryLimitExceeded")
            return
        delay = min(3600, 15 * (2 ** min(item.attempt_count, 8)))
        with self._connect() as connection:
            self._transition_sending(
                connection,
                item,
                state=LoggerOutboxState.RETRYABLE,
                failure_class=failure_class,
                next_attempt_at=(now + timedelta(seconds=delay)).isoformat(),
            )
            connection.execute(
                """UPDATE logger_destinations SET health='unreachable',last_failure_class=?,
                updated_at=? WHERE chat_id=?""",
                (_failure(failure_class), now.isoformat(), item.destination_chat_id),
            )

    def mark_uncertain(self, item: LoggerOutboxItem, failure_class: str) -> None:
        self._finish(item, LoggerOutboxState.UNCERTAIN, failure_class)

    def mark_terminal(self, item: LoggerOutboxItem, failure_class: str) -> None:
        self._finish(item, LoggerOutboxState.FAILED_TERMINAL, failure_class)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE logger_destinations SET health='forbidden',last_failure_class=?,
                updated_at=? WHERE chat_id=?""",
                (_failure(failure_class), _now(), item.destination_chat_id),
            )
            _terminalize_inactive_effects(
                connection,
                failure_class=failure_class,
                where="chat_id=?",
                parameters=(item.destination_chat_id,),
            )
            connection.execute("COMMIT")

    def health_snapshot(self) -> LoggerHealthSnapshot:
        with self._connect() as connection:
            destinations = {
                str(row["health"]): int(row["count"])
                for row in connection.execute(
                    """SELECT health,COUNT(*) AS count FROM logger_destinations
                    WHERE config_owned=1 OR runtime_owned=1 GROUP BY health"""
                ).fetchall()
            }
            effects = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM logger_outbox GROUP BY state"
                ).fetchall()
            }
            oldest = connection.execute(
                """SELECT MIN(created_at) FROM logger_outbox
                WHERE state IN ('pending','leased','sending','retryable')"""
            ).fetchone()
        oldest_pending_age_seconds = 0
        if oldest is not None and oldest[0] is not None:
            oldest_pending_age_seconds = max(
                0,
                int((datetime.now(UTC) - datetime.fromisoformat(str(oldest[0]))).total_seconds()),
            )
        return LoggerHealthSnapshot(
            effective_destinations=sum(destinations.values()),
            active_destinations=destinations.get(LoggerDestinationHealth.ACTIVE.value, 0),
            unreachable_destinations=destinations.get(LoggerDestinationHealth.UNREACHABLE.value, 0),
            forbidden_destinations=destinations.get(LoggerDestinationHealth.FORBIDDEN.value, 0),
            disabled_destinations=destinations.get(LoggerDestinationHealth.DISABLED.value, 0),
            pending_effects=effects.get(LoggerOutboxState.PENDING.value, 0)
            + effects.get(LoggerOutboxState.LEASED.value, 0)
            + effects.get(LoggerOutboxState.SENDING.value, 0),
            retryable_effects=effects.get(LoggerOutboxState.RETRYABLE.value, 0),
            uncertain_effects=effects.get(LoggerOutboxState.UNCERTAIN.value, 0),
            terminal_effects=effects.get(LoggerOutboxState.FAILED_TERMINAL.value, 0),
            oldest_pending_age_seconds=oldest_pending_age_seconds,
        )

    def _finish(
        self,
        item: LoggerOutboxItem,
        state: LoggerOutboxState,
        failure_class: str | None,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            self._transition_sending(
                connection,
                item,
                state=state,
                failure_class=failure_class,
                next_attempt_at=now,
            )
            if state is LoggerOutboxState.SUCCEEDED:
                connection.execute(
                    """UPDATE logger_outbox SET completed_at=?
                    WHERE event_id=? AND destination_chat_id=? AND state='succeeded'""",
                    (now, item.event.event_id, item.destination_chat_id),
                )
            elif state is LoggerOutboxState.UNCERTAIN:
                connection.execute(
                    """UPDATE logger_outbox SET uncertain_at=?
                    WHERE event_id=? AND destination_chat_id=? AND state='uncertain'""",
                    (now, item.event.event_id, item.destination_chat_id),
                )
            else:
                connection.execute(
                    """UPDATE logger_outbox SET failed_at=?
                    WHERE event_id=? AND destination_chat_id=? AND state='failed_terminal'""",
                    (now, item.event.event_id, item.destination_chat_id),
                )
            if state is LoggerOutboxState.SUCCEEDED:
                connection.execute(
                    """UPDATE logger_destinations SET health='active',last_failure_class=NULL,
                    updated_at=? WHERE chat_id=?""",
                    (now, item.destination_chat_id),
                )

    def _transition_sending(
        self,
        connection: sqlite3.Connection,
        item: LoggerOutboxItem,
        *,
        state: LoggerOutboxState,
        failure_class: str | None,
        next_attempt_at: str,
    ) -> None:
        changed = connection.execute(
            """UPDATE logger_outbox SET state=?,lease_token=NULL,lease_until=NULL,
            next_attempt_at=?,last_failure_class=?,updated_at=?
            WHERE event_id=? AND destination_chat_id=? AND state='sending' AND lease_token=?""",
            (
                state.value,
                next_attempt_at,
                _failure(failure_class),
                _now(),
                item.event.event_id,
                item.destination_chat_id,
                item.lease_token,
            ),
        ).rowcount
        if not changed:
            raise PersistenceError("stale logger delivery lease")


def _terminalize_inactive_effects(
    connection: sqlite3.Connection,
    *,
    failure_class: str,
    where: str,
    parameters: tuple[object, ...] = (),
) -> int:
    """Quarantine work that has not crossed the external-send boundary."""
    now = _now()
    return connection.execute(
        f"""UPDATE logger_outbox SET state='failed_terminal',lease_token=NULL,lease_until=NULL,
        failed_at=?,last_failure_class=?,updated_at=?
        WHERE destination_chat_id IN (SELECT chat_id FROM logger_destinations WHERE {where})
        AND state IN ('pending','retryable','leased')""",
        (now, _failure(failure_class), now, *parameters),
    ).rowcount


def serialize_event(event: AuditEvent) -> str:
    return json.dumps(_event_dict(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_event(payload: str) -> AuditEvent:
    return _event_from_dict(json.loads(payload))


def _validate_chat_id(chat_id: int) -> None:
    if chat_id > -1000000000000:
        raise ValueError("logger destination must be a numeric -100... Telegram channel ID")


def _destination(row: sqlite3.Row) -> LoggerDestination:
    ownership = frozenset(
        source
        for source, present in (
            (LoggerDestinationSource.CONFIG, bool(row["config_owned"])),
            (LoggerDestinationSource.RUNTIME, bool(row["runtime_owned"])),
        )
        if present
    )
    return LoggerDestination(
        chat_id=int(row["chat_id"]),
        ownership=ownership,
        enabled=bool(row["enabled"]),
        health=LoggerDestinationHealth(str(row["health"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        last_failure_class=(str(row["last_failure_class"]) if row["last_failure_class"] else None),
    )


def _event_dict(event: AuditEvent) -> dict[str, Any]:
    return {
        "category": event.category.value,
        "content_type": event.content_type,
        "correlation_id": event.correlation_id,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "job_id": event.job_id,
        "message": event.message,
        "occurred_at": event.occurred_at.isoformat(),
        "provider": event.provider,
        "severity": event.severity.value,
        "source": (
            {
                "chat_id": event.source.chat_id,
                "media_group_id": event.source.media_group_id,
                "message_ids": event.source.message_ids,
            }
            if event.source
            else None
        ),
        "telegram_user_id": event.telegram_user_id,
        "update_id": event.update_id,
    }


def _event_from_dict(data: dict[str, Any]) -> AuditEvent:
    source = data.get("source")
    return AuditEvent(
        event_id=str(data["event_id"]),
        event_type=AuditEventType(str(data["event_type"])),
        category=AuditCategory(str(data["category"])),
        severity=AuditSeverity(str(data["severity"])),
        occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
        correlation_id=str(data["correlation_id"]),
        message=str(data["message"]),
        telegram_user_id=(
            int(data["telegram_user_id"]) if data.get("telegram_user_id") is not None else None
        ),
        update_id=int(data["update_id"]) if data.get("update_id") is not None else None,
        job_id=str(data["job_id"]) if data.get("job_id") else None,
        content_type=str(data["content_type"]) if data.get("content_type") else None,
        provider=str(data["provider"]) if data.get("provider") else None,
        source=(
            TelegramSourceReference(
                chat_id=int(source["chat_id"]),
                message_ids=tuple(int(item) for item in source["message_ids"]),
                media_group_id=(
                    str(source["media_group_id"]) if source.get("media_group_id") else None
                ),
            )
            if isinstance(source, dict)
            else None
        ),
    )


def _compatible_submission_replay(persisted: AuditEvent, incoming: AuditEvent) -> bool:
    if (
        persisted.event_type is not AuditEventType.USER_SUBMISSION_RECEIVED
        or incoming.event_type is not AuditEventType.USER_SUBMISSION_RECEIVED
        or persisted.source is None
        or incoming.source is None
    ):
        return False
    persisted_without_source = replace(persisted, source=incoming.source)
    return (
        persisted_without_source == incoming
        and persisted.source.chat_id == incoming.source.chat_id
        and persisted.source.media_group_id == incoming.source.media_group_id
        and set(incoming.source.message_ids) <= set(persisted.source.message_ids)
    )


def _failure(value: str | None) -> str | None:
    return value[:96] if value else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SqliteAuditRepository", "deserialize_event", "serialize_event"]
