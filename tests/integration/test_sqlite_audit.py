import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.application.services.audit_sanitizer import (
    UnsafeAuditPayloadError,
    sanitize_audit_caption,
)
from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditDeliveryResult,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    DeliveredOutputAuditContext,
    LoggerDestinationHealth,
    LoggerDestinationSource,
    LoggerOutboxItem,
    LoggerOutboxState,
    TelegramSourceReference,
)
from telegram_media_bot.domain.errors import PersistenceError
from telegram_media_bot.infrastructure.persistence.sqlite_audit import (
    SqliteAuditRepository,
    deserialize_event,
    serialize_event,
)


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


def _output_event(
    captions: tuple[str, ...] = ("توضیحات <نمونه> & متن\nخط دوم 🎬", ""),
    telegram_username: str | None = "sample_user",
) -> AuditEvent:
    return AuditEvent(
        event_id="delivery-output:job-1",
        event_type=AuditEventType.DOWNLOAD_OUTPUT_DELIVERED,
        category=AuditCategory.USER_SUBMISSION,
        severity=AuditSeverity.INFO,
        occurred_at=datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
        correlation_id="job-1",
        message="Delivered download output",
        telegram_user_id=821868829,
        job_id="job-1",
        source=TelegramSourceReference(
            chat_id=4242, message_ids=tuple(range(101, 101 + len(captions)))
        ),
        output=DeliveredOutputAuditContext(
            source_url="https://www.instagram.com/reel/DeL5jdsIMo3/",
            captions=captions,
            telegram_username=telegram_username,
        ),
    )


def _state(path: Path, event_id: str, chat_id: int) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            """SELECT state FROM logger_outbox
            WHERE event_id=? AND destination_chat_id=?""",
            (event_id, chat_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _expire_and_make_due(path: Path) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE logger_outbox SET lease_until=?,next_attempt_at=?", (past, past))
        connection.commit()


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


def test_disabling_or_removing_destination_never_replays_old_pending_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    channel = -1001234567890
    repository.add_runtime_destination(channel)
    repository.enqueue(_event("before-disable"))

    repository.set_destination_enabled(channel, False)

    assert _state(path, "before-disable", channel) == LoggerOutboxState.FAILED_TERMINAL.value
    assert repository.claim_pending() == ()
    repository.set_destination_enabled(channel, True)
    assert repository.claim_pending() == ()

    repository.enqueue(_event("after-enable"))
    delivered = repository.claim_pending()[0]
    assert repository.mark_send_started(delivered)
    repository.mark_succeeded(delivered)
    repository.set_destination_enabled(channel, False)
    repository.set_destination_enabled(channel, True)
    assert _state(path, "after-enable", channel) == LoggerOutboxState.SUCCEEDED.value
    assert repository.claim_pending() == ()

    repository.enqueue(_event("before-remove"))
    assert repository.remove_runtime_destination(channel)
    assert _state(path, "before-remove", channel) == LoggerOutboxState.FAILED_TERMINAL.value
    assert repository.claim_pending() == ()


def test_forbidden_terminalizes_open_work_while_unreachable_remains_retryable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    forbidden, unreachable = -1001234567890, -1001234567891
    repository.reconcile_config((forbidden, unreachable))
    repository.enqueue(_event("permission-change"))

    repository.record_probe_health(
        forbidden, LoggerDestinationHealth.FORBIDDEN, "TelegramForbidden"
    )
    repository.record_probe_health(
        unreachable, LoggerDestinationHealth.UNREACHABLE, "TelegramNetworkError"
    )

    assert _state(path, "permission-change", forbidden) == LoggerOutboxState.FAILED_TERMINAL.value
    assert [item.destination_chat_id for item in repository.claim_pending()] == [unreachable]


def test_repeated_initialization_upgrades_legacy_database_without_rewriting_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("INSERT INTO jobs VALUES ('existing','unchanged')")
        connection.commit()

    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.initialize()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT * FROM jobs").fetchall() == [("existing", "unchanged")]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "logger_destinations",
        "audit_events",
        "logger_outbox",
        "logger_privacy_acknowledgements",
    } <= tables
    assert "audit_submission_groups" not in tables
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


