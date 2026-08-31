from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from PIL import Image

from telegram_media_bot.application.ports.download_engine import InstagramVideoDownloadEngine
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import ResolvedCredential
from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlUnsupportedUrlError,
    ImageValidationError,
    JobCancelledError,
    MediaUnavailableError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    ImageDeliveryMode,
    JobId,
    MediaInfo,
    MediaKind,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import (
    GalleryDlEngine,
    _cleanup_gallery_workspace,
    _validated_output_files,
)
from telegram_media_bot.infrastructure.gallerydl.command_builder import (
    GalleryDlCommandBuilder,
    provider_for_single_item,
)
from telegram_media_bot.infrastructure.gallerydl.errors import map_process_failure
from telegram_media_bot.infrastructure.gallerydl.mapper import map_gallery_info
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.parser import (
    parse_inspection,
    transient_asset_urls,
)
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner
from telegram_media_bot.infrastructure.media_engine_router import RoutedMediaEngine
from telegram_media_bot.infrastructure.ytdlp import engine as ytdlp_engine_module

FIXTURES = Path("tests/fixtures/gallerydl")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_events(name: str) -> list[list[Any]]:
    return _fixture_payload_events(_fixture(name))


def _fixture_payload_events(payload: bytes) -> list[list[Any]]:
    return [json.loads(line) for line in payload.splitlines()]


def _jsonl_payload(events: list[list[Any]]) -> bytes:
    return b"\n".join(json.dumps(event).encode() for event in events)


@pytest.mark.parametrize(
    ("name", "provider", "kinds"),
    [
        ("instagram-single.json", "instagram", (MediaKind.IMAGE,)),
        ("instagram-carousel.json", "instagram", (MediaKind.IMAGE, MediaKind.IMAGE)),
        ("instagram-mixed.json", "instagram", (MediaKind.IMAGE, MediaKind.VIDEO)),
        ("instagram-story.json", "instagram", (MediaKind.IMAGE,)),
        ("tiktok-photo.json", "tiktok", (MediaKind.IMAGE, MediaKind.IMAGE)),
        ("twitter-single.json", "twitter", (MediaKind.IMAGE,)),
        ("twitter-multiple.json", "twitter", (MediaKind.IMAGE, MediaKind.IMAGE)),
        ("twitter-mixed.json", "twitter", (MediaKind.IMAGE, MediaKind.VIDEO)),
        ("pinterest-single.json", "pinterest", (MediaKind.IMAGE,)),
    ],
)
def test_pinned_1328_fixtures_normalize_in_source_order(
    name: str, provider: str, kinds: tuple[MediaKind, ...]
) -> None:
    inspection = parse_inspection(_fixture(name), expected_provider=provider, max_assets=30)

    assert tuple(asset.kind for asset in inspection.assets) == kinds
    assert tuple(asset.index for asset in inspection.assets) == tuple(range(1, len(kinds) + 1))
    assert all(len(asset.asset_id) == 24 for asset in inspection.assets)


@pytest.mark.parametrize(
    ("name", "provider", "expected_modes"),
    [
        ("instagram-single.json", "instagram", {DownloadMode.IMAGE_ORIGINAL}),
        (
            "instagram-carousel.json",
            "instagram",
            {DownloadMode.IMAGES_ORIGINAL, DownloadMode.IMAGES_ZIP},
        ),
        (
            "instagram-mixed.json",
            "instagram",
            {
                DownloadMode.ALL_ORIGINAL_MEDIA,
                DownloadMode.IMAGES_ONLY,
                DownloadMode.VIDEOS_ONLY,
                DownloadMode.IMAGES_ZIP,
            },
        ),
    ],
)
def test_mapper_exposes_only_semantic_bundle_modes(
    name: str, provider: str, expected_modes: set[DownloadMode]
) -> None:
    inspection = parse_inspection(_fixture(name), expected_provider=provider, max_assets=30)
    info = map_gallery_info(inspection, "https://example.invalid/item")

    assert {option.mode for option in info.format_options} == expected_modes
    assert all(option.selected_format_ids for option in info.format_options)


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("tiktok-video.json", "tiktok"),
        ("twitter-video.json", "twitter"),
        ("pinterest-video.json", "pinterest"),
        ("instagram-reel-ytdl.json", "instagram"),
    ],
)
def test_video_only_fixtures_parse_as_video_assets(name: str, provider: str) -> None:
    inspection = parse_inspection(_fixture(name), expected_provider=provider, max_assets=30)

    assert tuple(asset.kind for asset in inspection.assets) == (MediaKind.VIDEO,)


