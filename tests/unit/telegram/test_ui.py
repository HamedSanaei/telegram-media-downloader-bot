from datetime import UTC, datetime, timedelta

import pytest

from telegram_media_bot.application.services.native_options import (
    build_native_option_catalog,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DeliveryProgressEvent,
    DeliveryStage,
    DownloadMode,
    JobId,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
    SizeConfidence,
)
from telegram_media_bot.telegram.delivery import sanitize_caption_value, sanitize_filename
from telegram_media_bot.telegram.handlers import (
    parse_native_container_callback,
    parse_native_option_callback,
    parse_navigation_callback,
    parse_selection_callback,
)
from telegram_media_bot.telegram.ui import (
    BACK_TEXT,
    cancellation_keyboard,
    container_keyboard,
    render_delivery_progress,
    render_media_info,
    render_progress,
    selection_keyboard,
)


def test_callback_parsers_are_versioned_semantic_and_bounded() -> None:
    token, mode = parse_selection_callback("fmt:opaque-token-123:video_720")
    assert token == "opaque-token-123"
    assert mode is DownloadMode.VIDEO_720
    assert parse_native_container_callback("c2:opaque-token-123:mp4") == (
        "opaque-token-123",
        OutputContainer.MP4,
    )
    assert parse_native_option_callback("o2:opaque-token-123:0123456789abcdef") == (
        "opaque-token-123",
        "0123456789abcdef",
    )
    assert parse_navigation_callback("n2:opaque-token-123:t") == (
        "opaque-token-123",
        "t",
    )
    for invalid in (
        "fmt:opaque-token-123:137",
        "o2:opaque-token-123:399",
        "n2:short:t",
        f"o2:opaque-token-123:{'a' * 70}",
    ):
        with pytest.raises(ValueError):
            if invalid.startswith("fmt:"):
                parse_selection_callback(invalid)
            elif invalid.startswith("n2:"):
                parse_navigation_callback(invalid)
            else:
                parse_native_option_callback(invalid)


def test_file_type_page_only_exposes_native_video_audio_and_back() -> None:
    selection = _selection()
    keyboard = container_keyboard(selection)
    labels = [row[0].text for row in keyboard.inline_keyboard]
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

    assert labels == [
        "🎬 MP4 Native · AV1 / H.264",
        "🎞 WebM Native · VP9 + Opus",
        "🎵 صوت MP3",
        BACK_TEXT,
    ]
    assert all(label not in {"MP4", "WEBM"} for label in labels)
    assert callbacks[-1] == "n2:opaque-token-123:s"
    assert all(
        callback is not None and len(callback.encode("utf-8")) <= 64 for callback in callbacks
    )


@pytest.mark.parametrize(
    "container", [OutputContainer.MP4, OutputContainer.WEBM, OutputContainer.MP3]
)
def test_every_quality_page_has_deterministic_back(container: OutputContainer) -> None:
    selection = _selection()
    keyboard = selection_keyboard(selection, container)

    assert keyboard.inline_keyboard[-1][0].text == BACK_TEXT
    assert keyboard.inline_keyboard[-1][0].callback_data == "n2:opaque-token-123:t"


def test_quality_page_uses_actual_plan_fields_and_opaque_option_id() -> None:
    selection = _selection()
    catalog = build_native_option_catalog(selection.media)
    keyboard = selection_keyboard(selection, OutputContainer.MP4, catalog)
    text = render_media_info(selection.media, OutputContainer.MP4, catalog)

    assert keyboard.inline_keyboard[0][0].text == "1080p · 30fps · H.264 · 82.5 MiB"
    assert "• 2160p" not in text
    assert "1080p · 30fps · H.264 · 82.5 MiB" in text
    callback = keyboard.inline_keyboard[0][0].callback_data
    assert callback is not None
    assert callback.startswith("o2:opaque-token-123:")
    assert "137" not in callback and "140" not in callback
    assert len(callback.encode("utf-8")) <= 64


