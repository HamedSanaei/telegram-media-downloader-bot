from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from io import BytesIO
from math import ceil, log10
from threading import RLock
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont

from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.domain.analytics import UsageDailyPoint, UsageReport, UsageReportPeriod
from telegram_media_bot.domain.errors import UsageChartFontError

IMAGE_WIDTH = 2200
IMAGE_HEIGHT = 1450

_FONT_PACKAGE = "telegram_media_bot.assets.fonts"
_FONT_NAME = "NotoSans-Regular.ttf"
_REQUIRED_GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789%/-"
_RENDER_LOCK = RLock()
_TEHRAN = ZoneInfo("Asia/Tehran")

_BACKGROUND = "#F3F6FB"
_PANEL = "#FFFFFF"
_BORDER = "#D9E2EF"
_GRID = "#DCE4EF"
_HEADING = "#17233F"
_TEXT = "#45516A"
_MUTED = "#71809D"
_INTERACTIONS = "#3674D9"
_DOWNLOADS = "#1D9E75"
_SUCCESS = "#845EC2"
_SERIES = (
    ("Interactions", _INTERACTIONS, "interactions"),
    ("Download Requests", _DOWNLOADS, "downloads"),
    ("Successful Downloads", _SUCCESS, "succeeded"),
)


@lru_cache(maxsize=1)
def chart_font_bytes() -> bytes:
    try:
        data = files(_FONT_PACKAGE).joinpath(_FONT_NAME).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise UsageChartFontError(
            "Bundled usage chart font is missing; reinstall the application package."
        ) from exc
    if not data:
        raise UsageChartFontError(
            "Bundled usage chart font is empty; reinstall the application package."
        )
    return data


@lru_cache(maxsize=16)
def load_chart_font(size: int) -> FreeTypeFont:
    if size <= 0:
        raise UsageChartFontError("Usage chart font size must be positive.")
    try:
        font = ImageFont.truetype(BytesIO(chart_font_bytes()), size=size)
    except (OSError, ValueError) as exc:
        raise UsageChartFontError(
            "Bundled usage chart font cannot be decoded by Pillow; reinstall the package."
        ) from exc
    missing = [glyph for glyph in _REQUIRED_GLYPHS if font.getmask(glyph).getbbox() is None]
    if missing:
        raise UsageChartFontError(
            "Bundled usage chart font lacks required ASCII glyphs: " + "".join(missing)
        )
    return font


def validate_chart_font() -> None:
    """Validate the packaged resource, Pillow decoder, and required chart glyphs."""

    chart_font_bytes()
    load_chart_font(24)


class PngUsageChartRenderer(UsageChartRenderer):
    """Render a deterministic, in-memory Pillow usage dashboard with bundled fonts."""

    def render(self, report: UsageReport) -> bytes:
        with _RENDER_LOCK:
            return _render_dashboard(report)


def _render_dashboard(report: UsageReport) -> bytes:
    with Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), _BACKGROUND) as image:
        draw = ImageDraw.Draw(image)
        _draw_header(draw, report)
        _draw_kpis(draw, report)
        _draw_plot(draw, report.daily)
        with BytesIO() as buffer:
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()


def _draw_header(draw: ImageDraw.ImageDraw, report: UsageReport) -> None:
    title = (
        "Weekly Usage Report"
        if report.period is UsageReportPeriod.WEEKLY
        else "Monthly Usage Report"
    )
    draw.text((70, 42), "DownloadKade", font=load_chart_font(34), fill=_INTERACTIONS)
    draw.text((70, 88), title, font=load_chart_font(58), fill=_HEADING)
    start = report.start_at.astimezone(_TEHRAN).date().isoformat()
    end = report.end_at.astimezone(_TEHRAN).date().isoformat()
    draw.text(
        (70, 166),
        f"Range: {start} to {end}  |  Timezone: Asia/Tehran",
        font=load_chart_font(26),
        fill=_TEXT,
    )
    generated = report.end_at.isoformat(timespec="minutes")
    text = f"Generated at: {generated}"
    width = draw.textlength(text, font=load_chart_font(24))
    draw.text((IMAGE_WIDTH - 70 - width, 174), text, font=load_chart_font(24), fill=_MUTED)


