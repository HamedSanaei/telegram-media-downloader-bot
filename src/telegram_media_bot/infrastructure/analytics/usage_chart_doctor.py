from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image

from telegram_media_bot.domain.analytics import UsageReport, UsageReportPeriod
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    PngUsageChartRenderer,
    validate_chart_font,
)


def check_usage_chart_runtime() -> dict[str, tuple[bool, str]]:
    checks: dict[str, tuple[bool, str]] = {}
    try:
        validate_chart_font()
    except Exception as exc:
        checks["usage_chart_font"] = (False, _actionable_detail(exc))
        checks["usage_chart_renderer"] = (False, "font validation failed")
        return checks
    checks["usage_chart_font"] = (True, "bundled Noto Sans decoded with required glyphs")

    try:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        report = UsageReport(
            period=UsageReportPeriod.WEEKLY,
            start_at=now,
            end_at=now,
            unique_users=0,
            interactions=0,
            downloads=0,
            succeeded=0,
            failed=0,
            cancelled=0,
            delivered_bytes=0,
            sources=(),
            formats=(),
            daily=(),
        )
        encoded = PngUsageChartRenderer().render(report)
        with Image.open(BytesIO(encoded)) as image:
            image.load()
            if image.format != "PNG" or image.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
                raise ValueError("renderer produced an unexpected image")
    except Exception as exc:
        checks["usage_chart_renderer"] = (False, _actionable_detail(exc))
    else:
        checks["usage_chart_renderer"] = (True, "in-memory PNG smoke passed")
    return checks


def _actionable_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}; reinstall the application image"
