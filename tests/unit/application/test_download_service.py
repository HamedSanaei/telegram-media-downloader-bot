from pathlib import Path

import pytest

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.domain.errors import (
    InvalidUrlError,
    PlaylistNotAllowedError,
    UnsupportedSourceError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    JobId,
    MediaInfo,
    MediaKind,
)


class FakeEngine:
    def __init__(self, kind: MediaKind = MediaKind.VIDEO) -> None:
        self.kind = kind

    def inspect(self, url: str) -> MediaInfo:
        return MediaInfo(
            media_id="1",
            title="Example",
            source="example",
            kind=self.kind,
            webpage_url=url,
        )

    def download(self, request: DownloadRequest) -> DownloadResult:
        path = request.output_directory / "result.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return DownloadResult(
            job_id=request.job_id,
            media_id="1",
            title="Example",
            source="example",
            kind=MediaKind.VIDEO,
            file_path=path,
            file_size_bytes=4,
        )


def test_rejects_non_http_url() -> None:
    service = DownloadService(FakeEngine(), frozenset({"example"}))
    with pytest.raises(InvalidUrlError):
        service.inspect("file:///etc/passwd")


def test_rejects_url_credentials() -> None:
    with pytest.raises(InvalidUrlError):
        DownloadService.validate_url("https://user:pass@example.com/media")


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


def test_download_uses_project_contract(tmp_path: Path) -> None:
    service = DownloadService(FakeEngine(), frozenset({"example"}))
    result = service.download(
        job_id=JobId("job-1"),
        url="https://example.com/media",
        mode=DownloadMode.BEST,
        output_directory=tmp_path / "job-1",
    )
    assert result.file_path.read_bytes() == b"data"
