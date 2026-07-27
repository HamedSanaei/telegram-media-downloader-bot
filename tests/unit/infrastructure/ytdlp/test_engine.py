from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DownloadFailedError,
    JobCancelledError,
    MediaTooLargeError,
    RateLimitedError,
    UnsafeUrlError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    JobId,
    MediaKind,
    OutputContainer,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.ytdlp import engine as engine_module
from telegram_media_bot.infrastructure.ytdlp.transcoder import VideoProbe


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
    downloaded_bytes: ClassVar[int] = 5

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.format_selector = lambda context: iter(context["formats"][-1:])

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
                        "downloaded_bytes": self.downloaded_bytes,
                        "total_bytes": 10,
                        "filename": "abc.webm",
                        "speed": 2,
                        "eta": 3,
                    }
                )
            template = self.options["outtmpl"]["default"]
            output = Path(self.options["paths"]["home"]) / template.replace(
                "%(id)s", "abc"
            ).replace("%(ext)s", "webm")
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


def test_inspect_offers_only_real_fixed_video_heights(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FormatYoutubeDL(FakeYoutubeDL):
        info: ClassVar[dict[str, Any]] = {
            "id": "video",
            "title": "Video",
            "extractor_key": "Youtube",
            "webpage_url": "https://example.test/video",
            "vcodec": "vp9",
            "acodec": "opus",
            "ext": "webm",
            "duration": 60,
            "formats": [
                {
                    "format_id": "audio",
                    "vcodec": "none",
                    "acodec": "opus",
                    "filesize": 10,
                },
                {
                    "format_id": "video-1080",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "height": 1080,
                    "width": 1920,
                    "fps": 30,
                    "filesize": 40,
                },
            ],
        }

        def build_format_selector(self, selector: str) -> Any:
            if selector.startswith("bestaudio"):
                return lambda context: iter(
                    item for item in reversed(context["formats"]) if item["acodec"] != "none"
                )

            def video_audio(context: dict[str, Any]) -> Any:
                videos = [item for item in context["formats"] if item["vcodec"] != "none"]
                audios = [item for item in context["formats"] if item["acodec"] != "none"]
                if not videos or not audios:
                    return iter(())
                return iter(
                    (
                        {
                            "requested_formats": [videos[-1], audios[-1]],
                            "vcodec": videos[-1]["vcodec"],
                            "acodec": audios[-1]["acodec"],
                        },
                    )
                )

            return video_audio

    monkeypatch.setattr(engine_module, "YoutubeDL", FormatYoutubeDL)
    info = engine_module.YtDlpEngine(_without_dns_checks(settings)).inspect(
        "https://example.test/video"
    )
    modes = {option.mode for option in info.format_options}

    assert DownloadMode.VIDEO_1080 in modes
    assert DownloadMode.VIDEO_2160 not in modes
    assert DownloadMode.VIDEO_1440 not in modes
    assert DownloadMode.VIDEO_720 not in modes
    assert DownloadMode.VIDEO_480 not in modes


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


def test_instagram_collection_returns_ordered_mp4_video_artifacts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InstagramYoutubeDL(FakeYoutubeDL):
        info: ClassVar[dict[str, Any]] = {
            "id": "collection",
            "title": "Highlight",
            "extractor_key": "Instagram",
            "webpage_url": "https://example.test/highlights/1",
            "entries": [
                {"id": "first", "vcodec": "h264", "acodec": "aac", "ext": "mp4"},
                {"id": "image", "vcodec": "none", "acodec": "none", "ext": "jpg"},
                {"id": "second", "vcodec": "h264", "acodec": "aac", "ext": "mp4"},
            ],
        }

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            if download:
                output = Path(self.options["paths"]["home"])
                output.mkdir(parents=True, exist_ok=True)
                (output / "first.mp4").write_bytes(b"first")
                (output / "image.jpg").write_bytes(b"image")
                (output / "second.mp4").write_bytes(b"second")
            return dict(self.info)

    monkeypatch.setattr(engine_module, "YoutubeDL", InstagramYoutubeDL)
    monkeypatch.setattr(
        engine_module,
        "is_guaranteed_container_compatible",
        lambda *_args: True,
    )
    configured = _without_dns_checks(settings)

    result = engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("instagram-collection"),
            url="https://example.test/highlights/1",
            mode=DownloadMode.BEST,
            output_directory=configured.storage.downloads_path() / "instagram-collection",
            container=OutputContainer.MP4,
            container_policy=ContainerPolicy.GUARANTEED,
            allow_collection=True,
        )
    )

    assert result.source == "instagram"
    assert result.kind is MediaKind.PLAYLIST
    assert [artifact.file_path.name for artifact in result.artifacts] == [
        "first.mp4",
        "second.mp4",
    ]
    assert result.total_file_size_bytes == 11


