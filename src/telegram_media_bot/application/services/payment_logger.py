"""Operator Logger for payment/VIP events (T025, sections 21-23).

Successful purchases and admin VIP mutations are emitted as typed PAYMENT-category audit events
with deterministic idempotency keys (``payment-confirmed:<order_id>`` etc.), so duplicate
callbacks/reconciliation never duplicate the audit log. Logger delivery is secondary: storage or
delivery failure never rolls back a settled payment or a granted gift.

The master kill switch remains ``telegram.logger.enabled``; the independent
``payment_events_enabled`` switch controls only purchase/admin payment events and never depends on
submission mirroring.
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_media_bot.application.services.audit_service import AuditService
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEventType,
    AuditSeverity,
)
from telegram_media_bot.domain.subscriptions import EntitlementGrant

_CONFIRMED_SEP = chr(10)


class PaymentAuditLogger:
    """Safe, idempotent payment/VIP audit events for the Operator Logger."""

    def __init__(self, audit: AuditService, *, payment_events_enabled: bool) -> None:
        self._audit = audit
        self._enabled = payment_events_enabled

    def log_purchase_confirmed(
        self,
        *,
        order_id: str,
        user_id: int,
        provider_id: str,
        plan_id: str,
        plan_name: str,
        duration_months: int,
        amount_toman: int,
        currency: str,
        authorized_until: datetime,
        confirmed_at: datetime,
    ) -> int:
        """One safe successful-purchase event. Never contains provider references, tokens,
        callbacks, or secrets - only the durable local facts the operator needs."""
        if not self._enabled:
            return 0
        sep = _CONFIRMED_SEP
        message = f"""✅ خرید VIP تایید شد{sep}user_id: {user_id}{sep}provider: {provider_id}{sep}plan: {plan_name}{sep}plan_id: {plan_id}{sep}duration_months: {duration_months}{sep}amount: {amount_toman} {currency}{sep}authorized_until: {authorized_until.isoformat()}{sep}confirmed_at: {confirmed_at.isoformat()}"""
        return self._audit.emit(
            event_type=AuditEventType.PAYMENT_CONFIRMED,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.INFO,
            correlation_id=f"order:{order_id}",
            message=message,
            telegram_user_id=user_id,
            provider=provider_id,
            idempotency_key=f"payment-confirmed:{order_id}",
            occurred_at=confirmed_at,
        )

    def log_payment_refunded(
        self,
        *,
        order_id: str,
        user_id: int,
        provider_id: str,
        reason: str,
        reversed_at: datetime,
    ) -> int:
        if not self._enabled:
            return 0
        sep = _CONFIRMED_SEP
        return self._audit.emit(
            event_type=AuditEventType.PAYMENT_REFUNDED,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.WARNING,
            correlation_id=f"order:{order_id}",
            message=f"""↩️ پرداخت برگشت خورد{sep}user_id: {user_id}{sep}provider: {provider_id}{sep}reason: {reason[:200]}{sep}reversed_at: {reversed_at.isoformat()}""",
            telegram_user_id=user_id,
            provider=provider_id,
            idempotency_key=f"payment-refunded:{order_id}",
            occurred_at=reversed_at,
        )

    # -- admin VIP management ---------------------------------------------------

    def log_admin_vip_granted(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        grant: EntitlementGrant,
        authorized_until: datetime | None,
        now: datetime,
    ) -> int:
        if not self._enabled:
            return 0
        sep = _CONFIRMED_SEP
        expiry = authorized_until.isoformat() if authorized_until else "-"
        return self._audit.emit(
            event_type=AuditEventType.ADMIN_VIP_GRANTED,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.INFO,
            correlation_id=f"grant:{grant.grant_id!s}",
            message=f"""🎁 VIP هدیه داده شد{sep}user_id: {target_user_id}{sep}admin_id: {actor_user_id}{sep}plan_id: {grant.plan_id!s}{sep}duration_months: {grant.duration_months}{sep}authorized_until: {expiry}{sep}at: {now.isoformat()}""",
            telegram_user_id=target_user_id,
            idempotency_key=f"admin-vip-granted:{grant.grant_id!s}",
            occurred_at=now,
        )

    def log_admin_vip_revoked(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        grant_ids: tuple[str, ...],
        authorized_until: datetime | None,
        now: datetime,
    ) -> int:
        if not self._enabled or not grant_ids:
            return 0
        sep = _CONFIRMED_SEP
        expiry = authorized_until.isoformat() if authorized_until else "-"
        return self._audit.emit(
            event_type=AuditEventType.ADMIN_VIP_REVOKED,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.WARNING,
            correlation_id=f"user:{target_user_id}:revoke",
            message=f"""↩️ VIP هدیه پس گرفته شد{sep}user_id: {target_user_id}{sep}admin_id: {actor_user_id}{sep}revoked_grants: {len(grant_ids)}{sep}authorized_until: {expiry}{sep}at: {now.isoformat()}""",
            telegram_user_id=target_user_id,
            idempotency_key=f"admin-vip-revoked:{target_user_id}:{now.isoformat()}",
            occurred_at=now,
        )

    def log_admin_vip_suspension(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        suspended: bool,
        reason: str | None,
        now: datetime,
    ) -> int:
        if not self._enabled:
            return 0
        sep = _CONFIRMED_SEP
        event_type = (
            AuditEventType.ADMIN_VIP_SUSPENDED
            if suspended
            else AuditEventType.ADMIN_VIP_UNSUSPENDED
        )
        label = "🚫 دسترسی VIP به‌طور موقت قطع شد" if suspended else "✅ دسترسی VIP باز شد"
        return self._audit.emit(
            event_type=event_type,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.WARNING if suspended else AuditSeverity.INFO,
            correlation_id=f"user:{target_user_id}:suspension",
            message=f"""{label}{sep}user_id: {target_user_id}{sep}admin_id: {actor_user_id}{sep}reason: {(reason or "-")[:200]}{sep}at: {now.isoformat()}""",
            telegram_user_id=target_user_id,
            idempotency_key=f"admin-vip-suspended:{target_user_id}:{now.isoformat()}",
            occurred_at=now,
        )

    def log_admin_plan_changed(
        self,
        *,
        actor_user_id: int,
        plan_id: str,
        action: str,
        now: datetime,
    ) -> int:
        if not self._enabled:
            return 0
        sep = _CONFIRMED_SEP
        return self._audit.emit(
            event_type=AuditEventType.ADMIN_PLAN_CHANGED,
            category=AuditCategory.PAYMENT,
            severity=AuditSeverity.INFO,
            correlation_id=f"plan:{plan_id}:{action}",
            message=f"""📋 تغییر پلن VIP{sep}plan_id: {plan_id}{sep}admin_id: {actor_user_id}{sep}action: {action[:64]}{sep}at: {now.isoformat()}""",
            telegram_user_id=actor_user_id,
            idempotency_key=f"admin-plan-changed:{plan_id}:{action}:{now.isoformat()}",
            occurred_at=now,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["PaymentAuditLogger", "utc_now"]