def test_media_and_progress_ui_use_owned_models_only() -> None:
    info = _selection().media
    text = render_media_info(info)
    assert "01:01" in text
    assert "بهترین نسخهٔ اصلی:" in text
    assert "2160p · WebM · VP9 · 500.0 MiB" in text
    assert "نوع خروجی را انتخاب کنید:" in text
    assert cancellation_keyboard(JobId("job")).inline_keyboard[0][0].callback_data == "cancel:job"
    assert "50٪" in render_progress(50, 512, 1024)
    assert "فشرده‌سازی" in render_progress(None, 0, None, status="transcoding")


def test_filename_and_caption_are_sanitized() -> None:
    assert sanitize_filename("../bad/name\x00", suffix=".MP4", max_length=32) == "name.mp4"
    assert sanitize_filename("title", suffix=".tar.gz", max_length=32) == "title"
    assert sanitize_caption_value("hello\n\x00world", 20) == "hello world"


def test_delivery_progress_distinguishes_transfer_from_telegram_processing() -> None:
    uploading = DeliveryProgressEvent(
        job_id=JobId("job"),
        stage=DeliveryStage.UPLOADING,
        transferred_bytes=50,
        total_bytes=100,
        item_transferred_bytes=25,
        item_size_bytes=50,
        item_ordinal=2,
        item_count=3,
        elapsed_seconds=5,
    )
    finalizing = DeliveryProgressEvent(
        job_id=JobId("job"),
        stage=DeliveryStage.FINALIZING,
        transferred_bytes=100,
        total_bytes=100,
        item_transferred_bytes=50,
        item_size_bytes=50,
        item_ordinal=2,
        item_count=3,
        elapsed_seconds=30,
    )

    assert "50٪" in render_delivery_progress(uploading)
    assert "پیشرفت کل: 50٪" in render_delivery_progress(uploading)
    assert "در حال پردازش" in render_delivery_progress(finalizing)
    assert "100٪" not in render_delivery_progress(finalizing)


def _selection() -> SelectionRecord:
    now = datetime.now(UTC)
    mib = 1024 * 1024
    info = MediaInfo(
        media_id="id",
        title="Title",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.com/media",
        duration_seconds=61,
        item_count=2,
        estimated_size_bytes=2048,
        format_options=(
            _video_option(
                DownloadMode.VIDEO_1080,
                OutputContainer.MP4,
                ("137", "140"),
                1080,
                "avc1.640028",
                "mp4a.40.2",
                int(82.5 * mib),
            ),
            _video_option(
                DownloadMode.VIDEO_2160,
                OutputContainer.MP4,
                ("399", "140"),
                2160,
                "av01.0.12M.08",
                "mp4a.40.2",
                300 * mib,
                requires_transcode=True,
            ),
            _video_option(
                DownloadMode.VIDEO_2160,
                OutputContainer.WEBM,
                ("315", "251"),
                2160,
                "vp09.00.50.08",
                "opus",
                500 * mib,
            ),
            MediaFormatOption(
                mode=DownloadMode.AUDIO_MP3,
                container=OutputContainer.MP3,
                container_policy=ContainerPolicy.GUARANTEED,
                audio_codec="mp3",
                size_bytes=2 * mib,
                size_confidence=SizeConfidence.ESTIMATED,
                selected_format_ids=("251",),
            ),
        ),
    )
    return SelectionRecord(
        token=SelectionToken("opaque-token-123"),
        owner_user_id=1,
        chat_id=1,
        media=info,
        allowed_modes=(
            DownloadMode.VIDEO_2160,
            DownloadMode.VIDEO_1080,
            DownloadMode.AUDIO_MP3,
        ),
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )


def _video_option(
    mode: DownloadMode,
    container: OutputContainer,
    selected_format_ids: tuple[str, ...],
    height: int,
    video_codec: str,
    audio_codec: str,
    size_bytes: int,
    *,
    requires_transcode: bool = False,
) -> MediaFormatOption:
    return MediaFormatOption(
        mode=mode,
        container=container,
        container_policy=(
            ContainerPolicy.EXPLICIT_TRANSCODE if requires_transcode else ContainerPolicy.GUARANTEED
        ),
        requires_transcode=requires_transcode,
        width=round(height * 16 / 9),
        height=height,
        fps=30,
        dynamic_range="SDR",
        size_bytes=size_bytes,
        size_confidence=SizeConfidence.EXACT,
        selected_format_ids=selected_format_ids,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )
