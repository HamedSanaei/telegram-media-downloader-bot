from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import DownloadFailedError, RateLimitedError
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest, JobId, MediaKind
from telegram_media_bot.infrastructure.ytdlp import engine as engine_module


class FakeYoutubeDL:
    info: dict[str, Any] = {
        "id": "abc",
        "title": "Example",
        "extractor_key": "SoundcloudSet",
        "webpage_url": "https://example.test/media",
        "vcodec": "none",
        "acodec": "opus",
        "ext": "webm",
    }
    error: Exception | None = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        if self.error:
            raise self.error
        if download:
            template = self.options["outtmpl"]["default"]
            output = Path(template.replace("%(id)s", "abc").replace("%(ext)s", "webm"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"media")
        return dict(self.info)

    def sanitize_info(self, raw: Any) -> Any:
        return raw


def test_inspect_returns_project_owned_model(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "YoutubeDL", FakeYoutubeDL)
    engine = engine_module.YtDlpEngine(settings)

    info = engine.inspect("https://example.test/media")

    assert info.media_id == "abc"
    assert info.source == "soundcloud"
    assert info.kind is MediaKind.AUDIO


def test_download_returns_file_beneath_job_directory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "YoutubeDL", FakeYoutubeDL)
    engine = engine_module.YtDlpEngine(settings)
    job_dir = settings.storage.downloads_path() / "job-1"
    result = engine.download(
        DownloadRequest(
            job_id=JobId("job-1"),
            url="https://example.test/media",
            mode=DownloadMode.BEST,
            output_directory=job_dir,
        )
    )

    assert result.file_path == job_dir / "abc.webm"
    assert result.file_size_bytes == 5


def test_download_rejects_output_outside_storage(settings: Settings, tmp_path: Path) -> None:
    engine = engine_module.YtDlpEngine(settings)
    with pytest.raises(DownloadFailedError):
        engine.download(
            DownloadRequest(
                job_id=JobId("job-1"),
                url="https://example.test/media",
                mode=DownloadMode.BEST,
                output_directory=tmp_path.parent / "outside",
            )
        )


def test_upstream_errors_are_translated(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingYoutubeDL(FakeYoutubeDL):
        error = RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(engine_module, "YoutubeDL", FailingYoutubeDL)
    with pytest.raises(RateLimitedError):
        engine_module.YtDlpEngine(settings).inspect("https://example.test/media")