def test_parser_rejects_invalid_and_changed_vendor_output() -> None:
    with pytest.raises(GalleryDlOutputChangedError):
        parse_inspection(b"not-json", expected_provider="twitter", max_assets=30)
    with pytest.raises(GalleryDlOutputChangedError):
        parse_inspection(b'{"unexpected": true}', expected_provider="twitter", max_assets=30)


def test_parser_rejects_pretty_printed_non_jsonl_output() -> None:
    pretty = json.dumps(_fixture_events("instagram-reel-ytdl.json"), indent=2).encode()

    with pytest.raises(GalleryDlOutputChangedError, match=r"message tuple|JSON Lines"):
        parse_inspection(pretty, expected_provider="instagram", max_assets=30)


def test_ytdl_pseudo_url_is_only_exposed_as_transient_http_url() -> None:
    payload = _fixture("instagram-reel-ytdl.json")

    assert transient_asset_urls(payload) == ("https://www.instagram.com/p/Db4UcovzZnl/1.mp4",)


def test_parser_enforces_asset_limit() -> None:
    with pytest.raises(CollectionTooLargeError):
        parse_inspection(
            _fixture("instagram-carousel.json"),
            expected_provider="instagram",
            max_assets=1,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/example/",
        "https://www.tiktok.com/@example",
        "https://x.com/example",
        "https://www.pinterest.com/example/board/",
        "https://www.pinterest.com/search/pins/?q=test",
        "https://pin.it/short-token",
    ],
)
def test_bulk_or_redirect_urls_are_rejected(url: str) -> None:
    with pytest.raises(GalleryDlUnsupportedUrlError):
        provider_for_single_item(url, frozenset({"instagram", "tiktok", "twitter", "pinterest"}))


def test_command_is_argv_only_and_every_provider_uses_the_canonical_cookie(
    settings: Settings, tmp_path: Path
) -> None:
    canonical = tmp_path / "combined.txt"
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = str(canonical)
    raw["gallery_dl"]["cookies"]["twitter"] = str(canonical)
    configured = Settings.model_validate(raw)
    commands = GalleryDlCommandBuilder(configured.gallery_dl, configured.effective_cookie_file())

    _provider, instagram = commands.inspection("https://instagram.com/p/abc123/")
    _provider, tiktok = commands.inspection("https://tiktok.com/@example/photo/123")
    _provider, twitter = commands.inspection("https://x.com/example/status/123")
    _provider, pinterest = commands.inspection("https://pinterest.com/pin/123/")

    for args in (instagram, tiktok, twitter, pinterest):
        assert args[:3] == [sys.executable, "-m", "gallery_dl"]
        assert "--config-ignore" in args
        assert "--no-input" in args
        assert "--no-colors" in args
        assert "--dump-json" in args
        assert "--no-download" in args
        option_index = args.index("-o")
        assert args[option_index : option_index + 2] == ["-o", "output.jsonl=true"]
        assert args[args.index("--cookies") + 1] == str(canonical.resolve())


def test_canonical_cookie_is_shared_with_every_gallery_provider(
    settings: Settings, tmp_path: Path
) -> None:
    canonical = tmp_path / "combined.txt"
    commands = GalleryDlCommandBuilder(settings.gallery_dl, canonical)

    for url in (
        "https://instagram.com/p/abc123/",
        "https://tiktok.com/@example/photo/123",
        "https://x.com/example/status/123",
        "https://pinterest.com/pin/123/",
    ):
        args = commands.inspection(url)[1]
        assert args[args.index("--cookies") + 1] == str(canonical)


