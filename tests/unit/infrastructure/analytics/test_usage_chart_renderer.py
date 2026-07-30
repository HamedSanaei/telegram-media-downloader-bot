from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from telegram_media_bot.domain.analytics import UsageDailyPoint, UsageReportPeriod
from telegram_media_bot.domain.errors import UsageChartFontError
from telegram_media_bot.infrastructure.analytics import usage_chart_renderer as renderer_module
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    PngUsageChartRenderer,
    chart_font_bytes,
    load_chart_font,
    validate_chart_font,
)
from telegram_media_bot.infrastructure.analytics.usage_chart_smoke import (
    TEXT_REGIONS,
    fixture_report,
    verify_chart_image,
)


@pytest.fixture(autouse=True)
def clear_font_caches() -> Generator[None]:
    chart_font_bytes.cache_clear()
    load_chart_font.cache_clear()
    yield
    chart_font_bytes.cache_clear()
    load_chart_font.cache_clear()


def test_bundled_font_and_license_are_readable_from_source_tree() -> None:
    root = Path("src/telegram_media_bot/assets/fonts")
    font = root / "NotoSans-Regular.ttf"
    license_file = root / "OFL.txt"

    assert font.stat().st_size > 500_000
    assert (
        sha256(font.read_bytes()).hexdigest()
        == (
            "b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5"  # pragma: allowlist secret
        )
    )
    assert "SIL OPEN FONT LICENSE" in license_file.read_text(encoding="utf-8")
    assert chart_font_bytes() == font.read_bytes()


def test_font_decodes_at_multiple_sizes_and_loader_is_cached() -> None:
    small = load_chart_font(18)
    same_small = load_chart_font(18)
    large = load_chart_font(58)

    validate_chart_font()
    assert small is same_small
    assert small is not large
    assert small.getmask("DownloadKade 123 %/-").getbbox() is not None


def test_missing_font_raises_actionable_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_files(_package: str) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(renderer_module, "files", missing_files)

    with pytest.raises(UsageChartFontError, match="missing"):
        chart_font_bytes()


def test_corrupt_font_never_falls_back_to_default_bitmap_font(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(renderer_module, "chart_font_bytes", lambda: b"not-a-font")

    with pytest.raises(UsageChartFontError, match="cannot be decoded"):
        load_chart_font(24)


@pytest.mark.parametrize("period", [UsageReportPeriod.WEEKLY, UsageReportPeriod.MONTHLY])
def test_dashboard_png_has_dimensions_mode_and_all_text_regions(
    period: UsageReportPeriod,
) -> None:
    encoded = PngUsageChartRenderer().render(fixture_report(period))

    counts = verify_chart_image(encoded)
    with Image.open(BytesIO(encoded)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (IMAGE_WIDTH, IMAGE_HEIGHT)
    assert set(counts) == set(TEXT_REGIONS)


@pytest.mark.parametrize(
    "points",
    [
        (),
        (UsageDailyPoint(fixture_report(UsageReportPeriod.WEEKLY).daily[0].day),),
        (
            UsageDailyPoint(
                fixture_report(UsageReportPeriod.WEEKLY).daily[0].day,
                interactions=9_999_999,
                downloads=8_888_888,
                succeeded=7_777_777,
                delivered_bytes=9_999_999_999,
            ),
        ),
    ],
)
def test_renderer_handles_zero_one_and_large_values(
    points: tuple[UsageDailyPoint, ...],
) -> None:
    base = fixture_report(UsageReportPeriod.WEEKLY)
    report = replace(base, daily=points)

    encoded = PngUsageChartRenderer().render(report)

    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")


def test_renderer_is_deterministic_does_not_mutate_input_or_write_to_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = fixture_report(UsageReportPeriod.MONTHLY)
    before = replace(report)
    monkeypatch.chdir(tmp_path)
    renderer = PngUsageChartRenderer()

    first = renderer.render(report)
    second = renderer.render(report)

    assert first == second
    assert report == before
    assert list(tmp_path.iterdir()) == []


def test_renderer_is_thread_safe() -> None:
    renderer = PngUsageChartRenderer()
    report = fixture_report(UsageReportPeriod.MONTHLY)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda _index: renderer.render(report), range(8)))

    assert len(set(results)) == 1


def test_monthly_fixture_uses_sparse_regular_labels_with_first_and_last_dates() -> None:
    report = fixture_report(UsageReportPeriod.MONTHLY)
    encoded = PngUsageChartRenderer().render(report)
    counts = verify_chart_image(encoded)

    assert len(report.daily) == 30
    assert counts["x_axis_labels"] > 200