class CancelledDelivery:
    async def deliver(self, _item: LoggerOutboxItem) -> AuditDeliveryResult:
        raise asyncio.CancelledError


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

    observed: list[tuple[AuditDeliveryOutcome, AuditCategory]] = []
    completed = await AuditOutboxProcessor(
        repository,
        delivery,
        observer=lambda outcome, category: observed.append((outcome, category)),
    ).dispatch_batch()

    assert completed == 1
    assert _state(path, "event-1", succeeded) == LoggerOutboxState.SUCCEEDED.value
    assert _state(path, "event-1", forbidden) == LoggerOutboxState.FAILED_TERMINAL.value
    assert _state(path, "event-1", uncertain) == LoggerOutboxState.UNCERTAIN.value
    destinations = {item.chat_id: item for item in repository.list_destinations()}
    assert destinations[forbidden].health is LoggerDestinationHealth.FORBIDDEN
    assert repository.claim_pending() == ()
    assert set(observed) == {
        (AuditDeliveryOutcome.SUCCEEDED, AuditCategory.SYSTEM),
        (AuditDeliveryOutcome.FAILED_TERMINAL, AuditCategory.SYSTEM),
        (AuditDeliveryOutcome.UNCERTAIN, AuditCategory.SYSTEM),
    }


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


async def test_dispatch_cancellation_leaves_send_started_for_uncertain_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    repository.enqueue(_event())

    with pytest.raises(asyncio.CancelledError):
        await AuditOutboxProcessor(repository, CancelledDelivery()).dispatch_batch()

    assert _state(path, "event-1", channel) == LoggerOutboxState.SENDING.value
    _expire_and_make_due(path)
    assert repository.recover_expired_leases() == (0, 1)
    assert _state(path, "event-1", channel) == LoggerOutboxState.UNCERTAIN.value


def test_output_intent_username_migration_preserves_legacy_rows_and_timestamps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-output.sqlite3"
    created_at = "2026-08-30T10:00:00+00:00"
    completed_at = "2026-08-30T10:05:00+00:00"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """CREATE TABLE logger_delivery_output_intents (
            job_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT)"""
        )
        connection.executemany(
            "INSERT INTO logger_delivery_output_intents VALUES (?,?,?)",
            (("pending", created_at, None), ("completed", created_at, completed_at)),
        )
        connection.commit()

    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.initialize()
    assert repository.pending_delivery_outputs() == ("pending",)
    assert repository.delivery_output_username("pending") is None
    assert repository.delivery_output_username("completed") is None
    assert not repository.prepare_delivery_output("pending", telegram_username="new_profile")
    assert not repository.prepare_delivery_output("completed", telegram_username="new_profile")
    assert repository.delivery_output_username("pending") is None
    assert repository.delivery_output_username("completed") is None

    with closing(sqlite3.connect(path)) as connection:
        columns = {
            row[1]: row[2]
            for row in connection.execute(
                "PRAGMA table_info(logger_delivery_output_intents)"
            ).fetchall()
        }
        rows = connection.execute(
            """SELECT job_id,created_at,completed_at,telegram_username
            FROM logger_delivery_output_intents ORDER BY job_id"""
        ).fetchall()
    assert columns["telegram_username"] == "TEXT"
    assert rows == [
        ("completed", created_at, completed_at, None),
        ("pending", created_at, None, None),
    ]


@pytest.mark.parametrize("username", ["sample_user", None])
def test_output_intent_username_is_insert_once_across_replay_completion_and_restart(
    tmp_path: Path, username: str | None
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    service = AuditService(repository, enabled=True)
    assert service.delivery_output_username("missing") is None
    assert service.prepare_delivery_output("job-1", telegram_username=username)
    with closing(sqlite3.connect(path)) as connection:
        original = connection.execute(
            "SELECT created_at,telegram_username FROM logger_delivery_output_intents"
        ).fetchone()

    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    service = AuditService(restarted, enabled=True)
    assert not service.prepare_delivery_output("job-1", telegram_username="changed_profile")
    assert service.delivery_output_username("job-1") == username
    assert service.delivery_output_pending("job-1")
    assert service.complete_delivery_output("job-1")
    assert not service.complete_delivery_output("job-1")
    assert not service.prepare_delivery_output("job-1", telegram_username="another_profile")
    assert service.delivery_output_username("job-1") == username
    assert service.pending_delivery_outputs() == ()
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT created_at,telegram_username,completed_at FROM logger_delivery_output_intents"
        ).fetchone()
    assert row is not None and original is not None
    assert row[:2] == original
    assert row[2] is not None