def test_per_attempt_cookie_file_overrides_canonical(settings: Settings, tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.txt"
    per_attempt = tmp_path / "user-session.txt"
    commands = GalleryDlCommandBuilder(settings.gallery_dl, canonical)

    _provider, args = commands.inspection(
        "https://instagram.com/p/abc123/", cookie_file=str(per_attempt)
    )
    assert args[args.index("--cookies") + 1] == str(per_attempt)

    _provider, download_args = commands.download(
        "https://instagram.com/p/abc123/",
        tmp_path / "out",
        cookie_file=str(per_attempt),
    )
    assert download_args[download_args.index("--cookies") + 1] == str(per_attempt)

    probe_args = commands.inspect_url(
        "instagram", "https://instagram.com/p/abc123/", cookie_file=str(per_attempt)
    )
    assert probe_args[probe_args.index("--cookies") + 1] == str(per_attempt)


def test_no_per_attempt_cookie_falls_back_to_canonical(settings: Settings, tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.txt"
    commands = GalleryDlCommandBuilder(settings.gallery_dl, canonical)

    _provider, args = commands.inspection("https://instagram.com/p/abc123/")
    assert args[args.index("--cookies") + 1] == str(canonical)


def test_explicit_no_credential_does_not_use_canonical(settings: Settings, tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.txt"
    commands = GalleryDlCommandBuilder(settings.gallery_dl, canonical)
    _provider, args = commands.inspection(
        "https://instagram.com/p/abc123/", credential=ResolvedCredential.none()
    )
    assert "--cookies" not in args


def test_instagram_image_download_explicitly_disables_gallery_video_downloads(
    settings: Settings,
    tmp_path: Path,
) -> None:
    commands = GalleryDlCommandBuilder(settings.gallery_dl, None)

    _provider, args = commands.download(
        "https://instagram.com/p/abc123/",
        tmp_path,
        images_only=True,
    )

    option_index = args.index("-o")
    assert args[option_index : option_index + 2] == [
        "-o",
        "extractor.instagram.videos=false",
    ]


def test_safe_nonzero_mapping_does_not_expose_stderr() -> None:
    error = map_process_failure(1, _fixture("instagram-auth-required.txt"))
    assert isinstance(error, GalleryDlAuthenticationRequiredError)
    expired = map_process_failure(1, _fixture("instagram-expired-cookies.txt"))
    assert isinstance(expired, GalleryDlCookiesExpiredError)
    assert isinstance(
        map_process_failure(1, _fixture("tiktok-private.txt")),
        MediaUnavailableError,
    )
    assert isinstance(
        map_process_failure(1, _fixture("twitter-unavailable.txt")),
        MediaUnavailableError,
    )
    secret = map_process_failure(1, b"Login required: secret cookie path C:/private/c.txt")
    assert "private" not in str(secret)
    assert isinstance(
        map_process_failure(1, b"HTTP 403 Forbidden"),
        GalleryDlAuthenticationRequiredError,
    )
    assert isinstance(map_process_failure(1, b"HTTP 404 Not Found"), MediaUnavailableError)


async def test_runner_bounds_stdout_and_times_out() -> None:
    runner = GalleryDlRunner(stdout_limit=4)
    with pytest.raises(GalleryDlOutputChangedError):
        await runner.run_async([sys.executable, "-c", "print('12345')"], timeout_seconds=5)
    with pytest.raises(GalleryDlExtractionError, match="timed out"):
        await runner.run_async(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.1,
        )


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://instagram.com/p/abc_123/", "instagram"),
        ("https://instagram.com/stories/example/123/", "instagram"),
        ("https://instagram.com/stories/highlights/123/", "instagram"),
        ("https://www.tiktok.com/@example/photo/123", "tiktok"),
        ("https://x.com/example/status/123?s=20", "twitter"),
        ("https://pinterest.com/pin/123/", "pinterest"),
    ],
)
def test_supported_scope_accepts_only_single_item_shapes(url: str, provider: str) -> None:
    assert (
        provider_for_single_item(url, frozenset({"instagram", "tiktok", "twitter", "pinterest"}))
        == provider
    )


def test_supported_scope_accepts_avatar_and_reels_shapes() -> None:
    assert (
        provider_for_single_item(
            "https://www.instagram.com/exampleuser/avatar/",
            frozenset({"instagram"}),
        )
        == "instagram"
    )
    assert (
        provider_for_single_item(
            "https://www.instagram.com/reels/AbC123/",
            frozenset({"instagram"}),
        )
        == "instagram"
    )


def test_story_account_url_is_now_a_bulk_collection() -> None:
    # v1.3.4: /stories/USERNAME/ is the authenticated all-stories collection target.
    assert (
        provider_for_single_item(
            "https://www.instagram.com/stories/exampleuser/",
            frozenset({"instagram"}),
        )
        == "instagram"
    )


def test_unsupported_instagram_paths_are_rejected() -> None:
    for url in (
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/stories/me/",
        "https://www.instagram.com/exampleuser/posts/",
    ):
        with pytest.raises(GalleryDlUnsupportedUrlError):
            provider_for_single_item(url, frozenset({"instagram"}))


def test_story_video_inspection_offers_video_original() -> None:
    inspection = parse_inspection(
        _fixture("instagram-story-video.json"), expected_provider="instagram", max_assets=30
    )
    info = map_gallery_info(inspection, "https://www.instagram.com/stories/u/3964254748584813861/")

    assert tuple(asset.kind for asset in inspection.assets) == (MediaKind.VIDEO,)
    assert inspection.post_id == "3964254748584813861"
    assert info.kind is MediaKind.VIDEO
    assert {option.mode for option in info.format_options} == {DownloadMode.VIDEO_ORIGINAL}


def test_avatar_inspection_offers_image_original() -> None:
    inspection = parse_inspection(
        _fixture("instagram-avatar.json"), expected_provider="instagram", max_assets=30
    )
    info = map_gallery_info(inspection, "https://www.instagram.com/cristiano/avatar/")

    assert tuple(asset.kind for asset in inspection.assets) == (MediaKind.IMAGE,)
    assert info.kind is MediaKind.IMAGE
    assert {option.mode for option in info.format_options} == {DownloadMode.IMAGE_ORIGINAL}


def test_story_image_inspection_keeps_image_original() -> None:
    inspection = parse_inspection(
        _fixture("instagram-story.json"), expected_provider="instagram", max_assets=30
    )
    info = map_gallery_info(inspection, "https://www.instagram.com/stories/u/991/")

    assert tuple(asset.kind for asset in inspection.assets) == (MediaKind.IMAGE,)
    assert info.kind is MediaKind.IMAGE
    assert {option.mode for option in info.format_options} == {DownloadMode.IMAGE_ORIGINAL}


def test_expired_story_empty_output_is_media_unavailable(settings: Settings) -> None:
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(b"")),
    )
    engine._validator = cast(Any, _AllowValidator())

    with pytest.raises(MediaUnavailableError, match="story is expired or unavailable"):
        engine.inspect("https://www.instagram.com/stories/exampleuser/123/")


