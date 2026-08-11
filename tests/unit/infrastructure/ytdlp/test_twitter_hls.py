from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from yt_dlp import YoutubeDL

from telegram_media_bot.application.services.native_options import build_native_option_catalog
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    JobId,
    MediaProcessingKind,
    OutputContainer,
)
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine
from telegram_media_bot.infrastructure.ytdlp.mapper import map_media_info
from telegram_media_bot.infrastructure.ytdlp.options import YtDlpOptionsFactory

_FIXTURE = Path(__file__).parents[3] / "fixtures" / "twitter-hls-info.json"


def test_twitter_hls_plans_h264_aac_mp4_remux(settings: Settings) -> None:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    engine = YtDlpEngine(settings)

    with YoutubeDL({"quiet": True}) as ydl:
        options = engine._inspect_format_options(ydl, raw)

    selected = next(
        option
        for option in options
        if option.container is OutputContainer.MP4
        and option.selected_format_ids == ("hls-1672", "hls-audio-128000-Audio")
    )
    assert selected.video_codec == "avc1.640028"
    assert selected.audio_codec == "aac"
    assert selected.requires_transcode is False
    assert selected.processing_kind is MediaProcessingKind.REMUX


def test_selected_twitter_formats_are_used_verbatim_for_download(
    settings: Settings, tmp_path: Path
) -> None:
    selected = ("hls-1672", "hls-audio-128000-Audio")
    request = DownloadRequest(
        job_id=JobId("twitter-format-contract"),
        url="https://x.com/example/status/1951000000000000000",
        mode=DownloadMode.BEST,
        output_directory=tmp_path,
        container=OutputContainer.MP4,
        container_policy=ContainerPolicy.GUARANTEED,
        selected_format_ids=selected,
    )

    assert YtDlpOptionsFactory(settings).format_for_request(request) == "+".join(selected)


def test_unknown_mp4_hls_audio_is_not_assumed_to_be_aac(settings: Settings) -> None:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    raw["formats"][0] = replace_format_id(raw["formats"][0], "mystery-audio")
    engine = YtDlpEngine(settings)

    with YoutubeDL({"quiet": True}) as ydl:
        options = engine._inspect_format_options(ydl, raw)

    info = replace(
        map_media_info(raw, original_url=raw["webpage_url"]),
        format_options=options,
    )
    assert not any(
        option.container is OutputContainer.MP4 and "mystery-audio" in option.selected_format_ids
        for option in build_native_option_catalog(info).options
    )


def replace_format_id(item: dict[str, object], format_id: str) -> dict[str, object]:
    return {**item, "format_id": format_id}
