from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_media_bot.domain.models import DownloadMode, JobId, MediaInfo, SelectionRecord

_MODE_LABELS = {
    DownloadMode.BEST: "بهترین ویدئو تا 1080p",
    DownloadMode.VIDEO_1080: "ویدئو تا 1080p",
    DownloadMode.VIDEO_720: "ویدئو تا 720p",
    DownloadMode.VIDEO_480: "ویدئو تا 480p",
    DownloadMode.AUDIO_BEST: "بهترین صدا",
    DownloadMode.AUDIO_MP3: "صدا MP3",
}


def selection_keyboard(selection: SelectionRecord) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_MODE_LABELS[mode],
                callback_data=f"fmt:{selection.token}:{mode.value}",
            )
        ]
        for mode in selection.allowed_modes
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancellation_keyboard(job_id: JobId) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="لغو دانلود", callback_data=f"cancel:{job_id}")]
        ]
    )


def render_media_info(info: MediaInfo) -> str:
    lines = [
        f"عنوان: {_clean(info.title, 256)}",
        f"منبع: {_clean(info.source, 64)}",
        f"نوع: {info.kind.value}",
    ]
    if info.duration_seconds is not None:
        lines.append(f"مدت: {_duration(info.duration_seconds)}")
    if info.estimated_size_bytes is not None:
        lines.append(f"حجم تقریبی: {_size(info.estimated_size_bytes)}")
    if info.item_count is not None:
        lines.append(f"تعداد آیتم: {info.item_count}")
    lines.append("خروجی موردنظر را انتخاب کنید:")
    return "\n".join(lines)


def render_progress(
    percent: float | None,
    downloaded: int,
    total: int | None,
    *,
    status: str | None = None,
) -> str:
    if status == "transcoding":
        return "در حال فشرده‌سازی ویدئو در کیفیت انتخابی…"
    percent_text = "؟" if percent is None else f"{percent:.0f}"
    size_text = _size(downloaded)
    if total is not None:
        size_text = f"{size_text} از {_size(total)}"
    return f"در حال دریافت… {percent_text}٪\n{size_text}"


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _clean(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]
