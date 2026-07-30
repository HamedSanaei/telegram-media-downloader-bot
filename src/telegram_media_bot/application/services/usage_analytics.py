from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram_media_bot.application.ports.usage_analytics import UsageAnalyticsRepository
from telegram_media_bot.domain.analytics import (
    UsageActivity,
    UsageBreakdown,
    UsageDailyPoint,
    UsageReport,
    UsageReportPeriod,
)
from telegram_media_bot.domain.models import JobKind, JobStatus

TEHRAN = ZoneInfo("Asia/Tehran")


class UsageAnalyticsService:
    def __init__(
        self,
        repository: UsageAnalyticsRepository,
        *,
        admin_ids: tuple[int, ...],
    ) -> None:
        self._repository = repository
        self._admin_ids = frozenset(admin_ids)

    def build(
        self,
        period: UsageReportPeriod,
        *,
        now: datetime | None = None,
    ) -> UsageReport:
        current = (now or datetime.now(UTC)).astimezone(TEHRAN)
        end_local = current
        if period is UsageReportPeriod.WEEKLY:
            start_day = current.date() - timedelta(days=6)
        elif period is UsageReportPeriod.MONTHLY:
            start_day = current.date() - timedelta(days=29)
        else:
            start_day = date(1970, 1, 1)
        start_local = datetime.combine(start_day, time.min, tzinfo=TEHRAN)
        activities = self._repository.load_activity(
            start_local.astimezone(UTC),
            end_local.astimezone(UTC),
        )
        public = tuple(item for item in activities if item.user_id not in self._admin_ids)
        user_ids = {item.user_id for item in public}
        interactions = sum(item.kind is JobKind.INSPECTION for item in public)
        downloads = tuple(item for item in public if item.kind is JobKind.DOWNLOAD)
        succeeded = sum(item.status is JobStatus.SUCCEEDED for item in downloads)
        cancelled = sum(item.status is JobStatus.CANCELLED for item in downloads)
        failed = sum(
            item.status in {JobStatus.FAILED, JobStatus.DELIVERY_UNCERTAIN} for item in downloads
        )
        source_counts = Counter(item.source or "unknown" for item in downloads)
        format_counts = Counter(
            " / ".join(
                value
                for value in (
                    item.mode.value if item.mode is not None else "unknown",
                    item.container.value.upper() if item.container is not None else "",
                )
                if value
            )
            for item in downloads
        )
        daily_start = (
            max(start_day, current.date() - timedelta(days=13))
            if period is UsageReportPeriod.FULL
            else start_day
        )
        daily = _daily_points(public, daily_start, current.date())
        return UsageReport(
            period=period,
            start_at=start_local,
            end_at=end_local,
            unique_users=len(user_ids),
            interactions=interactions,
            downloads=len(downloads),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            delivered_bytes=sum(
                max(0, item.delivered_bytes)
                for item in downloads
                if item.status is JobStatus.SUCCEEDED
            ),
            sources=_breakdown(source_counts),
            formats=_breakdown(format_counts),
            daily=daily,
        )


def _daily_points(
    activities: tuple[UsageActivity, ...],
    start_day: date,
    end_day: date,
) -> tuple[UsageDailyPoint, ...]:
    points: list[UsageDailyPoint] = []
    day = start_day
    while day <= end_day:
        items = tuple(
            item for item in activities if item.created_at.astimezone(TEHRAN).date() == day
        )
        downloads = tuple(item for item in items if item.kind is JobKind.DOWNLOAD)
        points.append(
            UsageDailyPoint(
                day=day,
                interactions=sum(item.kind is JobKind.INSPECTION for item in items),
                downloads=len(downloads),
                succeeded=sum(item.status is JobStatus.SUCCEEDED for item in downloads),
                failed=sum(
                    item.status in {JobStatus.FAILED, JobStatus.DELIVERY_UNCERTAIN}
                    for item in downloads
                ),
                cancelled=sum(item.status is JobStatus.CANCELLED for item in downloads),
                delivered_bytes=sum(
                    max(0, item.delivered_bytes)
                    for item in downloads
                    if item.status is JobStatus.SUCCEEDED
                ),
            )
        )
        day += timedelta(days=1)
    return tuple(points)


def _breakdown(counts: Counter[str]) -> tuple[UsageBreakdown, ...]:
    return tuple(
        UsageBreakdown(label=label, count=count)
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
