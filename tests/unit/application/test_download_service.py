from collections.abc import Callable
from pathlib import Path

import pytest

from telegram_media_bot.application.services.download_service import DownloadService
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

    def inspect(self, url: str) -> MediaInfo:
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


def test_download_uses_project_contract(tmp_path: Path) -> None:
    service = DownloadService(FakeEngine(), frozenset({"example"}))
    result = service.download(
        job_id=JobId("job-1"),
        url="https://example.com/media",
        mode=DownloadMode.BEST,
        output_directory=tmp_path / "job-1",
    )
    assert result.file_path.read_bytes() == b"xxxx"


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