def test_successful_empty_instagram_post_is_unavailable_not_output_changed(
    settings: Settings,
) -> None:
    runner = _FixtureRunner(b"")
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, runner),
    )
    engine._validator = cast(Any, _AllowValidator())

    with pytest.raises(MediaUnavailableError, match="unavailable or inaccessible"):
        engine.inspect("https://www.instagram.com/p/Db8-JS3jOMs/?img_index=2&igsi=synthetic")
    assert runner.inspections == 1
    assert len(runner.commands) == 1
    assert "--get-urls" not in runner.commands[0]
    assert "--list-keywords" not in runner.commands[0]


def test_successful_empty_instagram_output_uses_same_request_auth_evidence(
    settings: Settings,
) -> None:
    runner = _FixtureRunner(b"", stderr=b"HTTP 403 Forbidden: login required")
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))
    engine._validator = cast(Any, _AllowValidator())

    with pytest.raises(GalleryDlAuthenticationRequiredError):
        engine.inspect("https://www.instagram.com/p/Db8-JS3jOMs/")

    assert runner.inspections == 1
    assert len(runner.commands) == 1


def test_exact_instagram_regression_url_returns_full_carousel_fixture(
    settings: Settings,
) -> None:
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("instagram-carousel.json"))),
    )
    engine._validator = cast(Any, _AllowValidator())

    info = engine.inspect("https://www.instagram.com/p/Db8-JS3jOMs/?img_index=2&igsi=synthetic")

    assert info.webpage_url == "https://www.instagram.com/p/Db8-JS3jOMs/"
    assert tuple(asset.kind for asset in info.assets) == (MediaKind.IMAGE, MediaKind.IMAGE)


def test_story_video_download_succeeds_within_configured_limits(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LargeVideoRunner(_FixtureRunner):
        def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float,
            is_cancelled: object = None,
            **kwargs: object,
        ) -> GalleryProcessResult:
            if "--dump-json" in args:
                return super().run(
                    args,
                    timeout_seconds=timeout_seconds,
                    is_cancelled=is_cancelled,
                    **kwargs,
                )
            workspace = Path(args[args.index("--directory") + 1])
            self.download_commands.append(args)
            # Model the production story artifact: ~6.35 MB of actual bytes with only partial
            # extractor metadata (no filesize/duration fields). This must download successfully
            # under the configured limits instead of raising a false MediaTooLargeError.
            path = workspace / "0001-story.mp4"
            path.write_bytes(b"x" * 6_355_000)
            return GalleryProcessResult(0, b"", b"", 0.01)

    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.gallerydl.adapter.is_inline_video_streamable",
        lambda _path: False,
    )
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, LargeVideoRunner(_fixture("instagram-story-video.json"))),
    )
    engine._validator = cast(Any, _AllowValidator())
    info = engine.inspect("https://www.instagram.com/stories/u/3964254748584813861/")
    option = next(item for item in info.format_options if item.mode is DownloadMode.VIDEO_ORIGINAL)

    result = engine.download(
        DownloadRequest(
            job_id=JobId("story-job"),
            url=info.webpage_url,
            mode=option.mode,
            output_directory=tmp_path / "job",
            selected_format_ids=option.selected_format_ids,
        )
    )

    assert result.kind is MediaKind.VIDEO
    assert result.file_path.is_file()
    assert result.file_size_bytes == 6_355_000


