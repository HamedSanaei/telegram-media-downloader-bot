from datetime import UTC, datetime, timedelta

import pytest

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
from telegram_media_bot.telegram.handlers import parse_selection_callback
from telegram_media_bot.telegram.ui import (
    cancellation_keyboard,
    container_keyboard,
    render_delivery_progress,
    render_media_info,
    render_progress,
    selection_keyboard,
)


def test_callback_parser_accepts_only_semantic_modes() -> None:
    token, mode = parse_selection_callback("fmt:opaque-token-123:video_720")
    assert token == "opaque-token-123"
    assert mode is DownloadMode.VIDEO_720
    with pytest.raises(ValueError):
        parse_selection_callback("fmt:opaque-token-123:137")
    with pytest.raises(ValueError):
        parse_selection_callback("fmt:short:best")


def test_container_selection_is_tokenized_and_only_offers_real_combinations() -> None:
    now = datetime.now(UTC)
    info = MediaInfo(
        media_id="id",
        title="Title",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.com/media",
        format_options=(
            MediaFormatOption(
                mode=DownloadMode.VIDEO_1080,
                container=OutputContainer.MP4,
                container_policy=ContainerPolicy.GUARANTEED,
                height=1080,
            ),
            MediaFormatOption(
                mode=DownloadMode.VIDEO_720,
                container=OutputContainer.WEBM,
                container_policy=ContainerPolicy.GUARANTEED,
                height=720,
            ),
        ),
    )
    selection = SelectionRecord(
        token=SelectionToken("opaque-token-123"),
        owner_user_id=1,
        chat_id=1,
        media=info,
        allowed_modes=(DownloadMode.VIDEO_1080, DownloadMode.VIDEO_720),
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )

    containers = container_keyboard(selection)
    mp4 = selection_keyboard(selection, OutputContainer.MP4)

    assert containers.inline_keyboard[0][0].callback_data == ("container:opaque-token-123:mp4")
    assert len(mp4.inline_keyboard) == 1
    assert mp4.inline_keyboard[0][0].callback_data == ("fmt:opaque-token-123:mp4:video_1080")


def test_filename_and_caption_are_sanitized() -> None:
    assert sanitize_filename("../bad/name\x00", suffix=".MP4", max_length=32) == "name.mp4"
    assert sanitize_filename("title", suffix=".tar.gz", max_length=32) == "title"
    assert sanitize_caption_value("hello\n\x00world", 20) == "hello world"


def test_media_and_progress_ui_use_owned_models_only() -> None:
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
            MediaFormatOption(
                mode=DownloadMode.BEST,
                width=1920,
                height=1080,
                fps=60,
                size_bytes=100 * 1024 * 1024,
                size_confidence=SizeConfidence.EXACT,
            ),
            MediaFormatOption(
                mode=DownloadMode.VIDEO_720,
                height=720,
                size_confidence=SizeConfidence.UNKNOWN,
            ),
        ),
    )
    now = datetime.now(UTC)
    selection = SelectionRecord(
        token=SelectionToken("opaque-token-123"),
        owner_user_id=1,
        chat_id=1,
        media=info,
        allowed_modes=(DownloadMode.BEST, DownloadMode.VIDEO_720),
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    text = render_media_info(info)
    keyboard = selection_keyboard(selection)
    assert "01:01" in text
    assert "2.0 KiB" in text
    assert keyboard.inline_keyboard[1][0].text == "ویدئو 720p · حجم نامشخص"
    assert keyboard.inline_keyboard[1][0].callback_data == "fmt:opaque-token-123:video_720"
    assert cancellation_keyboard(JobId("job")).inline_keyboard[0][0].callback_data == "cancel:job"
    assert "50٪" in render_progress(50, 512, 1024)
    assert "فشرده‌سازی" in render_progress(None, 0, None, status="transcoding")
    assert "100.0 MiB" in keyboard.inline_keyboard[0][0].text
    assert "1080" in text
    assert "100.0 MiB" in text
    assert "حجم نامشخص" in text


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
