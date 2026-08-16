from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_media_bot.application.services.native_options import (
    NativeOptionCatalog,
    build_native_option_catalog,
    display_video_codec,
)
from telegram_media_bot.domain.models import (
    DeliveryProgressEvent,
    DeliveryStage,
    ImageDeliveryMode,
    JobId,
    MediaInfo,
    MediaKind,
    OutputContainer,
    RequiredChannel,
    SelectionRecord,
)

BACK_TEXT = "⬅️ بازگشت"


def selection_keyboard(
    selection: SelectionRecord,
    container: OutputContainer,
    catalog: NativeOptionCatalog | None = None,
) -> InlineKeyboardMarkup:
    resolved = catalog or build_native_option_catalog(selection.media)
    rows = [
        [
            InlineKeyboardButton(
                text=option.display_label,
                callback_data=f"o2:{selection.token}:{option.option_id}",
            )
        ]
        for option in resolved.for_container(container)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=BACK_TEXT,
                callback_data=f"n2:{selection.token}:t",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def container_keyboard(
    selection: SelectionRecord,
    catalog: NativeOptionCatalog | None = None,
) -> InlineKeyboardMarkup:
    resolved = catalog or build_native_option_catalog(selection.media)
    labels = {
        OutputContainer.MP4: "🎬 MP4 Native · AV1 / H.264",
        OutputContainer.WEBM: "🎞 WebM Native · VP9 + Opus",
        OutputContainer.MP3: "🎵 صوت MP3",
    }
    choices = tuple(
        container
        for container in (OutputContainer.MP4, OutputContainer.WEBM, OutputContainer.MP3)
        if resolved.for_container(container)
    )
    rows = [
        [
            InlineKeyboardButton(
                text=labels[choice],
                callback_data=f"c2:{selection.token}:{choice.value}",
            )
        ]
        for choice in choices
    ]
    artwork_labels = {
        "youtube_thumbnail": "🖼 دانلود تصویر بندانگشتی",
        "soundcloud_artwork": "🖼 دانلود تصویر کاور",
    }
    rows.extend(
        [
            InlineKeyboardButton(
                text=artwork_labels[mode.value],
                callback_data=f"m2:{selection.token}:{mode.value}",
            )
        ]
        for mode in selection.allowed_modes
        if mode.value in artwork_labels
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=BACK_TEXT,
                callback_data=f"n2:{selection.token}:s",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancellation_keyboard(job_id: JobId) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="لغو دانلود", callback_data=f"cancel:{job_id}")]
        ]
    )


def media_bundle_keyboard(selection: SelectionRecord) -> InlineKeyboardMarkup:
    labels = {
        "image_original": "🖼 دانلود تصویر اصلی",
        "images_original": "🖼 دانلود همه تصاویر",
        "all_original_media": "📦 دانلود همه رسانه‌ها",  # noqa: RUF001
        "images_only": "🖼 فقط تصاویر",
        "videos_only": "🎬 فقط ویدیوها",
        "video_original": "🎬 دانلود ویدیوی اصلی",
        "images_zip": "🗜 تصاویر به‌صورت ZIP",
        "youtube_thumbnail": "🖼 دانلود تصویر بندانگشتی",
        "soundcloud_artwork": "🖼 دانلود تصویر کاور",
    }
    rows = [
        [
            InlineKeyboardButton(
                text=labels[mode.value],
                callback_data=f"m2:{selection.token}:{mode.value}",
            )
        ]
        for mode in selection.allowed_modes
        if mode.value in labels
    ]
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data=f"n2:{selection.token}:s")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def instagram_image_delivery_keyboard(selection: SelectionRecord) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🖼 ارسال به‌صورت عکس",
                    callback_data=(f"i2:{selection.token}:{ImageDeliveryMode.PHOTO.value}"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📎 ارسال به‌صورت فایل",
                    callback_data=(f"i2:{selection.token}:{ImageDeliveryMode.DOCUMENT.value}"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو",
                    callback_data=f"n2:{selection.token}:s",
                )
            ],
        ]
    )


def render_instagram_image_delivery_prompt(info: MediaInfo) -> str:
    image_count = sum(asset.kind is MediaKind.IMAGE for asset in info.assets)
    video_count = sum(asset.kind is MediaKind.VIDEO for asset in info.assets)
    counts = f"این پست شامل {image_count} تصویر"
    if video_count:
        counts += f" و {video_count} ویدیو"
    return "\n".join(
        (
            render_media_info(info),
            "",
            f"{counts} است.",
            "نحوه ارسال تصاویر را انتخاب کنید:",
            "",
            "🖼 عکس — نمایش مستقیم در تلگرام؛ ممکن است تلگرام تصویر را فشرده کند.",
            "📎 فایل — فایل اصلی بدون فشرده‌سازی تلگرام.",
        )
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
    catalog: NativeOptionCatalog | None = None,
) -> str:
    lines = [
        f"عنوان: {_clean(info.title, 256)}",
        f"منبع: {_clean(info.source, 64)}",
        f"نوع: {info.kind.value}",
    ]
    if info.duration_seconds is not None:
        lines.append(f"مدت: {_duration(info.duration_seconds)}")
    if info.item_count is not None:
        lines.append(f"تعداد آیتم: {info.item_count}")
    resolved = catalog or build_native_option_catalog(info)
    best_original = resolved.best_original()
    if best_original is not None:
        summary = [
            (
                f"{best_original.actual_height}p"
                if best_original.actual_height is not None
                else f"{best_original.actual_width}px"
                if best_original.actual_width is not None
                else "کیفیت نامشخص"
            ),
            (
                "MP4"
                if best_original.container is OutputContainer.MP4
                else "WebM"
                if best_original.container is OutputContainer.WEBM
                else best_original.container.value.upper()
            ),
            display_video_codec(best_original.video_codec),
        ]
        if best_original.size_bytes is None:
            summary.append("حجم نامشخص")
        else:
            size = _size(best_original.size_bytes)
            summary.append(f"حدود {size}" if best_original.size_is_approximate else size)
        lines.extend(("بهترین نسخهٔ اصلی:", " · ".join(summary)))
    if container is not None:
        options = resolved.for_container(container)
        title = {
            OutputContainer.MP4: "کیفیت‌های واقعی MP4 Native:",
            OutputContainer.WEBM: "کیفیت‌های واقعی WebM Native:",
            OutputContainer.MP3: "کیفیت صوت را انتخاب کنید:",
        }[container]
        lines.append(title)
        lines.extend(f"• {option.display_label}" for option in options)
    else:
        lines.extend(
            (
                "نوع خروجی را انتخاب کنید:",
                "خروجی‌های ویدیویی Native هستند و بدون بازکدگذاری ارسال می‌شوند.",
            )
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