def test_avatar_downloads_as_original_image(settings: Settings, tmp_path: Path) -> None:
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("instagram-avatar.json"))),
    )
    engine._validator = cast(Any, _AllowValidator())
    info = engine.inspect("https://www.instagram.com/cristiano/avatar/")
    option = next(item for item in info.format_options if item.mode is DownloadMode.IMAGE_ORIGINAL)

    result = engine.download(
        DownloadRequest(
            job_id=JobId("avatar-job"),
            url=info.webpage_url,
            mode=option.mode,
            output_directory=tmp_path / "job",
            selected_format_ids=option.selected_format_ids,
        )
    )

    assert result.kind is MediaKind.IMAGE
    assert result.file_path.is_file()
    assert result.file_path.suffix.casefold() == ".jpg"


async def test_runner_cancellation_terminates_process_group() -> None:
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(JobCancelledError):
        await GalleryDlRunner().run_async(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=5,
            is_cancelled=cancelled,
        )


class _FixtureRunner:
    def __init__(self, payload: bytes, *, stderr: bytes = b"") -> None:
        self.payload = payload
        self.stderr = stderr
        self.inspections = 0
        self.commands: list[list[str]] = []
        self.download_commands: list[list[str]] = []

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        is_cancelled: object = None,
        **_kwargs: object,
    ) -> GalleryProcessResult:
        del timeout_seconds, is_cancelled
        self.commands.append(args)
        if "--version" in args:
            return GalleryProcessResult(0, b"1.32.8\n", b"", 0.01)
        if "--dump-json" in args:
            self.inspections += 1
            return GalleryProcessResult(0, self.payload, self.stderr, 0.01)
        workspace = Path(args[args.index("--directory") + 1])
        self.download_commands.append(args)
        events = [event for event in _fixture_payload_events(self.payload) if event[0] == 3]
        for index, event in enumerate(events, start=1):
            metadata = event[2]
            if "extractor.instagram.videos=false" in args and metadata["type"] == "video":
                continue
            extension = metadata["extension"]
            path = workspace / f"{index:04}-{metadata['type']}.{extension}"
            if extension.casefold() in {"jpg", "jpeg", "png", "webp", "gif", "avif"}:
                Image.new("RGB", (16, 16)).save(path, format="JPEG")
            else:
                path.write_bytes(b"video")
        return GalleryProcessResult(0, b"", b"", 0.01)


class _AllowValidator:
    def validate(self, url: str) -> str:
        return url


def test_adapter_reinspects_and_downloads_original_image(
    settings: Settings, tmp_path: Path
) -> None:
    runner = _FixtureRunner(_fixture("instagram-single.json"))
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, runner),
    )
    engine._validator = cast(Any, _AllowValidator())
    info = engine.inspect("https://instagram.com/p/abc123/")
    result = engine.download(
        DownloadRequest(
            job_id=JobId("gallery-job"),
            url=info.webpage_url,
            mode=DownloadMode.IMAGE_ORIGINAL,
            output_directory=tmp_path / "job",
            selected_format_ids=info.format_options[0].selected_format_ids,
        )
    )

    assert result.kind is MediaKind.IMAGE
    assert result.file_path.is_file()
    assert result.file_size_bytes < 10_000
    assert runner.inspections == 2


def test_adapter_cleans_partial_workspace_on_cancellation(
    settings: Settings, tmp_path: Path
) -> None:
    class CancelRunner(_FixtureRunner):
        def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float,
            is_cancelled: object = None,
            **kwargs: object,
        ) -> GalleryProcessResult:
            if "--dump-json" in args:
                return super().run(
                    args,
                    timeout_seconds=timeout_seconds,
                    is_cancelled=is_cancelled,
                    **kwargs,
                )
            workspace = Path(args[args.index("--directory") + 1])
            (workspace / "0001-image.jpg.part").write_bytes(b"partial")
            raise JobCancelledError("cancelled")

    workspace = tmp_path / "cancelled-job"
    engine = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, CancelRunner(_fixture("instagram-single.json"))),
    )
    engine._validator = cast(Any, _AllowValidator())
    info = engine.inspect("https://instagram.com/p/abc123/")

    with pytest.raises(JobCancelledError):
        engine.download(
            DownloadRequest(
                JobId("cancelled"),
                info.webpage_url,
                DownloadMode.IMAGE_ORIGINAL,
                workspace,
                selected_format_ids=info.format_options[0].selected_format_ids,
            )
        )

    assert workspace.is_dir()
    assert not any(workspace.iterdir())


