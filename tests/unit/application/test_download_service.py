from collections.abc import Callable
from pathlib import Path

import pytest

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.domain.errors import (
    InvalidUrlError,
    MediaTooLargeError,
    PlaylistNotAllowedError,
    UnsupportedSourceError,
)
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    ErrorCategory,
    ImageDeliveryMode,
    JobId,
    MediaInfo,
    MediaKind,
    ProgressEvent,
)


class FakeEngine:
    def __init__(
        self,
        kind: MediaKind = MediaKind.VIDEO,
        *,
        estimated_size_bytes: int | None = None,
        result_size_bytes: int = 4,
    ) -> None:
        self.kind = kind
        self.estimated_size_bytes = estimated_size_bytes
        self.result_size_bytes = result_size_bytes
        self.inspection_urls: list[str] = []
        self.download_requests: list[DownloadRequest] = []

    def inspect(self, url: str) -> MediaInfo:
        self.inspection_urls.append(url)
        return MediaInfo(
            media_id="1",
            title="Example",
            source="example",
            kind=self.kind,
            webpage_url=url,
            item_count=1 if self.kind is MediaKind.PLAYLIST else None,
            estimated_size_bytes=self.estimated_size_bytes,
        )

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: Callable[[ProgressEvent], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        del progress, is_cancelled
        self.download_requests.append(request)
        path = request.output_directory / "result.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * self.result_size_bytes)
        return DownloadResult(
            job_id=request.job_id,
            media_id="1",
            title="Example",
            source="example",
            kind=MediaKind.VIDEO,
            file_path=path,
            file_size_bytes=self.result_size_bytes,
        )

    def health(self) -> ComponentHealth:
        return ComponentHealth("fake", True)


def test_rejects_non_http_url() -> None:
    service = DownloadService(FakeEngine(), frozenset({"example"}))
    with pytest.raises(InvalidUrlError):
        service.inspect("file:///etc/passwd")


def test_exact_inspection_format_pair_reaches_download_engine(tmp_path: Path) -> None:
    engine = FakeEngine()
    service = DownloadService(engine, frozenset({"example"}))
    selected = ("hls-1672", "hls-audio-128000-Audio")

    service.download(
        job_id=JobId("twitter-pair"),
        url="https://example.com/media",
        mode=DownloadMode.BEST,
        output_directory=tmp_path / "twitter-pair",
        selected_format_ids=selected,
    )

    assert engine.download_requests[0].selected_format_ids == selected


def test_image_delivery_mode_reaches_download_engine_explicitly(tmp_path: Path) -> None:
    engine = FakeEngine()
    service = DownloadService(engine, frozenset({"example"}))

    service.download(
        job_id=JobId("image-mode"),
        url="https://example.com/media",
        mode=DownloadMode.IMAGE_ORIGINAL,
        output_directory=tmp_path / "image-mode",
        image_delivery_mode=ImageDeliveryMode.DOCUMENT,
    )

    assert engine.download_requests[0].image_delivery_mode is ImageDeliveryMode.DOCUMENT


def test_rejects_url_credentials() -> None:
    with pytest.raises(InvalidUrlError):
        DownloadService.validate_url(
            "https://user:pass@example.com/media"  # pragma: allowlist secret
        )


def test_rejects_disabled_source() -> None:
    service = DownloadService(FakeEngine(), frozenset({"other"}))
    with pytest.raises(UnsupportedSourceError):
        service.inspect("https://example.com/media")


def test_rejects_playlist_in_default_download_flow(tmp_path: Path) -> None:
    service = DownloadService(FakeEngine(MediaKind.PLAYLIST), frozenset({"example"}))
    with pytest.raises(PlaylistNotAllowedError):
        service.download(
            job_id=JobId("job-1"),
            url="https://example.com/media",
            mode=DownloadMode.BEST,
            output_directory=tmp_path / "job-1",
        )


def test_allows_bounded_playlist_when_enabled() -> None:
    service = DownloadService(
        FakeEngine(MediaKind.PLAYLIST),
        frozenset({"example"}),
        allow_playlists=True,
    )
    assert service.inspect("https://example.com/media").kind is MediaKind.PLAYLIST


def test_real_playlist_still_enforces_item_limit() -> None:
    service = DownloadService(
        FakeEngine(MediaKind.PLAYLIST),
        frozenset({"example"}),
        allow_playlists=True,
        playlist_max_items=0,
    )

    with pytest.raises(PlaylistNotAllowedError):
        service.inspect("https://www.youtube.com/playlist?list=PL123")


def test_download_uses_project_contract(tmp_path: Path) -> None:
    service = DownloadService(FakeEngine(), frozenset({"example"}))
    result = service.download(
        job_id=JobId("job-1"),
        url="https://example.com/media",
        mode=DownloadMode.BEST,
        output_directory=tmp_path / "job-1",
    )
    assert result.file_path.read_bytes() == b"xxxx"


def test_youtube_mix_uses_canonical_url_for_inspection_and_download(tmp_path: Path) -> None:
    engine = FakeEngine()
    service = DownloadService(engine, frozenset({"example"}))
    raw = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"
    canonical = "https://www.youtube.com/watch?v=DGbwtVtthu8"

    info = service.inspect(raw)
    result = service.download(
        job_id=JobId("youtube-mix"),
        url=raw,
        mode=DownloadMode.BEST,
        output_directory=tmp_path / "youtube-mix",
    )

    assert info.kind is MediaKind.VIDEO
    assert engine.inspection_urls == [canonical, canonical]
    assert len(engine.download_requests) == 1
    assert engine.download_requests[0].url == canonical
    assert result.kind is MediaKind.VIDEO


def test_generic_inspection_size_is_advisory_until_mode_is_selected(tmp_path: Path) -> None:
    service = DownloadService(
        FakeEngine(estimated_size_bytes=1_000, result_size_bytes=4),
        frozenset({"example"}),
        max_file_size_bytes=10,
    )

    info = service.inspect("https://example.com/media")
    result = service.download(
        job_id=JobId("job-advisory"),
        url=info.webpage_url,
        mode=DownloadMode.VIDEO_480,
        output_directory=tmp_path / "job-advisory",
    )

    assert info.estimated_size_bytes == 1_000
    assert result.file_size_bytes == 4


def test_rejects_oversized_selected_result(tmp_path: Path) -> None:
    service = DownloadService(
        FakeEngine(result_size_bytes=11),
        frozenset({"example"}),
        max_file_size_bytes=10,
    )

    with pytest.raises(MediaTooLargeError):
        service.download(
            job_id=JobId("job-too-large"),
            url="https://example.com/media",
            mode=DownloadMode.BEST,
            output_directory=tmp_path / "job-too-large",
        )


def test_engine_failure_is_attributed_to_the_inspected_source(tmp_path: Path) -> None:
    class FailingEngine(FakeEngine):
        def download(
            self,
            request: DownloadRequest,
            *,
            progress: Callable[[ProgressEvent], None] | None = None,
            is_cancelled: Callable[[], bool] | None = None,
        ) -> DownloadResult:
            del progress, is_cancelled
            raise MediaTooLargeError("engine raised after inspection")

    service = DownloadService(
        FailingEngine(result_size_bytes=6_355_000),
        frozenset({"example"}),
    )

    with pytest.raises(MediaTooLargeError) as raised:
        service.download(
            job_id=JobId("attributed-job"),
            url="https://example.com/stories/user/123/",
            mode=DownloadMode.BEST_ORIGINAL,
            output_directory=tmp_path / "attributed-job",
        )

    assert raised.value.source == "example"
    assert error_category(raised.value) is ErrorCategory.TOO_LARGE


def test_story_sized_result_within_limits_never_raises_too_large(tmp_path: Path) -> None:
    service = DownloadService(
        FakeEngine(result_size_bytes=6_355_000),
        frozenset({"example"}),
        max_file_size_bytes=49 * 1024 * 1024,
    )

    result = service.download(
        job_id=JobId("story-ok"),
        url="https://example.com/stories/user/3964254748584813861/",
        mode=DownloadMode.VIDEO_ORIGINAL,
        output_directory=tmp_path / "story-ok",
    )

    assert result.file_size_bytes == 6_355_000


def test_duration_genuinely_over_limit_is_too_large_and_attributed(tmp_path: Path) -> None:
    class LongEngine(FakeEngine):
        def inspect(self, url: str) -> MediaInfo:
            return MediaInfo(
                media_id="1",
                title="Long story",
                source="instagram",
                kind=MediaKind.VIDEO,
                webpage_url=url,
                duration_seconds=14400,
            )

    service = DownloadService(
        LongEngine(),
        frozenset({"instagram"}),
        max_duration_seconds=60,
        max_file_size_bytes=49 * 1024 * 1024,
    )

    with pytest.raises(MediaTooLargeError) as raised:
        service.inspect("https://www.instagram.com/stories/user/123/")

    assert raised.value.source == "instagram"
    assert error_category(raised.value) is ErrorCategory.TOO_LARGE
