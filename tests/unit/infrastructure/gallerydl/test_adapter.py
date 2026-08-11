from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from telegram_media_bot.application.ports.download_engine import DownloadEngine
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlExtractionError,
    GalleryDlNoImagesError,
    GalleryDlOutputChangedError,
    GalleryDlUnsupportedUrlError,
    ImageValidationError,
    JobCancelledError,
    MediaUnavailableError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
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
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner
from telegram_media_bot.infrastructure.media_engine_router import RoutedMediaEngine

FIXTURES = Path("tests/fixtures/gallerydl")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


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
    ],
)
def test_video_only_fixture_signals_ytdlp_fallback(name: str, provider: str) -> None:
    with pytest.raises(GalleryDlNoImagesError):
        parse_inspection(_fixture(name), expected_provider=provider, max_assets=30)


def test_parser_rejects_invalid_and_changed_vendor_output() -> None:
    with pytest.raises(GalleryDlOutputChangedError):
        parse_inspection(b"not-json", expected_provider="twitter", max_assets=30)
    with pytest.raises(GalleryDlOutputChangedError):
        parse_inspection(b'{"unexpected": true}', expected_provider="twitter", max_assets=30)


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


def test_command_is_argv_only_and_cookie_is_source_isolated(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["gallery_dl"]["cookies"] = {
        "instagram": str(tmp_path / "ig.txt"),
        "twitter": str(tmp_path / "x.txt"),
    }
    configured = Settings.model_validate(raw)
    commands = GalleryDlCommandBuilder(configured.gallery_dl, None)

    _provider, instagram = commands.inspection("https://instagram.com/p/abc123/")
    _provider, twitter = commands.inspection("https://x.com/example/status/123")

    for args in (instagram, twitter):
        assert args[:3] == [sys.executable, "-m", "gallery_dl"]
        assert "--config-ignore" in args
        assert "--no-input" in args
        assert "--no-colors" in args
        assert "--dump-json" in args
        assert "--no-download" in args
    assert str(tmp_path / "ig.txt") in instagram
    assert str(tmp_path / "x.txt") not in instagram
    assert str(tmp_path / "x.txt") in twitter
    assert str(tmp_path / "ig.txt") not in twitter


def test_legacy_instagram_cookie_is_not_shared(settings: Settings, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.txt"
    commands = GalleryDlCommandBuilder(settings.gallery_dl, legacy)
    assert str(legacy) in commands.inspection("https://instagram.com/p/abc123/")[1]
    assert str(legacy) not in commands.inspection("https://x.com/example/status/123")[1]


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
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.inspections = 0

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        is_cancelled: object = None,
        **_kwargs: object,
    ) -> GalleryProcessResult:
        del timeout_seconds, is_cancelled
        if "--version" in args:
            return GalleryProcessResult(0, b"1.32.8\n", b"", 0.01)
        if "--dump-json" in args:
            self.inspections += 1
            return GalleryProcessResult(0, self.payload, b"", 0.01)
        workspace = Path(args[args.index("--directory") + 1])
        events = json.loads(self.payload)
        for index, event in enumerate(events, start=1):
            metadata = event[2]
            extension = metadata["extension"]
            path = workspace / f"{index:04}-{metadata['type']}.{extension}"
            if metadata["type"] in {"image", "photo"}:
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
    events = json.loads(_fixture("instagram-single.json"))
    events[0][2]["filesize"] = 2 * 1024 * 1024
    engine = GalleryDlEngine(
        configured,
        runner=cast(GalleryDlRunner, _FixtureRunner(json.dumps(events).encode())),
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

    def health(self) -> ComponentHealth:
        return ComponentHealth("yt_dlp", True, "test")


def test_router_falls_back_only_for_video_only_single_post(settings: Settings) -> None:
    gallery = GalleryDlEngine(
        settings,
        runner=cast(GalleryDlRunner, _FixtureRunner(_fixture("twitter-video.json"))),
    )
    gallery._validator = cast(Any, _AllowValidator())
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(gallery, cast(DownloadEngine, ytdlp))

    info = router.inspect("https://x.com/example/status/8004?s=20")

    assert info.kind is MediaKind.VIDEO
    assert ytdlp.inspected == ["https://x.com/example/status/8004"]


def test_router_never_turns_social_bulk_url_into_ytdlp_crawl(settings: Settings) -> None:
    ytdlp = _FakeEngine()
    router = RoutedMediaEngine(GalleryDlEngine(settings), cast(DownloadEngine, ytdlp))

    with pytest.raises(GalleryDlUnsupportedUrlError):
        router.inspect("https://www.pinterest.com/example/board/")
    assert not ytdlp.inspected
