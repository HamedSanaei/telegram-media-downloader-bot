from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_media_bot.domain.models import (
    DeliveryProgressEvent,
    DeliveryStage,
    DownloadMode,
    JobId,
    MediaFormatOption,
    MediaInfo,
    OutputContainer,
    RequiredChannel,
    SelectionRecord,
    SizeConfidence,
)

_MODE_LABELS = {
    DownloadMode.BEST: "بهترین ویدئو تا 1080p",
    DownloadMode.BEST_ORIGINAL: "بهترین کیفیت اصلی",
    DownloadMode.VIDEO_2160: "ویدئو 2160p (4K)",
    DownloadMode.VIDEO_1440: "ویدئو 1440p (2K)",
    DownloadMode.VIDEO_1080: "ویدئو 1080p",
    DownloadMode.VIDEO_720: "ویدئو 720p",
    DownloadMode.VIDEO_480: "ویدئو 480p",
    DownloadMode.AUDIO_BEST: "بهترین صدا",
    DownloadMode.AUDIO_MP3: "صدا MP3",
}


def selection_keyboard(
    selection: SelectionRecord,
    container: OutputContainer | None = None,
) -> InlineKeyboardMarkup:
    options = {
        option.mode: option
        for option in selection.media.format_options
        if option.container is container
    }
    rows = [
        [
            InlineKeyboardButton(
                text=_button_label(mode, options.get(mode)),
                callback_data=(
                    f"fmt:{selection.token}:{container.value}:{mode.value}"
                    if container is not None
                    else f"fmt:{selection.token}:{mode.value}"
                ),
            )
        ]
        for mode in selection.allowed_modes
        if mode in options or container is None
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def container_keyboard(selection: SelectionRecord) -> InlineKeyboardMarkup:
    labels = {
        OutputContainer.MP4: "ویدئو MP4",
        OutputContainer.WEBM: "ویدئو WebM",
        OutputContainer.MP3: "صوت MP3",
    }
    containers = tuple(
        container
        for container in OutputContainer
        if any(option.container is container for option in selection.media.format_options)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=labels[container],
                    callback_data=f"container:{selection.token}:{container.value}",
                )
            ]
            for container in containers
        ]
    )


def cancellation_keyboard(job_id: JobId) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="لغو دانلود", callback_data=f"cancel:{job_id}")]
        ]
    )


def required_channels_keyboard(
    channels: tuple[RequiredChannel, ...],
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"عضویت در {channel.title}", url=channel.join_url)]
        for channel in channels
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="عضو شدم، بررسی مجدد",
                callback_data="membership:recheck",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_media_info(
    info: MediaInfo,
    container: OutputContainer | None = None,
) -> str:
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
    matching_options = tuple(
        option
        for option in info.format_options
        if container is None or option.container is container
    )
    legacy_options = container is None and not any(
        option.container is not None for option in info.format_options
    )
    if (container is not None or legacy_options) and matching_options:
        lines.append("کیفیت‌های قابل دریافت:")
        lines.extend(
            f"• {_MODE_LABELS[option.mode]} — {_option_details(option)}"
            for option in matching_options
        )
    lines.append(
        "کیفیت موردنظر را انتخاب کنید:"
        if container is not None
        else "نوع فایل خروجی را انتخاب کنید:"
    )
    return "\n".join(lines)


def render_progress(
    percent: float | None,
    downloaded: int,
    total: int | None,
    *,
    status: str | None = None,
) -> str:
    if status == "transcoding":
        return "در حال تبدیل فرمت، فشرده‌سازی و آماده‌سازی ویدئو…"
    percent_text = "؟" if percent is None else f"{percent:.0f}"
    size_text = _size(downloaded)
    if total is not None:
        size_text = f"{size_text} از {_size(total)}"
    return f"در حال دریافت از منبع… {percent_text}٪\n{size_text}"


def render_delivery_progress(event: DeliveryProgressEvent) -> str:
    elapsed = _duration(round(event.elapsed_seconds))
    if event.stage is DeliveryStage.PACKAGING:
        return f"در حال پارت‌بندی فایل…\nزمان سپری‌شده: {elapsed}"  # noqa: RUF001
    item = f"بخش {event.item_ordinal} از {event.item_count}"
    if event.stage is DeliveryStage.FINALIZING:
        return (
            f"آپلود {item} کامل شد.\n"
            f"Telegram در حال پردازش نهایی فایل است…\nزمان سپری‌شده: {elapsed}"  # noqa: RUF001
        )
    item_percent = "؟" if event.item_percent is None else f"{event.item_percent:.0f}"
    overall_percent = "؟" if event.percent is None else f"{event.percent:.0f}"
    item_size = _size(event.item_transferred_bytes)
    if event.item_size_bytes is not None:
        item_size = f"{item_size} از {_size(event.item_size_bytes)}"
    return f"در حال آپلود {item}… {item_percent}٪\n{item_size}\nپیشرفت کل: {overall_percent}٪"


def _button_label(mode: DownloadMode, option: MediaFormatOption | None) -> str:
    base = _MODE_LABELS[mode]
    if option is None:
        return base
    return f"{base} · {_size_label(option)}"


def _option_details(option: MediaFormatOption) -> str:
    details: list[str] = []
    if option.height is not None:
        resolution = (
            f"{option.width}x{option.height}" if option.width is not None else f"{option.height}p"
        )
        details.append(resolution)
    if option.fps is not None:
        details.append(f"{option.fps:g}fps")
    if option.height is not None:
        details.append("HDR" if option.is_hdr else "SDR")
    if option.requires_transcode:
        details.append("نیازمند تبدیل")
    elif option.container is not None:
        details.append("نسخهٔ اصلی")
    details.append(_size_label(option))
    return "، ".join(details)


def _size_label(option: MediaFormatOption) -> str:
    if option.size_bytes is None or option.size_confidence is SizeConfidence.UNKNOWN:
        return "حجم نامشخص"
    value = _size(option.size_bytes)
    if option.size_confidence is SizeConfidence.ESTIMATED:
        return f"حدود {value}"
    return f"{value} دقیق"


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