def test_adapter_rejects_known_collection_size_before_download(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["gallery_dl"]["max_total_size_mb"] = 1
    configured = Settings.model_validate(raw)
    events = _fixture_events("instagram-single.json")
    events[0][2]["filesize"] = 2 * 1024 * 1024
    engine = GalleryDlEngine(
        configured,
        runner=cast(GalleryDlRunner, _FixtureRunner(_jsonl_payload(events))),
    )
    engine._validator = cast(Any, _AllowValidator())

    with pytest.raises(CollectionTooLargeError):
        engine.inspect("https://instagram.com/p/abc123/")


def test_workspace_rejects_unexpected_directory_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    (workspace / "unexpected").mkdir()
    with pytest.raises(ImageValidationError, match="unexpected entry"):
        _validated_output_files(workspace)
    _cleanup_gallery_workspace(workspace)
    assert not any(workspace.iterdir())

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"image")
    link = workspace / "0001-image.jpg"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ImageValidationError, match="escapes"):
        _validated_output_files(workspace)


class _FakeEngine:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def inspect(self, url: str) -> MediaInfo:
        self.inspected.append(url)
        return MediaInfo("video", "Video", "twitter", MediaKind.VIDEO, url)

    def download(self, request: DownloadRequest, **_kwargs: object) -> DownloadResult:
        raise AssertionError(request)

    def download_instagram_video_children(
        self,
        request: DownloadRequest,
        *,
        expected_parent_media_id: str,
        expected_total_slots: int,
        expected_video_indices: tuple[int, ...],
        **kwargs: object,
    ) -> DownloadResult:
        del expected_parent_media_id, expected_total_slots
        result = self.download(request, **kwargs)
        return replace(
            result,
            artifacts=tuple(
                replace(
                    artifact,
                    source_index=(
                        expected_video_indices[index]
                        if index < len(expected_video_indices)
                        else None
                    ),
                )
                for index, artifact in enumerate(result.delivery_artifacts)
            ),
        )

    def health(self) -> ComponentHealth:
        return ComponentHealth("yt_dlp", True, "test")


def test_router_keeps_video_only_single_post_gallery_owned(settings: Settings) -> None:
    gallery = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("twitter-video.json"))),
    )
    gallery._validator = cast(Any, _AllowValidator())
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(gallery, cast(InstagramVideoDownloadEngine, ytdlp))

    info = router.inspect("https://x.com/example/status/8004?s=20")

    assert info.kind is MediaKind.VIDEO
    assert tuple(asset.kind for asset in info.assets) == (MediaKind.VIDEO,)
    assert {option.mode for option in info.format_options} == {DownloadMode.VIDEO_ORIGINAL}
    assert ytdlp.inspected == []


def test_router_keeps_instagram_reel_ytdl_video_event_gallery_owned(settings: Settings) -> None:
    gallery = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("instagram-reel-ytdl.json"))),
    )
    gallery._validator = cast(Any, _AllowValidator())
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(gallery, cast(InstagramVideoDownloadEngine, ytdlp))

    info = router.inspect("https://www.instagram.com/reel/Db4UcovzZnl/")

    assert info.kind is MediaKind.VIDEO
    assert tuple(asset.kind for asset in info.assets) == (MediaKind.VIDEO,)
    assert {option.mode for option in info.format_options} == {DownloadMode.VIDEO_ORIGINAL}
    assert ytdlp.inspected == []


def test_router_keeps_mixed_instagram_ytdl_post_gallery_owned(settings: Settings) -> None:
    gallery = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("instagram-mixed.json"))),
    )
    gallery._validator = cast(Any, _AllowValidator())
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(gallery, cast(InstagramVideoDownloadEngine, ytdlp))

    info = router.inspect("https://www.instagram.com/p/IG3/")

    assert tuple(asset.kind for asset in info.assets) == (MediaKind.IMAGE, MediaKind.VIDEO)
    assert ytdlp.inspected == []
    assert "cdn.example.invalid" not in repr(info)
    assert "ytdl:" not in repr(info)


