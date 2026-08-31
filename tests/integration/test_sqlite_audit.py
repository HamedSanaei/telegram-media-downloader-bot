import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditDeliveryResult,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    LoggerDestinationHealth,
    LoggerDestinationSource,
    LoggerOutboxItem,
    LoggerOutboxState,
)
from telegram_media_bot.domain.errors import PersistenceError
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository


def _event(identity: str = "event-1", message: str = "safe") -> AuditEvent:
    return AuditEvent(
        event_id=identity,
        event_type=AuditEventType.SYSTEM_HEALTH,
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        occurred_at=datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
        correlation_id=identity,
        message=message,
    )


def _state(path: Path, event_id: str, chat_id: int) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """SELECT state FROM logger_outbox
            WHERE event_id=? AND destination_chat_id=?""",
            (event_id, chat_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _expire_and_make_due(path: Path) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE logger_outbox SET lease_until=?,next_attempt_at=?", (past, past))


def test_config_runtime_union_deduplicates_and_runtime_removal_preserves_config(
    tmp_path: Path,
) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    destination = repository.add_runtime_destination(channel)

    assert destination.ownership == frozenset(
        {LoggerDestinationSource.CONFIG, LoggerDestinationSource.RUNTIME}
    )
    assert repository.remove_runtime_destination(channel)
    remaining = repository.list_destinations()
    assert len(remaining) == 1
    assert remaining[0].ownership == frozenset({LoggerDestinationSource.CONFIG})
    assert remaining[0].enabled


def test_runtime_destination_enable_disable_and_removal(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    channel = -1001234567890
    created = repository.add_runtime_destination(channel)
    assert created.runtime_owned and created.health is LoggerDestinationHealth.ACTIVE
    disabled = repository.set_destination_enabled(channel, False)
    assert not disabled.enabled and disabled.health is LoggerDestinationHealth.DISABLED
    enabled = repository.set_destination_enabled(channel, True)
    assert enabled.enabled and enabled.updated_at >= created.created_at
    assert repository.remove_runtime_destination(channel)
    assert repository.list_destinations() == ()


def test_repeated_initialization_upgrades_legacy_database_without_rewriting_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("INSERT INTO jobs VALUES ('existing','unchanged')")

    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM jobs").fetchall() == [("existing", "unchanged")]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"logger_destinations", "audit_events", "logger_outbox"} <= tables
    assert repository.list_destinations() == ()


def test_enqueue_is_idempotent_per_event_destination_and_detects_collision(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890, -1001234567891))
    assert repository.enqueue(_event()) == 2
    assert repository.enqueue(_event()) == 0
    with pytest.raises(PersistenceError, match="identity collision"):
        repository.enqueue(_event(message="different"))


def test_concurrent_enqueue_creates_one_effect_per_destination(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890, -1001234567891))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: repository.enqueue(_event()), range(20)))

    assert sum(results) == 2
    assert len(repository.claim_pending(limit=20)) == 2


class OutcomeDelivery:
    def __init__(self, outcomes: dict[int, AuditDeliveryResult | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[int] = []

    async def deliver(self, item: LoggerOutboxItem) -> AuditDeliveryResult:
        self.calls.append(item.destination_chat_id)
        outcome = self.outcomes[item.destination_chat_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_per_destination_success_terminal_and_uncertain_are_isolated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    succeeded, forbidden, uncertain = (
        -1001234567890,
        -1001234567891,
        -1001234567892,
    )
    repository.reconcile_config((succeeded, forbidden, uncertain))
    repository.enqueue(_event())
    delivery = OutcomeDelivery(
        {
            succeeded: AuditDeliveryResult(AuditDeliveryOutcome.SUCCEEDED),
            forbidden: AuditDeliveryResult(
                AuditDeliveryOutcome.FAILED_TERMINAL, "TelegramForbidden"
            ),
            uncertain: TimeoutError("ambiguous transport outcome"),
        }
    )

    completed = await AuditOutboxProcessor(repository, delivery).dispatch_batch()

    assert completed == 1
    assert _state(path, "event-1", succeeded) == LoggerOutboxState.SUCCEEDED.value
    assert _state(path, "event-1", forbidden) == LoggerOutboxState.FAILED_TERMINAL.value
    assert _state(path, "event-1", uncertain) == LoggerOutboxState.UNCERTAIN.value
    destinations = {item.chat_id: item for item in repository.list_destinations()}
    assert destinations[forbidden].health is LoggerDestinationHealth.FORBIDDEN
    assert repository.claim_pending() == ()


async def test_typed_retryable_failure_retries_but_generic_exception_never_does(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    repository.enqueue(_event())
    retry = OutcomeDelivery(
        {channel: AuditDeliveryResult(AuditDeliveryOutcome.RETRYABLE, "PreSendUnavailable")}
    )
    processor = AuditOutboxProcessor(repository, retry)

    assert await processor.dispatch_batch() == 0
    assert _state(path, "event-1", channel) == LoggerOutboxState.RETRYABLE.value
    _expire_and_make_due(path)
    retry.outcomes[channel] = AuditDeliveryResult(AuditDeliveryOutcome.SUCCEEDED)
    assert await processor.dispatch_batch() == 1
    assert _state(path, "event-1", channel) == LoggerOutboxState.SUCCEEDED.value


def test_lease_recovery_distinguishes_pre_send_from_send_started(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    first, second = -1001234567890, -1001234567891
    repository.reconcile_config((first, second))
    repository.enqueue(_event())
    items = {item.destination_chat_id: item for item in repository.claim_pending()}
    assert repository.mark_send_started(items[second])
    _expire_and_make_due(path)

    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    safe, uncertain = restarted.recover_expired_leases()

    assert (safe, uncertain) == (1, 1)
    assert _state(path, "event-1", first) == LoggerOutboxState.RETRYABLE.value
    assert _state(path, "event-1", second) == LoggerOutboxState.UNCERTAIN.value
    claimed = restarted.claim_pending()
    assert [item.destination_chat_id for item in claimed] == [first]


async def test_retry_limit_becomes_terminal(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    repository.enqueue(_event())
    delivery = OutcomeDelivery(
        {channel: AuditDeliveryResult(AuditDeliveryOutcome.RETRYABLE, "PreSendUnavailable")}
    )
    processor = AuditOutboxProcessor(repository, delivery)

    for _attempt in range(6):
        await processor.dispatch_batch()
        _expire_and_make_due(path)

    assert _state(path, "event-1", channel) == LoggerOutboxState.FAILED_TERMINAL.value
    assert repository.health_snapshot().terminal_effects == 1
