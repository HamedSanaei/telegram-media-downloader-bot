from datetime import UTC, datetime, timedelta

import pytest

from telegram_media_bot.domain.models import (
    DownloadMode,
    JobId,
    MediaInfo,
    MediaKind,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.telegram.delivery import sanitize_caption_value, sanitize_filename
from telegram_media_bot.telegram.handlers import parse_selection_callback
from telegram_media_bot.telegram.ui import (
    cancellation_keyboard,
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
    assert keyboard.inline_keyboard[1][0].callback_data == "fmt:opaque-token-123:video_720"
    assert cancellation_keyboard(JobId("job")).inline_keyboard[0][0].callback_data == "cancel:job"
    assert "50٪" in render_progress(50, 512, 1024)
