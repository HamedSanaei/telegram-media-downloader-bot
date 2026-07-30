from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from telegram_media_bot.domain.models import DownloadMode, JobKind, JobStatus, OutputContainer


class UsageReportPeriod(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class UsageActivity:
    user_id: int
    kind: JobKind
    status: JobStatus
    created_at: datetime
    source: str | None = None
    mode: DownloadMode | None = None
    container: OutputContainer | None = None
    delivered_bytes: int = 0


@dataclass(frozen=True, slots=True)
class UsageDailyPoint:
    day: date
    interactions: int = 0
    downloads: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    delivered_bytes: int = 0


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class UsageReport:
    period: UsageReportPeriod
    start_at: datetime
    end_at: datetime
    unique_users: int
    interactions: int
    downloads: int
    succeeded: int
    failed: int
    cancelled: int
    delivered_bytes: int
    sources: tuple[UsageBreakdown, ...]
    formats: tuple[UsageBreakdown, ...]
    daily: tuple[UsageDailyPoint, ...]