def test_router_downloads_mixed_instagram_images_with_gallery_and_video_with_ytdlp(
    settings: Settings,
    tmp_path: Path,
) -> None:
    runner = _FixtureRunner(_fixture("instagram-mixed-four.json"))
    gallery = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))
    gallery._validator = cast(Any, _AllowValidator())

    class VideoEngine(_FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.downloaded: list[DownloadRequest] = []

        def download(self, request: DownloadRequest, **_kwargs: object) -> DownloadResult:
            self.downloaded.append(request)
            request.output_directory.mkdir(parents=True)
            path = request.output_directory / "0001.mp4"
            path.write_bytes(b"yt-dlp-video")
            return DownloadResult(
                job_id=request.job_id,
                media_id="IG4",
                title="Video",
                source="instagram",
                kind=MediaKind.VIDEO,
                file_path=path,
                file_size_bytes=path.stat().st_size,
                mime_type="video/mp4",
                inline_video_streamable=True,
            )

    ytdlp = VideoEngine()
    router = RoutedMediaEngine(gallery, cast(InstagramVideoDownloadEngine, ytdlp))
    info = router.inspect("https://www.instagram.com/p/IG4/")
    option = next(
        item for item in info.format_options if item.mode is DownloadMode.ALL_ORIGINAL_MEDIA
    )

    result = router.download(
        DownloadRequest(
            job_id=JobId("mixed-job"),
            url=info.webpage_url,
            mode=option.mode,
            output_directory=tmp_path / "job",
            temp_directory=tmp_path / "temp",
            selected_format_ids=option.selected_format_ids,
            allow_collection=True,
            image_delivery_mode=ImageDeliveryMode.PHOTO,
        )
    )

    assert [item.kind for item in result.artifacts] == [
        MediaKind.IMAGE,
        MediaKind.IMAGE,
        MediaKind.VIDEO,
        MediaKind.IMAGE,
    ]
    assert [item.source_index for item in result.artifacts] == [1, 2, 3, 4]
    assert len(ytdlp.downloaded) == 1
    assert ytdlp.downloaded[0].url == "https://www.instagram.com/p/IG4/"
    assert ytdlp.downloaded[0].selected_format_ids == ()
    assert ytdlp.downloaded[0].output_directory.name == "videos"
    assert "extractor.instagram.videos=false" in runner.download_commands[0]
    assert "cdn.example.invalid" not in repr(result)
    assert "ytdl:" not in repr(result)


def test_production_mixed_carousel_resolves_only_the_real_video_child(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_url = "https://www.instagram.com/p/DZUwLh3jEDk/"
    video_url = "https://www.instagram.com/p/DZUtxnNDJg7/"
    photo_child_id = "DZUtbhzsvJy"
    entries: list[dict[str, Any]] = []
    for source_index in range(1, 18):
        if source_index == 11:
            entries.append(
                {
                    "id": "DZUtxnNDJg7",
                    "title": "Video child",
                    "extractor_key": "Instagram",
                    "vcodec": "avc1.64001f",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "formats": [
                        {
                            "format_id": "dash-video",
                            "vcodec": "avc1.64001f",
                            "acodec": "none",
                            "ext": "mp4",
                        }
                    ],
                }
            )
        else:
            entries.append(
                {
                    "id": photo_child_id if source_index == 1 else f"PHOTO{source_index:02d}",
                    "title": "Photo child",
                    "extractor_key": "Instagram",
                    "formats": [],
                }
            )

    class ProductionCarouselYoutubeDL:
        calls: ClassVar[list[tuple[str, bool, bool]]] = []

        def __init__(self, options: dict[str, Any]) -> None:
            assert "ignoreerrors" not in options
            self.options = options
            self.format_selector = lambda context: iter(context["formats"][-1:])

        def __enter__(self) -> ProductionCarouselYoutubeDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(
            self,
            url: str,
            *,
            download: bool,
            process: bool = True,
        ) -> dict[str, Any]:
            self.calls.append((url, download, process))
            if url == parent_url:
                assert not download
                assert not process
                return {
                    "_type": "playlist",
                    "id": "DZUwLh3jEDk",
                    "title": "Production mixed carousel",
                    "extractor_key": "Instagram",
                    "entries": entries,
                }
            if url != video_url:
                raise AssertionError(f"photo child was selected as video: {url}")
            assert download
            output = Path(self.options["paths"]["home"]) / "DZUtxnNDJg7.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"valid-video-child")
            return {
                "id": "DZUtxnNDJg7",
                "title": "Video by fixture",
                "extractor_key": "Instagram",
                "webpage_url": video_url,
                "vcodec": "avc1.64001f",
                "acodec": "mp4a.40.2",
                "ext": "mp4",
                "duration": 18,
            }

        def sanitize_info(self, raw: Any) -> Any:
            return raw

    raw_settings = settings.model_dump()
    raw_settings["security"]["reject_private_network_urls"] = False
    configured = Settings.model_validate(raw_settings)
    runner = _FixtureRunner(_fixture("instagram-mixed-seventeen.json"))
    gallery = GalleryDlEngine(configured, runner=cast(GalleryDlRunner, runner))
    gallery._validator = cast(Any, _AllowValidator())
    monkeypatch.setattr(ytdlp_engine_module, "YoutubeDL", ProductionCarouselYoutubeDL)
    monkeypatch.setattr(ytdlp_engine_module, "is_inline_video_streamable", lambda _path: True)
    monkeypatch.setattr(
        ytdlp_engine_module.YtDlpEngine,
        "_log_selected_media",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    router = RoutedMediaEngine(gallery, ytdlp_engine_module.YtDlpEngine(configured))
    info = router.inspect(parent_url)

    result = router.download(
        DownloadRequest(
            JobId("production-mixed"),
            parent_url,
            DownloadMode.ALL_ORIGINAL_MEDIA,
            configured.storage.downloads_path() / "production-mixed",
            temp_directory=configured.storage.temp_path() / "production-mixed",
            image_delivery_mode=ImageDeliveryMode.PHOTO,
        )
    )

    assert len(info.assets) == 17
    assert sum(asset.kind is MediaKind.IMAGE for asset in info.assets) == 16
    assert [artifact.source_index for artifact in result.delivery_artifacts] == list(range(1, 18))
    assert [artifact.kind for artifact in result.delivery_artifacts].count(MediaKind.IMAGE) == 16
    assert [artifact.kind for artifact in result.delivery_artifacts].count(MediaKind.VIDEO) == 1
    assert result.delivery_artifacts[10].file_path.name == "DZUtxnNDJg7.mp4"
    assert ProductionCarouselYoutubeDL.calls == [
        (parent_url, False, False),
        (video_url, True, True),
    ]
    assert len(runner.download_commands) == 1
    assert "extractor.instagram.videos=false" in runner.download_commands[0]
    assert photo_child_id not in repr(ProductionCarouselYoutubeDL.calls)
    assert "cdn.example.invalid" not in repr(result)
    assert "ytdl:" not in repr(result)


def test_router_fails_closed_when_ytdlp_video_count_does_not_match_plan(
    settings: Settings,
    tmp_path: Path,
) -> None:
    runner = _FixtureRunner(_fixture("instagram-mixed-four.json"))
    gallery = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))
    gallery._validator = cast(Any, _AllowValidator())

    class ExtraVideoEngine(_FakeEngine):
        def download(self, request: DownloadRequest, **_kwargs: object) -> DownloadResult:
            request.output_directory.mkdir(parents=True)
            artifacts: list[DownloadArtifact] = []
            for index in (1, 2):
                path = request.output_directory / f"{index:04}.mp4"
                path.write_bytes(b"video")
                artifacts.append(
                    DownloadArtifact(path, 5, MediaKind.VIDEO, "video/mp4", f"Video {index}")
                )
            return DownloadResult(
                request.job_id,
                "IG4",
                "Videos",
                "instagram",
                MediaKind.PLAYLIST,
                artifacts[0].file_path,
                10,
                artifacts=tuple(artifacts),
            )

    router = RoutedMediaEngine(
        gallery,
        cast(InstagramVideoDownloadEngine, ExtraVideoEngine()),
    )
    info = router.inspect("https://www.instagram.com/p/IG4/")

    with pytest.raises(GalleryDlOutputChangedError, match="video count"):
        router.download(
            DownloadRequest(
                JobId("mismatch"),
                info.webpage_url,
                DownloadMode.ALL_ORIGINAL_MEDIA,
                tmp_path / "job",
                temp_directory=tmp_path / "temp",
                image_delivery_mode=ImageDeliveryMode.PHOTO,
            )
        )

    assert not (tmp_path / "job" / "images").exists()
    assert not (tmp_path / "job" / "videos").exists()
    assert runner.download_commands == []


def test_router_never_turns_social_bulk_url_into_ytdlp_crawl(settings: Settings) -> None:
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(
        GalleryDlEngine(settings),
        cast(InstagramVideoDownloadEngine, ytdlp),
    )

    with pytest.raises(GalleryDlUnsupportedUrlError):
        router.inspect("https://www.pinterest.com/example/board/")
    assert not ytdlp.inspected