def _draw_kpis(draw: ImageDraw.ImageDraw, report: UsageReport) -> None:
    success_rate = report.succeeded / report.downloads if report.downloads else 0.0
    cards = (
        ("Unique Users", _compact_number(report.unique_users)),
        ("Interactions", _compact_number(report.interactions)),
        ("Download Requests", _compact_number(report.downloads)),
        ("Successful Downloads", _compact_number(report.succeeded)),
        ("Success Rate", f"{success_rate * 100:.1f}%"),
        ("Delivered Volume", _format_bytes(report.delivered_bytes)),
    )
    left, top, gap = 70, 250, 18
    card_width = (IMAGE_WIDTH - 140 - gap * 5) // 6
    for index, (label, value) in enumerate(cards):
        x1 = left + index * (card_width + gap)
        x2 = x1 + card_width
        draw.rounded_rectangle((x1, top, x2, 430), radius=22, fill=_PANEL, outline=_BORDER, width=2)
        draw.text((x1 + 25, top + 25), label, font=load_chart_font(24), fill=_MUTED)
        value_font = load_chart_font(46 if len(value) <= 8 else 38)
        draw.text((x1 + 25, top + 82), value, font=value_font, fill=_HEADING)


def _draw_plot(draw: ImageDraw.ImageDraw, points: tuple[UsageDailyPoint, ...]) -> None:
    panel = (70, 475, IMAGE_WIDTH - 70, IMAGE_HEIGHT - 55)
    draw.rounded_rectangle(panel, radius=24, fill=_PANEL, outline=_BORDER, width=2)
    draw.text((105, 510), "Daily Activity", font=load_chart_font(34), fill=_HEADING)
    _draw_legend(draw, 510)

    left, top, right, bottom = 190, 655, IMAGE_WIDTH - 105, 1280
    maximum = max(
        [1, *(max(point.interactions, point.downloads, point.succeeded) for point in points)]
    )
    axis_max = _nice_axis_max(maximum)
    for step in range(6):
        value = axis_max * step // 5
        y = bottom - (bottom - top) * step / 5
        draw.line((left, y, right, y), fill=_GRID, width=2)
        label = _compact_number(value)
        width = draw.textlength(label, font=load_chart_font(22))
        draw.text((left - 20 - width, y), label, font=load_chart_font(22), fill=_TEXT, anchor="lm")
    draw.line((left, top, left, bottom), fill=_TEXT, width=3)
    draw.line((left, bottom, right, bottom), fill=_TEXT, width=3)
    draw.text((105, top - 58), "Total", font=load_chart_font(23), fill=_MUTED)
    draw.text(
        ((left + right) / 2, IMAGE_HEIGHT - 82),
        "Date",
        font=load_chart_font(23),
        fill=_MUTED,
        anchor="mm",
    )

    if not points:
        draw.text(
            ((left + right) / 2, (top + bottom) / 2),
            "No activity in this period",
            font=load_chart_font(34),
            fill=_MUTED,
            anchor="mm",
        )
        return

    group_width = (right - left) / len(points)
    bar_width = max(5, min(34, int((group_width - 10) / 3)))
    label_indices = _date_label_indices(len(points))
    maximum_by_series = {
        attribute: max(getattr(point, attribute) for point in points) for _, _, attribute in _SERIES
    }
    for index, point in enumerate(points):
        center = left + group_width * (index + 0.5)
        for series_index, (_, color, attribute) in enumerate(_SERIES):
            value = int(getattr(point, attribute))
            x1 = center + (series_index - 1) * bar_width - bar_width / 2
            x2 = x1 + bar_width - 2
            height = (bottom - top) * value / axis_max
            y1 = bottom - height
            if value:
                draw.rounded_rectangle((x1, y1, x2, bottom - 1), radius=3, fill=color)
            if value and (
                len(points) <= 7
                or value == maximum_by_series[attribute]
                or index == len(points) - 1
            ):
                draw.text(
                    ((x1 + x2) / 2, max(top + 8, y1 - 10)),
                    _compact_number(value),
                    font=load_chart_font(18),
                    fill=_HEADING,
                    anchor="ms",
                )
        if index in label_indices:
            draw.text(
                (center, bottom + 24),
                point.day.strftime("%m-%d"),
                font=load_chart_font(20),
                fill=_TEXT,
                anchor="ma",
            )


def _draw_legend(draw: ImageDraw.ImageDraw, y: int) -> None:
    x = 720
    for label, color, _ in _SERIES:
        draw.rounded_rectangle((x, y + 5, x + 38, y + 29), radius=5, fill=color)
        draw.text((x + 53, y), label, font=load_chart_font(24), fill=_TEXT)
        x += 53 + int(draw.textlength(label, font=load_chart_font(24))) + 70


def _date_label_indices(count: int) -> frozenset[int]:
    if count <= 7:
        return frozenset(range(count))
    interval = max(1, ceil((count - 1) / 6))
    return frozenset({0, count - 1, *range(0, count, interval)})


def _nice_axis_max(value: int) -> int:
    if value <= 5:
        return 5
    magnitude = 10 ** max(0, int(log10(value)) - 1)
    return int(ceil(value / (5 * magnitude)) * 5 * magnitude)


def _compact_number(value: int) -> str:
    absolute = abs(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if absolute >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return str(value)


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"
