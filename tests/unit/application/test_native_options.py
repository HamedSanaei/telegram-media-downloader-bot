from telegram_media_bot.application.services.native_options import (
    build_native_option_catalog,
    is_native_video_option,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    SizeConfidence,
)


def test_fallback_modes_are_deduplicated_to_the_real_exact_plan() -> None:
    info = _info(
        _option(
            DownloadMode.VIDEO_2160,
            container=OutputContainer.MP4,
            selected_format_ids=("137", "140"),
            height=1080,
            video_codec="avc1.640028",
            audio_codec="mp4a.40.2",
            fallback=True,
        ),
        _option(
            DownloadMode.VIDEO_1440,
            container=OutputContainer.MP4,
            selected_format_ids=("137", "140"),
            height=1080,
            video_codec="avc1.640028",
            audio_codec="mp4a.40.2",
            fallback=True,
        ),
        _option(
            DownloadMode.VIDEO_1080,
            container=OutputContainer.MP4,
            selected_format_ids=("137", "140"),
            height=1080,
            video_codec="avc1.640028",
            audio_codec="mp4a.40.2",
        ),
        MediaFormatOption(
            mode=DownloadMode.BEST_ORIGINAL,
            container=OutputContainer.MP4,
            container_policy=ContainerPolicy.NATIVE_ONLY,
            width=1920,
            height=1080,
            fps=30,
            dynamic_range="SDR",
            size_bytes=10_000,
            size_confidence=SizeConfidence.EXACT,
            selected_format_ids=("137", "140"),
            video_codec="avc1.640028",
            audio_codec="mp4a.40.2",
        ),
    )

    catalog = build_native_option_catalog(info)
    options = catalog.for_container(OutputContainer.MP4)

    assert len(options) == 1
    assert options[0].mode is DownloadMode.VIDEO_1080
    assert options[0].actual_height == 1080
    assert "2160" not in options[0].display_label
    assert catalog.planned_option_count == 4
    assert catalog.deduplicated_option_count == 1


def test_identity_keeps_real_fps_and_dynamic_range_variants() -> None:
    options = (
        _option(
            DownloadMode.VIDEO_1080,
            container=OutputContainer.WEBM,
            selected_format_ids=("248", "251"),
            height=1080,
            fps=30,
            dynamic_range="SDR",
            video_codec="vp9",
            audio_codec="opus",
        ),
        _option(
            DownloadMode.VIDEO_1080,
            container=OutputContainer.WEBM,
            selected_format_ids=("303", "251"),
            height=1080,
            fps=60,
            dynamic_range="SDR",
            video_codec="vp09.00.41.08",
            audio_codec="opus",
        ),
        _option(
            DownloadMode.VIDEO_1080,
            container=OutputContainer.WEBM,
            selected_format_ids=("335", "251"),
            height=1080,
            fps=60,
            dynamic_range="HDR",
            video_codec="vp9",
            audio_codec="opus",
        ),
    )

    visible = build_native_option_catalog(_info(*options)).for_container(OutputContainer.WEBM)

    assert len(visible) == 3
    assert {(item.actual_fps, item.dynamic_range) for item in visible} == {
        (30, "SDR"),
        (60, "SDR"),
        (60, "HDR"),
    }


def test_transcode_and_unsupported_codec_options_are_hidden() -> None:
    transcode = _option(
        DownloadMode.VIDEO_2160,
        container=OutputContainer.MP4,
        selected_format_ids=("399", "140"),
        height=2160,
        video_codec="av01.0.12M.08",
        audio_codec="mp4a.40.2",
        requires_transcode=True,
    )
    unsupported_native_claim = _option(
        DownloadMode.VIDEO_1440,
        container=OutputContainer.MP4,
        selected_format_ids=("400", "140"),
        height=1440,
        video_codec="hevc",
        audio_codec="mp4a.40.2",
    )
    native = _option(
        DownloadMode.VIDEO_1080,
        container=OutputContainer.MP4,
        selected_format_ids=("137", "140"),
        height=1080,
        video_codec="h264",
        audio_codec="aac",
    )

    catalog = build_native_option_catalog(_info(transcode, unsupported_native_claim, native))

    assert catalog.for_container(OutputContainer.MP4)[0].actual_height == 1080
    assert catalog.hidden_transcode_option_count == 2
    assert not is_native_video_option(transcode)
    assert not is_native_video_option(unsupported_native_claim)
    assert is_native_video_option(native)
    assert all(not option.transcode_required for option in catalog.options)


