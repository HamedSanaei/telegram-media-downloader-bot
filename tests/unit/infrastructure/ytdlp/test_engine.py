from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DownloadFailedError,
    JobCancelledError,
    RateLimitedError,
    UnsafeUrlError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    DownloadRequest,
    JobId,
    MediaKind,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.ytdlp import engine as engine_module


class FakeYoutubeDL:
    info: ClassVar[dict[str, Any]] = {
        "id": "abc",
        "title": "Example",
        "extractor_key": "SoundcloudSet",
        "webpage_url": "https://example.test/media",
        "vcodec": "none",
        "acodec": "opus",
        "ext": "webm",
    }
    error: ClassVar[Exception | None] = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        if self.error:
            raise self.error
        if download:
            for hook in self.options.get("progress_hooks", []):
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 5,
                        "total_bytes": 10,
                        "speed": 2,
                        "eta": 3,
                    }
                )
            template = self.options["outtmpl"]["default"]
            output = Path(template.replace("%(id)s", "abc").replace("%(ext)s", "webm"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"media")
        return dict(self.info)

    def sanitize_info(self, raw: Any) -> Any:
        return raw


def test_inspect_returns_project_owned_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "YoutubeDL", FakeYoutubeDL)
    settings = _without_dns_checks(settings)
    engine = engine_module.YtDlpEngine(settings)

    info = engine.inspect("https://example.test/media")

    assert info.media_id == "abc"
    assert info.source == "soundcloud"
    assert info.kind is MediaKind.AUDIO


def test_download_returns_file_beneath_job_directory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "YoutubeDL", FakeYoutubeDL)
    settings = _without_dns_checks(settings)
    engine = engine_module.YtDlpEngine(settings)
    job_dir = settings.storage.downloads_path() / "job-1"
    events: list[ProgressEvent] = []
    result = engine.download(
        DownloadRequest(
            job_id=JobId("job-1"),
            url="https://example.test/media",
            mode=DownloadMode.BEST,
            output_directory=job_dir,
        ),
        progress=events.append,
    )

    assert result.file_path == job_dir / "abc.webm"
    assert result.file_size_bytes == 5
    assert events[0].percent == 50
    assert events[0].eta_seconds == 3


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


def test_upstream_errors_are_translated(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingYoutubeDL(FakeYoutubeDL):
        error = RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(engine_module, "YoutubeDL", FailingYoutubeDL)
    settings = _without_dns_checks(settings)
    with pytest.raises(RateLimitedError):
        engine_module.YtDlpEngine(settings).inspect("https://example.test/media")


def test_cancellation_stops_before_upstream_download(settings: Settings) -> None:
    engine = engine_module.YtDlpEngine(settings)
    with pytest.raises(JobCancelledError):
        engine.download(
            DownloadRequest(
                job_id=JobId("cancelled"),
                url="https://example.com/media",
                mode=DownloadMode.BEST,
                output_directory=settings.storage.downloads_path() / "cancelled",
            ),
            is_cancelled=lambda: True,
        )


def test_extracted_playlist_entry_urls_are_revalidated(settings: Settings) -> None:
    engine = engine_module.YtDlpEngine(settings)
    with pytest.raises(UnsafeUrlError):
        engine._validate_info_urls({"entries": [{"url": "http://127.0.0.1/private"}]})


def _without_dns_checks(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["security"]["reject_private_network_urls"] = False
    return Settings.model_validate(raw)
