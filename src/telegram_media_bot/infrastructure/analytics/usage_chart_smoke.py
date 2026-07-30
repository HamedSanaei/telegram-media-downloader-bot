from __future__ import annotations

import argparse
import os
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from PIL import Image

from telegram_media_bot.domain.analytics import UsageDailyPoint, UsageReport, UsageReportPeriod
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    PngUsageChartRenderer,
    validate_chart_font,
)

TEHRAN = ZoneInfo("Asia/Tehran")

TEXT_REGIONS = {
    "brand": (60, 30, 500, 85),
    "title": (60, 80, 900, 155),
    "date_range": (60, 155, 1050, 215),
    "kpi_label": (85, 265, 390, 320),
    "kpi_value": (85, 325, 390, 420),
    "legend_labels": (760, 495, 2050, 570),
    "y_axis_labels": (80, 630, 185, 1305),
    "x_axis_labels": (170, 1285, 2130, 1360),
    "value_labels": (180, 620, 2120, 1275),
}


def fixture_report(period: UsageReportPeriod) -> UsageReport:
    count = 7 if period is UsageReportPeriod.WEEKLY else 30
    end_day = date(2026, 7, 30)
    start_day = end_day - timedelta(days=count - 1)
    points = tuple(
        UsageDailyPoint(
            day=start_day + timedelta(days=index),
            interactions=(index * 7 + 3) % 23,
            downloads=(index * 5 + 2) % 17,
            succeeded=(index * 3 + 1) % 13,
            delivered_bytes=(index + 1) * 12_500_000,
        )
        for index in range(count)
    )
    return UsageReport(
        period=period,
        start_at=datetime.combine(start_day, datetime.min.time(), tzinfo=TEHRAN),
        end_at=datetime.combine(end_day, datetime.max.time(), tzinfo=TEHRAN),
        unique_users=1_248,
        interactions=sum(point.interactions for point in points),
        downloads=sum(point.downloads for point in points),
        succeeded=sum(point.succeeded for point in points),
        failed=8,
        cancelled=3,
        delivered_bytes=sum(point.delivered_bytes for point in points),
        sources=(),
        formats=(),
        daily=points,
    )


def verify_chart_image(encoded: bytes) -> dict[str, int]:
    with Image.open(BytesIO(encoded)) as image:
        image.load()
        if image.format != "PNG":
            raise ValueError("usage chart is not PNG")
        if image.mode not in {"RGB", "RGBA"}:
            raise ValueError("usage chart has an unsupported color mode")
        if image.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
            raise ValueError("usage chart dimensions do not match the visual contract")
        rgb = image.convert("RGB")
        counts = {name: _dark_pixel_count(rgb, region) for name, region in TEXT_REGIONS.items()}
    minimums = {
        "brand": 250,
        "title": 800,
        "date_range": 400,
        "kpi_label": 150,
        "kpi_value": 150,
        "legend_labels": 500,
        "y_axis_labels": 120,
        "x_axis_labels": 200,
        "value_labels": 300,
    }
    missing = [name for name, minimum in minimums.items() if counts[name] < minimum]
    if missing:
        raise ValueError("usage chart text regions are blank: " + ", ".join(missing))
    return counts


def render_smoke_artifacts(output_dir: Path) -> tuple[Path, Path]:
    validate_chart_font()
    output_dir.mkdir(parents=True, exist_ok=True)
    renderer = PngUsageChartRenderer()
    paths: list[Path] = []
    for period in (UsageReportPeriod.WEEKLY, UsageReportPeriod.MONTHLY):
        encoded = renderer.render(fixture_report(period))
        verify_chart_image(encoded)
        path = output_dir / f"usage-chart-{period.value}-smoke.png"
        path.write_bytes(encoded)
        paths.append(path)
    return paths[0], paths[1]


def _dark_pixel_count(image: Image.Image, region: tuple[int, int, int, int]) -> int:
    pixels = cast(
        Iterable[tuple[int, int, int]],
        image.crop(region).get_flattened_data(),
    )
    return sum(
        1 for red, green, blue in pixels if red * 2126 + green * 7152 + blue * 722 < 1_750_000
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deterministic usage chart smoke artifacts")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify-uid", type=int)
    args = parser.parse_args()
    if args.verify_uid is not None and hasattr(os, "getuid") and os.getuid() != args.verify_uid:
        raise SystemExit(f"Expected UID {args.verify_uid}, got {os.getuid()}")
    for path in render_smoke_artifacts(args.output_dir):
        print(path)


if __name__ == "__main__":
    main()
