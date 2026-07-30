from __future__ import annotations

import struct
import zlib

from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.domain.analytics import UsageReport

_WIDTH = 1000
_HEIGHT = 560
_BACKGROUND = (248, 250, 252)
_GRID = (218, 224, 230)
_INTERACTIONS = (54, 116, 217)
_DOWNLOADS = (29, 158, 117)
_SUCCESS = (132, 94, 194)


class PngUsageChartRenderer(UsageChartRenderer):
    """Render a dependency-free daily bar chart suitable for Telegram."""

    def render(self, report: UsageReport) -> bytes:
        pixels = bytearray(_BACKGROUND * (_WIDTH * _HEIGHT))
        left, top, right, bottom = 70, 60, _WIDTH - 35, _HEIGHT - 70
        for step in range(6):
            y = top + (bottom - top) * step // 5
            _line(pixels, left, y, right, y, _GRID)
        _line(pixels, left, top, left, bottom, (90, 100, 110))
        _line(pixels, left, bottom, right, bottom, (90, 100, 110))
        points = report.daily
        if points:
            maximum = max(
                1,
                *(max(point.interactions, point.downloads, point.succeeded) for point in points),
            )
            group_width = max(1, (right - left) // len(points))
            bar_width = max(2, min(14, (group_width - 4) // 3))
            for index, point in enumerate(points):
                center = left + index * group_width + group_width // 2
                for offset, value, color in (
                    (-bar_width, point.interactions, _INTERACTIONS),
                    (0, point.downloads, _DOWNLOADS),
                    (bar_width, point.succeeded, _SUCCESS),
                ):
                    height = (bottom - top - 5) * value // maximum
                    _rectangle(
                        pixels,
                        center + offset - bar_width // 2,
                        bottom - height,
                        center + offset + bar_width // 2,
                        bottom - 1,
                        color,
                    )
        _rectangle(pixels, 70, 22, 105, 38, _INTERACTIONS)
        _rectangle(pixels, 270, 22, 305, 38, _DOWNLOADS)
        _rectangle(pixels, 470, 22, 505, 38, _SUCCESS)
        return _png(bytes(pixels), _WIDTH, _HEIGHT)


def _rectangle(
    pixels: bytearray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: tuple[int, int, int],
) -> None:
    for y in range(max(0, y1), min(_HEIGHT, y2 + 1)):
        for x in range(max(0, x1), min(_WIDTH, x2 + 1)):
            offset = (y * _WIDTH + x) * 3
            pixels[offset : offset + 3] = bytes(color)


def _line(
    pixels: bytearray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: tuple[int, int, int],
) -> None:
    if y1 == y2:
        _rectangle(pixels, x1, y1, x2, y1, color)
    elif x1 == x2:
        _rectangle(pixels, x1, y1, x1, y2, color)


def _png(rgb: bytes, width: int, height: int) -> bytes:
    rows = b"".join(b"\x00" + rgb[row * width * 3 : (row + 1) * width * 3] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(rows, level=9))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )
