"""Framework-free Operator Logger contracts for T026 and T027."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

_SAFE_CLASSIFICATION = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_+-][A-Za-z0-9_.:+-]{0,191}$")


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
    DOWNLOAD_OUTPUT_DELIVERED = "download_output_delivered"
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


_EVENT_CATEGORIES = {
    AuditEventType.TERMINAL_OPERATIONAL_ERROR: AuditCategory.ERROR,
    AuditEventType.COOKIE_HEALTH_CHANGED: AuditCategory.COOKIE_HEALTH,
    AuditEventType.USER_SUBMISSION_RECEIVED: AuditCategory.USER_SUBMISSION,
    AuditEventType.DOWNLOAD_OUTPUT_DELIVERED: AuditCategory.USER_SUBMISSION,
    AuditEventType.SYSTEM_HEALTH: AuditCategory.SYSTEM,
}


@dataclass(frozen=True, slots=True)
class TelegramSourceReference:
    chat_id: int
    message_ids: tuple[int, ...]
    media_group_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.chat_id) is not int or self.chat_id == 0:
            raise ValueError("source chat ID must be a non-zero integer")
        if not isinstance(self.message_ids, tuple) or not self.message_ids:
            raise ValueError("source message IDs must be a non-empty tuple")
        if any(type(item) is not int or item <= 0 for item in self.message_ids):
            raise ValueError("source message IDs must be positive")
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("source message IDs must be unique")
        if self.media_group_id is not None and (
            not isinstance(self.media_group_id, str)
            or not _SAFE_IDENTIFIER.fullmatch(self.media_group_id)
        ):
            raise ValueError("media_group_id must be a bounded safe identifier")


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
        if not isinstance(self.event_type, AuditEventType):
            raise ValueError("event_type must be a typed audit event type")
        if not isinstance(self.category, AuditCategory):
            raise ValueError("category must be a typed audit category")
        if not isinstance(self.severity, AuditSeverity):
            raise ValueError("severity must be a typed audit severity")
        if _EVENT_CATEGORIES[self.event_type] is not self.category:
            raise ValueError("audit event type and category do not match")
        if not isinstance(self.event_id, str) or not isinstance(self.correlation_id, str):
            raise ValueError("audit identity and correlation must be strings")
        if not self.event_id or not self.correlation_id:
            raise ValueError("audit identity and correlation are required")
        for label, value in (
            ("event_id", self.event_id),
            ("correlation_id", self.correlation_id),
            ("job_id", self.job_id),
        ):
            if value is not None and (
                not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value)
            ):
                raise ValueError(f"{label} must be a bounded safe identifier")
        _require_utc(self.occurred_at, "audit occurred_at")
        if (
            not isinstance(self.message, str)
            or not self.message.strip()
            or len(self.message) > 2000
        ):
            raise ValueError("audit message must be 1..2000 characters")
        if self.telegram_user_id is not None and (
            type(self.telegram_user_id) is not int or self.telegram_user_id <= 0
        ):
            raise ValueError("telegram_user_id must be a positive numeric user ID")
        if self.update_id is not None and (type(self.update_id) is not int or self.update_id < 0):
            raise ValueError("update_id must be a non-negative integer")
        for label, value in (("content_type", self.content_type), ("provider", self.provider)):
            if value is not None and (
                not isinstance(value, str) or not _SAFE_CLASSIFICATION.fullmatch(value)
            ):
                raise ValueError(f"{label} must be a bounded safe classification")
        if self.source is not None and not isinstance(self.source, TelegramSourceReference):
            raise ValueError("source must be a typed Telegram source reference")
        if (
            self.event_type
            in {
                AuditEventType.USER_SUBMISSION_RECEIVED,
                AuditEventType.DOWNLOAD_OUTPUT_DELIVERED,
            }
            and self.source is None
        ):
            raise ValueError("Telegram copy events require a source reference")


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
    effective_destinations: int
    active_destinations: int
    unreachable_destinations: int
    forbidden_destinations: int
    disabled_destinations: int
    pending_effects: int
    retryable_effects: int
    uncertain_effects: int
    terminal_effects: int
    oldest_pending_age_seconds: int


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
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
