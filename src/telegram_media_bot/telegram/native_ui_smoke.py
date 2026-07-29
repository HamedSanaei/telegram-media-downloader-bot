from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
    SizeConfidence,
)
from telegram_media_bot.telegram.handlers import (
    parse_native_container_callback,
    parse_native_option_callback,
    parse_navigation_callback,
)
from telegram_media_bot.telegram.ui import container_keyboard, selection_keyboard


def main() -> None:
    selection = _fixture()
    catalog = build_native_option_catalog(selection.media)
    mp4 = catalog.for_container(OutputContainer.MP4)
    webm = catalog.for_container(OutputContainer.WEBM)
    assert [option.actual_height for option in mp4] == [1080]
    assert [option.actual_height for option in webm] == [2160]
    assert all(not option.transcode_required for option in (*mp4, *webm))

    types = container_keyboard(selection, catalog)
    labels = [row[0].text for row in types.inline_keyboard]
    assert labels[:3] == [
        "🎬 MP4 Native · H.264 + AAC",
        "🎞 WebM Native · VP9 + Opus",
        "🎵 صوت MP3",
    ]
    assert "MP4" not in labels
    assert "WEBM" not in labels
    callbacks = [
        button.callback_data
        for keyboard in (
            types,
            selection_keyboard(selection, OutputContainer.MP4, catalog),
            selection_keyboard(selection, OutputContainer.WEBM, catalog),
            selection_keyboard(selection, OutputContainer.MP3, catalog),
        )
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    for callback in callbacks:
        if callback.startswith("c2:"):
            parse_native_container_callback(callback)
        elif callback.startswith("o2:"):
            parse_native_option_callback(callback)
        elif callback.startswith("n2:"):
            parse_navigation_callback(callback)
    print(
        json.dumps(
            {
                "mp4_heights": [option.actual_height for option in mp4],
                "webm_heights": [option.actual_height for option in webm],
                "hidden_transcode_options": catalog.hidden_transcode_option_count,
                "generic_video_buttons": False,
                "all_video_options_native": True,
                "callback_count": len(callbacks),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _fixture() -> SelectionRecord:
    now = datetime.now(UTC)
    info = MediaInfo(
        media_id="fixture",
        title="fixture",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.test/video",
        format_options=(
            MediaFormatOption(
                mode=DownloadMode.VIDEO_2160,
                container=OutputContainer.MP4,
                container_policy=ContainerPolicy.GUARANTEED,
                width=1920,
                height=1080,
                fps=30,
                size_bytes=80_000_000,
                size_confidence=SizeConfidence.EXACT,
                selected_format_ids=("137", "140"),
                video_codec="avc1.640028",
                audio_codec="mp4a.40.2",
                dynamic_range="SDR",
                fallback_reason="exact_h264_not_available",
            ),
            MediaFormatOption(
                mode=DownloadMode.VIDEO_1080,
                container=OutputContainer.MP4,
                container_policy=ContainerPolicy.GUARANTEED,
                width=1920,
                height=1080,
                fps=30,
                size_bytes=80_000_000,
                size_confidence=SizeConfidence.EXACT,
                selected_format_ids=("137", "140"),
                video_codec="avc1.640028",
                audio_codec="mp4a.40.2",
                dynamic_range="SDR",
            ),
            MediaFormatOption(
                mode=DownloadMode.VIDEO_2160,
                container=OutputContainer.MP4,
                container_policy=ContainerPolicy.EXPLICIT_TRANSCODE,
                requires_transcode=True,
                width=3840,
                height=2160,
                selected_format_ids=("399", "140"),
                video_codec="av01.0.12M.08",
                audio_codec="mp4a.40.2",
            ),
            MediaFormatOption(
                mode=DownloadMode.VIDEO_2160,
                container=OutputContainer.WEBM,
                container_policy=ContainerPolicy.GUARANTEED,
                width=3840,
                height=2160,
                fps=30,
                size_bytes=500_000_000,
                size_confidence=SizeConfidence.EXACT,
                selected_format_ids=("313", "251"),
                video_codec="vp9",
                audio_codec="opus",
                dynamic_range="SDR",
            ),
            MediaFormatOption(
                mode=DownloadMode.AUDIO_MP3,
                container=OutputContainer.MP3,
                container_policy=ContainerPolicy.GUARANTEED,
                size_bytes=5_000_000,
                size_confidence=SizeConfidence.EXACT,
                selected_format_ids=("251",),
                audio_codec="mp3",
            ),
        ),
    )
    return SelectionRecord(
        token=SelectionToken("runtime-token-123"),
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


if __name__ == "__main__":
    main()
