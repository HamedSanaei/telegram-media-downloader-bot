"""Payment Logger tests (T025).

Successful purchases flow to the durable Operator Logger outbox as safe, idempotent events; a
logger failure must never roll back a settlement, and provider secrets/references never appear.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.application.services.payment_logger import PaymentAuditLogger
from telegram_media_bot.domain.audit import AuditCategory, AuditEventType
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository


def _make(tmp_path: Path) -> tuple[PaymentAuditLogger, AuditService, SqliteAuditRepository]:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    service = AuditService(repository, enabled=True)
    return PaymentAuditLogger(service, payment_events_enabled=True), service, repository


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_purchase_confirmed_event_is_safe_and_idempotent(tmp_path: Path) -> None:
    logger, _, repository = _make(tmp_path)

    event_id = logger.log_purchase_confirmed(
        order_id="order-1",
        user_id=7,
        provider_id="uniquepay",
        plan_id="vip-1",
        plan_name="VIP",
        duration_months=1,
        amount_toman=250_000,
        currency="IRT",
        authorized_until=datetime(2026, 2, 1, tzinfo=UTC),
        confirmed_at=_now(),
    )
    duplicate = logger.log_purchase_confirmed(
        order_id="order-1",
        user_id=7,
        provider_id="uniquepay",
        plan_id="vip-1",
        plan_name="VIP",
        duration_months=1,
        amount_toman=250_000,
        currency="IRT",
        authorized_until=datetime(2026, 2, 1, tzinfo=UTC),
        confirmed_at=_now(),
    )

    assert event_id == 1
    assert duplicate == 0  # INSERT OR IGNORE on the deterministic idempotency key
    item = repository.claim_pending()[0]
    assert item.event.event_type is AuditEventType.PAYMENT_CONFIRMED
    assert item.event.category is AuditCategory.PAYMENT
    assert item.event.telegram_user_id == 7
    # Deterministic event identity: a replayed envelope is ignored, so the durable event appears once.
    assert item.event.event_id
    assert len(repository.claim_pending(limit=50)) == 0  # exactly one outbox row was enqueued
    message = item.event.message
    assert "250000 IRT" in message
    assert "uniquepay" in message
    # No provider refs or secrets anywhere in the payload.
    for forbidden in (
        "up-ref",
        "pay_id",
        "tetra",
        "hoosh",
        "token",
        "secret",
        "callback",
        "Bearer",
    ):
        assert forbidden not in message
    assert item.event.provider == "uniquepay"


def test_logger_disabled_emits_nothing(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    logger = PaymentAuditLogger(
        AuditService(repository, enabled=True), payment_events_enabled=False
    )
    assert (
        logger.log_purchase_confirmed(
            order_id="order-1",
            user_id=7,
            provider_id="uniquepay",
            plan_id="vip-1",
            plan_name="VIP",
            duration_months=1,
            amount_toman=250_000,
            currency="IRT",
            authorized_until=datetime(2026, 2, 1, tzinfo=UTC),
            confirmed_at=_now(),
        )
        == 0
    )
    assert repository.health_snapshot().pending_effects == 0


def test_audit_disabled_master_switch_silences_payment_events(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.reconcile_config((-1001234567890,))
    logger = PaymentAuditLogger(
        AuditService(repository, enabled=False), payment_events_enabled=True
    )
    assert (
        logger.log_purchase_confirmed(
            order_id="order-1",
            user_id=7,
            provider_id="uniquepay",
            plan_id="vip-1",
            plan_name="VIP",
            duration_months=1,
            amount_toman=250_000,
            currency="IRT",
            authorized_until=datetime(2026, 2, 1, tzinfo=UTC),
            confirmed_at=_now(),
        )
        == 0
    )


def test_admin_grant_event_records_actor_and_target(tmp_path: Path) -> None:
    logger, _, repository = _make(tmp_path)
    from telegram_media_bot.domain.subscriptions import EntitlementGrant, GrantId, PlanId

    grant = EntitlementGrant(
        grant_id=GrantId("grant-admin-abc"),
        user_id=7,
        plan_id=PlanId("vip-1"),
        duration_months=3,
        confirmed_at=_now(),
        source_type="admin_grant",
        source_reference="admin:99:ref",
        created_at=_now(),
    )
    logger.log_admin_vip_granted(
        actor_user_id=99,
        target_user_id=7,
        grant=grant,
        authorized_until=datetime(2026, 4, 1, tzinfo=UTC),
        now=_now(),
    )
    item = repository.claim_pending()[0]
    assert item.event.event_type is AuditEventType.ADMIN_VIP_GRANTED
    assert "admin_id: 99" in item.event.message
    assert "user_id: 7" in item.event.message
    assert item.event.telegram_user_id == 7
    assert "grant-admin-abc" not in item.event.message  # internal grant id is not user-facing


def test_admin_revoke_event_is_safe(tmp_path: Path) -> None:
    logger, _, repository = _make(tmp_path)
    logger.log_admin_vip_revoked(
        actor_user_id=99,
        target_user_id=7,
        grant_ids=("grant-admin-abc",),
        authorized_until=None,
        now=_now(),
    )
    item = repository.claim_pending()[0]
    assert item.event.event_type is AuditEventType.ADMIN_VIP_REVOKED
    assert "admin_id: 99" in item.event.message
    assert "grant-admin-abc" not in item.event.message
