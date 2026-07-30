from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.domain.analytics import UsageActivity, UsageReportPeriod
from telegram_media_bot.domain.models import DownloadMode, JobKind, JobStatus, OutputContainer


class FakeAnalyticsRepository:
    def __init__(self, activity: tuple[UsageActivity, ...]) -> None:
        self.activity = activity

    def load_activity(self, _start_at: datetime, _end_at: datetime) -> tuple[UsageActivity, ...]:
        return self.activity


def test_usage_report_excludes_admin_activity_without_deleting_it() -> None:
    now = datetime(2026, 7, 30, 8, tzinfo=UTC)
    activity = (
        UsageActivity(
            user_id=10,
            kind=JobKind.INSPECTION,
            status=JobStatus.SUCCEEDED,
            created_at=now - timedelta(hours=2),
        ),
        UsageActivity(
            user_id=10,
            kind=JobKind.DOWNLOAD,
            status=JobStatus.SUCCEEDED,
            created_at=now - timedelta(hours=1),
            source="youtube",
            mode=DownloadMode.VIDEO_1080,
            container=OutputContainer.MP4,
            delivered_bytes=1_024,
        ),
        UsageActivity(
            user_id=99,
            kind=JobKind.INSPECTION,
            status=JobStatus.SUCCEEDED,
            created_at=now - timedelta(hours=2),
        ),
        UsageActivity(
            user_id=99,
            kind=JobKind.DOWNLOAD,
            status=JobStatus.FAILED,
            created_at=now - timedelta(hours=1),
            source="instagram",
            mode=DownloadMode.BEST_ORIGINAL,
        ),
    )
    repository = FakeAnalyticsRepository(activity)

    report = UsageAnalyticsService(repository, admin_ids=(99,)).build(
        UsageReportPeriod.WEEKLY,
        now=now,
    )

    assert report.unique_users == 1
    assert report.interactions == 1
    assert report.downloads == 1
    assert report.succeeded == 1
    assert report.failed == 0
    assert report.delivered_bytes == 1_024
    assert report.sources[0].label == "youtube"
    assert len(repository.activity) == 4
    assert "99" not in repr(report)


def test_full_report_keeps_only_the_latest_fourteen_daily_points() -> None:
    now = datetime(2026, 7, 30, 8, tzinfo=UTC)
    report = UsageAnalyticsService(FakeAnalyticsRepository(()), admin_ids=()).build(
        UsageReportPeriod.FULL,
        now=now,
    )

    assert len(report.daily) == 14
    assert report.daily[-1].day.isoformat() == "2026-07-30"
