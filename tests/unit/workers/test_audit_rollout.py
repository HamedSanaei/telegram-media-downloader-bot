from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditDeliveryResult,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    LoggerHealthSnapshot,
    LoggerOutboxItem,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.workers.jobs import audit_dispatch_job

if TYPE_CHECKING:
    from telegram_media_bot.bootstrap.config import Settings


class SuccessfulDelivery:
    async def deliver(self, _item: LoggerOutboxItem) -> AuditDeliveryResult:
        return AuditDeliveryResult(AuditDeliveryOutcome.SUCCEEDED)


def test_dispatcher_has_independent_bounded_thirty_second_schedule() -> None:
    from telegram_media_bot.workers.settings import WorkerSettings

    jobs = {job.name: job for job in WorkerSettings.cron_jobs}

    dispatcher = jobs["cron:audit_dispatch_job"]
    assert dispatcher.second == {5, 35}
    assert dispatcher.minute is None
    assert not dispatcher.run_at_startup


def test_operational_alert_gate_is_independent_from_submission_mirror(
    settings: Settings,
) -> None:
    from telegram_media_bot.workers.settings import _worker_alerts_enabled

    mirror_only = settings.model_copy(
        update={
            "telegram": settings.telegram.model_copy(
                update={
                    "logger": settings.telegram.logger.model_copy(
                        update={"enabled": True, "submission_mirror_enabled": True}
                    )
                }
            )
        }
    )
    alerts = mirror_only.model_copy(
        update={
            "telegram": mirror_only.telegram.model_copy(
                update={
                    "logger": mirror_only.telegram.logger.model_copy(
                        update={"alerts_enabled": True}
                    )
                }
            )
        }
    )

    assert not _worker_alerts_enabled(settings)
    assert not _worker_alerts_enabled(mirror_only)
    assert _worker_alerts_enabled(alerts)


def _event() -> AuditEvent:
    return AuditEvent(
        event_id="rollout-event",
        event_type=AuditEventType.SYSTEM_HEALTH,
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        occurred_at=datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
        correlation_id="rollout-event",
        message="safe rollout event",
    )


async def test_bounded_worker_dispatch_updates_only_safe_aggregate_metrics(
    tmp_path: Path,
) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    channel = -1001234567890
    repository.reconcile_config((channel,))
    repository.enqueue(_event())
    metrics = MetricsRegistry()
    processor = AuditOutboxProcessor(
        repository,
        SuccessfulDelivery(),
        observer=lambda outcome, category: metrics.record_audit_delivery(
            outcome=outcome.value, category=category.value
        ),
    )

    completed = await audit_dispatch_job(
        {"audit_processor": processor, "audit_store": repository, "metrics": metrics}
    )

    rendered = metrics.render()
    assert completed == 1
    assert 'outcome="succeeded",category="system"} 1' in rendered
    assert "media_bot_audit_outbox_pending 0" in rendered
    assert str(channel) not in rendered
    assert "safe rollout event" not in rendered


def test_logger_health_is_safe_secondary_readiness_detail(settings: Settings) -> None:
    from telegram_media_bot.workers.settings import _logger_health_component

    logger_settings = settings.telegram.logger.model_copy(
        update={
            "enabled": True,
            "channels": (-1001234567890,),
            "alerts_enabled": True,
            "submission_mirror_enabled": True,
            "operator_privacy_attested": True,
        }
    )
    configured = settings.model_copy(
        update={"telegram": settings.telegram.model_copy(update={"logger": logger_settings})}
    )
    snapshot = LoggerHealthSnapshot(
        effective_destinations=1,
        active_destinations=0,
        unreachable_destinations=1,
        forbidden_destinations=0,
        disabled_destinations=0,
        pending_effects=2,
        retryable_effects=1,
        uncertain_effects=1,
        terminal_effects=0,
        oldest_pending_age_seconds=45,
    )

    component = _logger_health_component(configured, snapshot)

    assert component.name == "operator_logger"
    assert component.healthy  # Logger degradation must not block ordinary download readiness.
    assert "state=degraded" in component.detail
    assert "enabled=1" in component.detail
    assert "pending=2" in component.detail
    assert "oldest_pending_seconds=45" in component.detail
    assert "alerts=1" in component.detail
    assert "mirror=1" in component.detail
    assert "-1001234567890" not in component.detail