def test_best_original_vp9_mp4_under_limit_never_transcodes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InstagramReelYoutubeDL(FakeYoutubeDL):
        info: ClassVar[dict[str, Any]] = {
            "id": "DbQqWqBDLXS",
            "title": "Instagram Reel",
            "extractor_key": "Instagram",
            "webpage_url": "https://example.test/reel/DbQqWqBDLXS",
            "vcodec": "vp09.00.41.08",
            "acodec": "mp4a.40.2",
            "ext": "mp4",
            "height": 1920,
            "duration": 30,
            "requested_formats": [
                {"format_id": "990651570467829v", "vcodec": "vp9", "acodec": "none"},
                {"format_id": "989654117234241a", "vcodec": "none", "acodec": "aac"},
            ],
        }

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            if download:
                output = Path(self.options["paths"]["home"])
                output.mkdir(parents=True, exist_ok=True)
                (output / "DbQqWqBDLXS.mp4").write_bytes(b"x" * (7 * 1024 * 1024))
            return dict(self.info)

    def unexpected_transcode(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("BEST_ORIGINAL must never enter the transcoder")

    monkeypatch.setattr(engine_module, "YoutubeDL", InstagramReelYoutubeDL)
    monkeypatch.setattr(engine_module, "transcode_video_to_container", unexpected_transcode)
    monkeypatch.setattr(engine_module, "transcode_video_to_limit", unexpected_transcode)
    monkeypatch.setattr(
        engine_module,
        "probe_video",
        lambda _path: VideoProbe(
            30.0,
            1920,
            True,
            video_codec="vp9",
            audio_codec="aac",
            source_container="mov,mp4,m4a,3gp,3g2,mj2",
        ),
    )
    configured = _without_dns_checks(settings)

    result = engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("instagram-original"),
            url="https://example.test/reel/DbQqWqBDLXS",
            mode=DownloadMode.BEST_ORIGINAL,
            output_directory=configured.storage.downloads_path() / "instagram-original",
            container=OutputContainer.MP4,
            container_policy=ContainerPolicy.GUARANTEED,
            allow_collection=True,
        )
    )

    assert result.file_path.suffix == ".mp4"
    assert result.file_size_bytes == 7 * 1024 * 1024


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


def test_progress_guard_aborts_unknown_oversized_source_download(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OversizedYoutubeDL(FakeYoutubeDL):
        downloaded_bytes = 2 * 1024 * 1024 * 1024

    monkeypatch.setattr(engine_module, "YoutubeDL", OversizedYoutubeDL)
    settings = _without_dns_checks(settings)
    engine = engine_module.YtDlpEngine(settings)

    with pytest.raises(MediaTooLargeError):
        engine.download(
            DownloadRequest(
                job_id=JobId("oversized"),
                url="https://example.test/media",
                mode=DownloadMode.BEST,
                output_directory=settings.storage.downloads_path() / "oversized",
            )
        )


def test_oversized_selected_video_is_transcoded_at_requested_height(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class VideoYoutubeDL(FakeYoutubeDL):
        info: ClassVar[dict[str, Any]] = {
            "id": "video",
            "title": "Video",
            "extractor_key": "Youtube",
            "webpage_url": "https://example.test/video",
            "vcodec": "vp9",
            "acodec": "opus",
            "ext": "webm",
            "height": 720,
            "duration": 60,
        }

        def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
            info = super().extract_info(url, download=download)
            if download:
                output = Path(self.options["paths"]["home"]) / "abc.webm"
                output.write_bytes(b"x" * (2 * 1024 * 1024))
            return info

    calls: list[int] = []

    def fake_transcode(
        source: Path,
        *,
        target_height: int,
        max_size_bytes: int,
        is_cancelled: object,
    ) -> Path:
        calls.append(target_height)
        output = source.with_name("bounded.mp4")
        output.write_bytes(b"x" * (max_size_bytes // 2))
        source.unlink()
        return output

    raw = settings.model_dump()
    raw["media"]["max_file_size_mb"] = 1
    raw["media"]["max_source_size_mb"] = 10
    raw["telegram"]["max_upload_size_mb"] = 1
    configured = _without_dns_checks(Settings.model_validate(raw))
    monkeypatch.setattr(engine_module, "YoutubeDL", VideoYoutubeDL)
    monkeypatch.setattr(engine_module, "transcode_video_to_limit", fake_transcode)
    events: list[ProgressEvent] = []

    result = engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("transcode"),
            url="https://example.test/video",
            mode=DownloadMode.VIDEO_720,
            output_directory=configured.storage.downloads_path() / "transcode",
        ),
        progress=events.append,
    )

    assert calls == [720]
    assert result.file_path.name == "bounded.mp4"
    assert result.file_size_bytes == 512 * 1024
    assert any(event.status == "transcoding" for event in events)


def _without_dns_checks(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["security"]["reject_private_network_urls"] = False
    return Settings.model_validate(raw)