def test_disabled_output_intent_service_does_not_access_repository(tmp_path: Path) -> None:
    service = AuditService(SqliteAuditRepository(tmp_path / "uninitialized.sqlite3"), enabled=False)
    assert not service.prepare_delivery_output("job-1", telegram_username="sample_user")
    assert service.delivery_output_username("job-1") is None


@pytest.mark.parametrize("username", ["sample_user", None])
def test_enriched_output_json_and_outbox_roundtrip_preserves_context(
    tmp_path: Path, username: str | None
) -> None:
    path = tmp_path / "state.sqlite3"
    event = _output_event(telegram_username=username)
    payload = serialize_event(event)
    assert json.loads(payload)["output"] == {
        "source_url": "https://www.instagram.com/reel/DeL5jdsIMo3/",
        "telegram_username": username,
        "captions": ["توضیحات <نمونه> & متن\nخط دوم 🎬", ""],
    }
    assert deserialize_event(payload) == event

    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    assert repository.enqueue(event) == 1
    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    assert restarted.enqueue(event) == 0
    assert [item.event for item in restarted.claim_pending()] == [event]
    assert event.output is not None
    changed = replace(event.output, telegram_username="changed_profile")
    with pytest.raises(PersistenceError, match="identity collision"):
        restarted.enqueue(replace(event, output=changed))
    with pytest.raises(PersistenceError, match="identity collision"):
        restarted.enqueue(replace(event, output=replace(event.output, captions=("different", ""))))


@pytest.mark.parametrize("include_null_output", [False, True])
def test_historical_output_json_remains_readable_without_context(
    tmp_path: Path, include_null_output: bool
) -> None:
    path = tmp_path / "state.sqlite3"
    event = replace(_output_event(), output=None)
    data = json.loads(serialize_event(event))
    if not include_null_output:
        del data["output"]
    payload = json.dumps(data, ensure_ascii=False)
    assert deserialize_event(payload) == event
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    assert repository.enqueue(event) == 1
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE audit_events SET event_json=? WHERE event_id=?", (payload, event.event_id)
        )
        connection.commit()
    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    assert restarted.enqueue(event) == 0
    assert [item.event for item in restarted.claim_pending()] == [event]