def test_native_av1_and_h264_at_same_resolution_remain_distinct() -> None:
    av1 = _option(
        DownloadMode.VIDEO_1080,
        container=OutputContainer.MP4,
        selected_format_ids=("399", "140"),
        height=1080,
        video_codec="av01.0.08M.08",
        audio_codec="mp4a.40.2",
    )
    h264 = _option(
        DownloadMode.VIDEO_1080,
        container=OutputContainer.MP4,
        selected_format_ids=("137", "140"),
        height=1080,
        video_codec="avc1.640028",
        audio_codec="mp4a.40.2",
    )

    visible = build_native_option_catalog(_info(av1, h264)).for_container(OutputContainer.MP4)

    assert len(visible) == 2
    assert {option.video_codec for option in visible} == {
        "av01.0.08M.08",
        "avc1.640028",
    }
    assert {option.display_label.split(" · ")[2] for option in visible} == {"AV1", "H.264"}


def test_size_label_distinguishes_exact_approximate_and_unknown() -> None:
    exact = _option(
        DownloadMode.VIDEO_1080,
        container=OutputContainer.MP4,
        selected_format_ids=("137", "140"),
        height=1080,
        video_codec="h264",
        audio_codec="aac",
        size_bytes=80 * 1024 * 1024,
        confidence=SizeConfidence.EXACT,
    )
    approximate = _option(
        DownloadMode.VIDEO_720,
        container=OutputContainer.MP4,
        selected_format_ids=("136", "140"),
        height=720,
        video_codec="h264",
        audio_codec="aac",
        size_bytes=20 * 1024 * 1024,
        confidence=SizeConfidence.ESTIMATED,
    )
    unknown = _option(
        DownloadMode.VIDEO_480,
        container=OutputContainer.MP4,
        selected_format_ids=("135", "140"),
        height=480,
        video_codec="h264",
        audio_codec="aac",
        size_bytes=None,
        confidence=SizeConfidence.UNKNOWN,
    )

    visible = build_native_option_catalog(_info(exact, approximate, unknown)).for_container(
        OutputContainer.MP4
    )

    assert visible[0].display_label.endswith("80.0 MiB")
    assert visible[1].display_label.endswith("حدود 20.0 MiB")
    assert visible[2].display_label.endswith("حجم نامشخص")


def _info(*options: MediaFormatOption) -> MediaInfo:
    return MediaInfo(
        media_id="id",
        title="title",
        source="youtube",
        kind=MediaKind.VIDEO,
        webpage_url="https://example.test/video",
        format_options=options,
    )


def _option(
    mode: DownloadMode,
    *,
    container: OutputContainer,
    selected_format_ids: tuple[str, ...],
    height: int,
    video_codec: str,
    audio_codec: str,
    fps: float = 30,
    dynamic_range: str = "SDR",
    size_bytes: int | None = 10_000,
    confidence: SizeConfidence = SizeConfidence.EXACT,
    fallback: bool = False,
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
        fps=fps,
        dynamic_range=dynamic_range,
        is_hdr=dynamic_range == "HDR",
        size_bytes=size_bytes,
        size_confidence=confidence,
        selected_format_ids=selected_format_ids,
        video_codec=video_codec,
        audio_codec=audio_codec,
        selection_reason=(
            "native_h264_lower_resolution" if fallback else "native_h264_exact_resolution"
        ),
        fallback_reason="exact_h264_not_available" if fallback else None,
    )
