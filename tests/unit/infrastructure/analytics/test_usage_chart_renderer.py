from __future__ import annotations

import struct
from datetime import UTC, date, datetime

from telegram_media_bot.domain.analytics import (
    UsageDailyPoint,
    UsageReport,
    UsageReportPeriod,
)
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import (
    PngUsageChartRenderer,
)


def test_renderer_creates_a_real_png_chart() -> None:
    report = UsageReport(
        period=UsageReportPeriod.WEEKLY,
        start_at=datetime(2026, 7, 24, tzinfo=UTC),
        end_at=datetime(2026, 7, 30, tzinfo=UTC),
        unique_users=2,
        interactions=5,
        downloads=4,
        succeeded=3,
        failed=1,
        cancelled=0,
        delivered_bytes=2048,
        sources=(),
        formats=(),
        daily=(
            UsageDailyPoint(date(2026, 7, 29), interactions=2, downloads=1, succeeded=1),
            UsageDailyPoint(date(2026, 7, 30), interactions=3, downloads=3, succeeded=2),
        ),
    )

    image = PngUsageChartRenderer().render(report)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", image[16:24]) == (1000, 560)
    assert len(image) > 2_000