@pytest.mark.parametrize(
    ("caption", "error"),
    [
        ("description  with\tspaces", ValueError),
        ("bot_token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", ValueError),
        ("Cookie: sessionid=private-value", ValueError),
        ("Traceback (most recent call last):\ninternal failure", UnsafeAuditPayloadError),
        (r"C:\Users\operator\private.txt", UnsafeAuditPayloadError),
        (".example.com\tTRUE\t/\tFALSE\t1234567890\tsessionid\tsecret", UnsafeAuditPayloadError),
    ],
)
def test_enriched_output_rejects_unsanitized_captions_before_persistence(
    tmp_path: Path, caption: str, error: type[Exception]
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    with pytest.raises(error):
        repository.enqueue(_output_event(captions=(caption,)))
    assert repository.claim_pending() == ()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_sanitized_caption_secrets_roundtrip_without_bypassing_message_validation(
    tmp_path: Path,
) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    caption = sanitize_audit_caption("توضیحات\nbot_token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef")
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef" not in caption
    assert "\n" in caption
    event = _output_event(captions=(caption,))
    assert repository.enqueue(event) == 1
    assert repository.claim_pending()[0].event == event
    with pytest.raises(ValueError, match="audit event must be sanitized"):
        repository.enqueue(replace(event, event_id="unsafe-message", message="password=secret"))


@pytest.mark.parametrize(
    "failure",
    [
        "MissingSourceReference",
        "MissingOutputCaptionContext",
        "MissingOutputUserIdentity",
        "SubmissionMirrorRetired",
    ],
)
async def test_local_terminal_failure_preserves_destination_and_other_pending_work(
    tmp_path: Path, failure: str
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    repository.enqueue(_output_event())
    item = repository.claim_pending()[0]
    assert repository.mark_send_started(item)
    repository.enqueue(_event("operational-after"))
    repository.mark_terminal(item, failure)
    assert _state(path, item.event.event_id, channel) == "failed_terminal"
    assert repository.list_destinations()[0].health is LoggerDestinationHealth.ACTIVE
    assert _state(path, "operational-after", channel) == "pending"
    delivery = OutcomeDelivery({channel: AuditDeliveryResult(AuditDeliveryOutcome.SUCCEEDED)})
    assert await AuditOutboxProcessor(repository, delivery).dispatch_batch() == 1
    assert _state(path, "operational-after", channel) == "succeeded"


def test_historical_accepted_effect_retirement_preserves_unsafe_history_and_group_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    states = (
        "pending",
        "retryable",
        "leased",
        "sending",
        "succeeded",
        "uncertain",
        "failed_terminal",
    )
    channels = tuple(-1001234567890 - index for index in range(len(states)))
    repository.reconcile_config(channels)
    event = replace(
        _event("historical-input"),
        event_type=AuditEventType.USER_SUBMISSION_RECEIVED,
        category=AuditCategory.USER_SUBMISSION,
        telegram_user_id=821868829,
        source=TelegramSourceReference(4242, (101, 102), "old-album"),
    )
    data = json.loads(serialize_event(event))
    del data["output"]
    payload = json.dumps(data, ensure_ascii=False)
    timestamp = "2026-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO audit_events VALUES (?,?,?)", (event.event_id, payload, timestamp)
        )
        connection.execute(
            """CREATE TABLE audit_submission_groups (
            source_chat_id INTEGER, media_group_id TEXT, event_id TEXT)"""
        )
        connection.execute(
            "INSERT INTO audit_submission_groups VALUES (?,?,?)",
            (4242, "old-album", event.event_id),
        )
        for channel, state in zip(channels, states, strict=True):
            connection.execute(
                """INSERT INTO logger_outbox (
                event_id,destination_chat_id,state,attempt_count,next_attempt_at,
                lease_token,lease_until,send_started_at,created_at,updated_at,last_failure_class)
                VALUES (?,?,?,3,?,'old-lease','2027-01-01T00:00:00+00:00',?,?,?,'OldFailure')""",
                (event.event_id, channel, state, timestamp, timestamp, timestamp, timestamp),
            )
        before = {
            row["destination_chat_id"]: dict(row)
            for row in connection.execute("SELECT * FROM logger_outbox").fetchall()
        }
        connection.commit()
    old_lease = LoggerOutboxItem(
        event=event,
        destination_chat_id=channels[2],
        state=LoggerOutboxState.LEASED,
        attempt_count=3,
        lease_token="old-lease",
    )
    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    restarted.initialize()
    assert not restarted.mark_send_started(old_lease)
    assert all(
        destination.health is LoggerDestinationHealth.ACTIVE
        for destination in restarted.list_destinations()
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        after = {
            row["destination_chat_id"]: dict(row)
            for row in connection.execute("SELECT * FROM logger_outbox").fetchall()
        }
        assert (
            connection.execute(
                "SELECT event_json FROM audit_events WHERE event_id=?", (event.event_id,)
            ).fetchone()[0]
            == payload
        )
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM audit_submission_groups").fetchall()
        ] == [(4242, "old-album", event.event_id)]
    for channel, state in zip(channels, states, strict=True):
        row = after[channel]
        if state in {"pending", "retryable", "leased"}:
            assert row["state"] == "failed_terminal"
            assert row["last_failure_class"] == "SubmissionMirrorRetired"
            assert row["lease_token"] is None and row["lease_until"] is None
            assert row["failed_at"] is not None
            assert row["send_started_at"] == timestamp
            assert row["attempt_count"] == 3
        else:
            assert row == before[channel]
    assert deserialize_event(payload) == event


def test_new_accepted_submission_events_are_rejected_before_persistence(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    event = replace(
        _event(),
        event_type=AuditEventType.USER_SUBMISSION_RECEIVED,
        category=AuditCategory.USER_SUBMISSION,
        source=TelegramSourceReference(4242, (101,)),
    )
    with pytest.raises(ValueError, match="accepted submission mirroring is retired"):
        repository.enqueue(event)
    assert repository.health_snapshot().pending_effects == 0
    assert repository.list_destinations()[0].health is LoggerDestinationHealth.ACTIVE


def test_legacy_privacy_acknowledgement_rows_survive_restart_without_gating_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    audit = AuditService(repository, enabled=True)
    assert audit.acknowledge_privacy(4242, "logger-v1")
    assert not audit.has_privacy_acknowledgement(821868829, "logger-v1")
    assert repository.enqueue(_output_event()) == 1
    restarted = SqliteAuditRepository(path)
    restarted.initialize()
    assert restarted.has_privacy_acknowledgement(4242, "logger-v1")
    assert not restarted.acknowledge_privacy(4242, "logger-v1")
    assert restarted.claim_pending()[0].event == _output_event()
