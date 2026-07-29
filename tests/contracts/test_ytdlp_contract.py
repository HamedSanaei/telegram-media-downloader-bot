import os
from datetime import UTC, datetime, timedelta

import pytest

from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.telegram.ui import container_keyboard

_SOURCE_FIXTURES = (
    ("youtube", "CONTRACT_YOUTUBE_URL"),
    ("soundcloud", "CONTRACT_SOUNDCLOUD_URL"),
    ("instagram", "CONTRACT_INSTAGRAM_URL"),
    ("instagram", "CONTRACT_INSTAGRAM_STORY_URL"),
    ("instagram", "CONTRACT_INSTAGRAM_HIGHLIGHT_URL"),
    ("twitter", "CONTRACT_TWITTER_URL"),
    ("pinterest", "CONTRACT_PINTEREST_URL"),
    ("tiktok", "CONTRACT_TIKTOK_URL"),
)


@pytest.mark.contract
@pytest.mark.parametrize(("expected_source", "environment_key"), _SOURCE_FIXTURES)
def test_operator_supplied_public_url_inspection(
    settings: Settings, expected_source: str, environment_key: str
) -> None:
    if os.environ.get("RUN_CONTRACT_TESTS") != "1":
        pytest.skip("Set RUN_CONTRACT_TESTS=1 to enable external contract tests")
    url = os.environ.get(environment_key)
    if not url:
        pytest.skip(f"Set {environment_key} to an operator-maintained safe public fixture")

    info = YtDlpEngine(settings).inspect(url)
    assert info.media_id
    assert info.title
    assert info.source == expected_source


@pytest.mark.contract
def test_youtube_production_regression_selects_native_h264_plan(settings: Settings) -> None:
    if os.environ.get("RUN_CONTRACT_TESTS") != "1":
        pytest.skip("Set RUN_CONTRACT_TESTS=1 to enable external contract tests")

    info = YtDlpEngine(settings).inspect("https://www.youtube.com/watch?v=kyLrKXBETNg")
    option = next(
        (
            item
            for item in info.format_options
            if item.mode is DownloadMode.VIDEO_1080
            and item.container is OutputContainer.MP4
            and item.container_policy is ContainerPolicy.GUARANTEED
        ),
        None,
    )

    assert option is not None
    assert option.height == 1080
    assert option.requires_transcode is False
    assert option.selection_reason == "native_h264_exact_resolution"
    webm = next(
        (
            item
            for item in info.format_options
            if item.mode is DownloadMode.VIDEO_1080
            and item.container is OutputContainer.WEBM
            and item.container_policy is ContainerPolicy.GUARANTEED
        ),
        None,
    )
    assert webm is not None
    assert webm.height == 1080
    assert webm.requires_transcode is False
    assert webm.selection_reason == "webm_native"


@pytest.mark.contract
def test_youtube_native_ui_catalog_is_truthful_and_unique(settings: Settings) -> None:
    if os.environ.get("RUN_CONTRACT_TESTS") != "1":
        pytest.skip("Set RUN_CONTRACT_TESTS=1 to enable external contract tests")

    info = YtDlpEngine(settings).inspect("https://www.youtube.com/watch?v=7bOptq-NPJQ")
    catalog = build_native_option_catalog(info)
    mp4 = catalog.for_container(OutputContainer.MP4)
    webm = catalog.for_container(OutputContainer.WEBM)

    assert mp4
    assert webm
    assert len({option.option_id for option in mp4}) == len(mp4)
    assert len({option.option_id for option in webm}) == len(webm)
    assert all(
        option.video_codec
        and (
            option.video_codec.casefold() == "h264"
            or option.video_codec.casefold().startswith("avc1")
        )
        and option.audio_codec
        and (
            option.audio_codec.casefold() == "aac"
            or option.audio_codec.casefold().startswith("mp4a")
        )
        for option in mp4
    )
    assert all(
        option.video_codec
        and (
            option.video_codec.casefold() == "vp9"
            or option.video_codec.casefold().startswith("vp09")
        )
        and option.audio_codec
        and option.audio_codec.casefold() == "opus"
        for option in webm
    )
    assert all(not option.transcode_required for option in (*mp4, *webm))
    assert all(
        option.actual_height is None or f"{option.actual_height}p" in option.display_label
        for option in (*mp4, *webm)
    )
    for source_option in info.format_options:
        if (
            source_option.video_size_bytes is not None
            and source_option.audio_size_bytes is not None
        ):
            assert source_option.size_bytes == (
                source_option.video_size_bytes + source_option.audio_size_bytes
            )

    now = datetime.now(UTC)
    selection = SelectionRecord(
        token=SelectionToken("contract-token"),
        owner_user_id=1,
        chat_id=1,
        media=info,
        allowed_modes=tuple(dict.fromkeys(option.mode for option in catalog.options)),
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    labels = [row[0].text for row in container_keyboard(selection, catalog).inline_keyboard]
    assert "MP4" not in labels
    assert "WEBM" not in labels
    assert any("MP4 Native" in label for label in labels)
    assert any("WebM Native" in label for label in labels)
