"""Framework-free Operator Logger contracts for T026 and T027."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

_SAFE_CLASSIFICATION = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


class AuditCategory(StrEnum):
    ERROR = "error"
    COOKIE_HEALTH = "cookie_health"
    USER_SUBMISSION = "user_submission"
    SYSTEM = "system"


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditEventType(StrEnum):
    TERMINAL_OPERATIONAL_ERROR = "terminal_operational_error"
    COOKIE_HEALTH_CHANGED = "cookie_health_changed"
    USER_SUBMISSION_RECEIVED = "user_submission_received"
    SYSTEM_HEALTH = "system_health"


class LoggerDestinationSource(StrEnum):
    CONFIG = "config"
    RUNTIME = "runtime"


class LoggerDestinationHealth(StrEnum):
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    FORBIDDEN = "forbidden"
    DISABLED = "disabled"


class LoggerOutboxState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENDING = "sending"
    RETRYABLE = "retryable"
    SUCCEEDED = "succeeded"
    UNCERTAIN = "uncertain"
    FAILED_TERMINAL = "failed_terminal"


class AuditDeliveryOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    UNCERTAIN = "uncertain"
    FAILED_TERMINAL = "failed_terminal"


class DestinationProbeOutcome(StrEnum):
    OK = "ok"
    NOT_CHANNEL = "not_channel"
    BOT_NOT_MEMBER = "bot_not_member"
    FORBIDDEN = "forbidden"
    UNREACHABLE = "unreachable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class TelegramSourceReference:
    chat_id: int
    message_ids: tuple[int, ...]
    media_group_id: str | None = None

    def __post_init__(self) -> None:
        if not self.message_ids or any(item <= 0 for item in self.message_ids):
            raise ValueError("source message IDs must be positive")
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("source message IDs must be unique")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: AuditEventType
    category: AuditCategory
    severity: AuditSeverity
    occurred_at: datetime
    correlation_id: str
    message: str
    telegram_user_id: int | None = None
    update_id: int | None = None
    job_id: str | None = None
    content_type: str | None = None
    provider: str | None = None
    source: TelegramSourceReference | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.correlation_id:
            raise ValueError("audit identity and correlation are required")
        _require_utc(self.occurred_at, "audit occurred_at")
        if not self.message.strip() or len(self.message) > 2000:
            raise ValueError("audit message must be 1..2000 characters")
        for label, value in (("content_type", self.content_type), ("provider", self.provider)):
            if value is not None and not _SAFE_CLASSIFICATION.fullmatch(value):
                raise ValueError(f"{label} must be a bounded safe classification")


@dataclass(frozen=True, slots=True)
class LoggerDestination:
    chat_id: int
    ownership: frozenset[LoggerDestinationSource]
    enabled: bool
    health: LoggerDestinationHealth
    created_at: datetime
    updated_at: datetime
    last_failure_class: str | None = None

    def __post_init__(self) -> None:
        if self.chat_id > -1000000000000:
            raise ValueError("logger destination must be a numeric -100... Telegram channel ID")
        if not self.ownership:
            raise ValueError("effective logger destination must have an owner")
        _require_utc(self.created_at, "destination created_at")
        _require_utc(self.updated_at, "destination updated_at")

    @property
    def config_owned(self) -> bool:
        return LoggerDestinationSource.CONFIG in self.ownership

    @property
    def runtime_owned(self) -> bool:
        return LoggerDestinationSource.RUNTIME in self.ownership


@dataclass(frozen=True, slots=True)
class LoggerOutboxItem:
    event: AuditEvent
    destination_chat_id: int
    state: LoggerOutboxState
    attempt_count: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class AuditDeliveryResult:
    outcome: AuditDeliveryOutcome
    failure_class: str | None = None


@dataclass(frozen=True, slots=True)
class DestinationProbeResult:
    outcome: DestinationProbeOutcome
    failure_class: str | None = None


@dataclass(frozen=True, slots=True)
class LoggerHealthSnapshot:
    active_destinations: int
    forbidden_destinations: int
    pending_effects: int
    retryable_effects: int
    uncertain_effects: int
    terminal_effects: int


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be UTC")


__all__ = [
    "AuditCategory",
    "AuditDeliveryOutcome",
    "AuditDeliveryResult",
    "AuditEvent",
    "AuditEventType",
    "AuditSeverity",
    "DestinationProbeOutcome",
    "DestinationProbeResult",
    "LoggerDestination",
    "LoggerDestinationHealth",
    "LoggerDestinationSource",
    "LoggerHealthSnapshot",
    "LoggerOutboxItem",
    "LoggerOutboxState",
    "TelegramSourceReference",
]
