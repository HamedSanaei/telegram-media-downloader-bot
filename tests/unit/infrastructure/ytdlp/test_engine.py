from __future__ import annotations

import errno
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    DownloadFailedError,
    JobCancelledError,
    LocalRuntimeError,
    MediaTooLargeError,
    MediaUnavailableError,
    RateLimitedError,
    UnsafeUrlError,
)
from telegram_media_bot.domain.failures import FailureStage
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    DownloadRequest,
    ErrorCategory,
    JobId,
    MediaKind,
    NativeVideoCodec,
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


@pytest.mark.parametrize(
    ("extractor", "expected_mode"),
    [
        ("Youtube", DownloadMode.YOUTUBE_THUMBNAIL),
        ("Soundcloud", DownloadMode.SOUNDCLOUD_ARTWORK),
    ],
)
def test_inspect_offers_highest_quality_artwork_without_persisting_cdn_url(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    extractor: str,
    expected_mode: DownloadMode,
) -> None:
    class ArtworkYoutubeDL(FakeYoutubeDL):
        info: ClassVar[dict[str, Any]] = {
            "id": "artwork",
            "title": "Artwork",
            "extractor_key": extractor,
            "webpage_url": "https://example.test/media",
            "thumbnail": "https://cdn.example.test/signed.jpg?token=secret",
            "vcodec": "none",
            "acodec": "opus",
            "ext": "webm",
        }

    monkeypatch.setattr(engine_module, "YoutubeDL", ArtworkYoutubeDL)

    info = engine_module.YtDlpEngine(_without_dns_checks(settings)).inspect(
        "https://example.test/media"
    )

    assert expected_mode in {option.mode for option in info.format_options}
    assert info.thumbnail_url is None


def test_missing_thumbnail_does_not_offer_artwork(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "YoutubeDL", FakeYoutubeDL)

    info = engine_module.YtDlpEngine(_without_dns_checks(settings)).inspect(
        "https://example.test/media"
    )

    assert DownloadMode.SOUNDCLOUD_ARTWORK not in {option.mode for option in info.format_options}


def test_youtube_mix_is_canonicalized_before_inspection(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CanonicalYoutubeDL(FakeYoutubeDL):
        received_url: ClassVar[str | None] = None
        received_options: ClassVar[dict[str, Any] | None] = None
        info: ClassVar[dict[str, Any]] = {
            "id": "DGbwtVtthu8",
            "title": "Single video",
            "extractor_key": "Youtube",
            "webpage_url": (
                "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"
            ),
            "vcodec": "h264",
            "acodec": "aac",
            "ext": "mp4",
        }

        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            type(self).received_options = options

        def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
            assert not download
            type(self).received_url = url
            return dict(self.info)

    monkeypatch.setattr(engine_module, "YoutubeDL", CanonicalYoutubeDL)
    raw = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"

    info = engine_module.YtDlpEngine(_without_dns_checks(settings)).inspect(raw)

    assert CanonicalYoutubeDL.received_url == ("https://www.youtube.com/watch?v=DGbwtVtthu8")
    assert CanonicalYoutubeDL.received_options is not None
    assert CanonicalYoutubeDL.received_options["noplaylist"] is True
    assert info.media_id == "DGbwtVtthu8"
    assert info.kind is MediaKind.VIDEO
    assert info.item_count is None
    assert info.webpage_url == CanonicalYoutubeDL.received_url


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
                    "ext": "webm",
                    "vcodec": "none",
                    "acodec": "opus",
                    "filesize": 10,
                },
                {
                    "format_id": "video-1080",
                    "ext": "webm",
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


def test_legacy_raw_youtube_mix_job_is_canonicalized_before_download(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CanonicalDownloadYoutubeDL(FakeYoutubeDL):
        received_url: ClassVar[str | None] = None
        received_options: ClassVar[dict[str, Any] | None] = None
        info: ClassVar[dict[str, Any]] = {
            "id": "DGbwtVtthu8",
            "title": "Single video",
            "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=DGbwtVtthu8",
            "vcodec": "h264",
            "acodec": "aac",
            "ext": "webm",
        }

        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            type(self).received_options = options

        def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
            type(self).received_url = url
            return super().extract_info(url, download=download)

    monkeypatch.setattr(engine_module, "YoutubeDL", CanonicalDownloadYoutubeDL)
    configured = _without_dns_checks(settings)
    raw = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"

    engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("legacy-youtube-mix"),
            url=raw,
            mode=DownloadMode.BEST,
            output_directory=configured.storage.downloads_path() / "legacy-youtube-mix",
        )
    )

    assert CanonicalDownloadYoutubeDL.received_url == (
        "https://www.youtube.com/watch?v=DGbwtVtthu8"
    )
    assert CanonicalDownloadYoutubeDL.received_options is not None
    assert CanonicalDownloadYoutubeDL.received_options["noplaylist"] is True


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
    monkeypatch.setattr(engine_module, "is_inline_video_streamable", lambda *_args: True)
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


def test_fast_mp4_selects_native_h264_and_never_transcodes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProductionYoutubeDL(FakeYoutubeDL):
        format_selector: Callable[[dict[str, Any]], Iterable[dict[str, Any]]]
        selected_ids: ClassVar[tuple[str, ...]] = ()
        formats: ClassVar[list[dict[str, Any]]] = [
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 10,
                "protocol": "https",
            },
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "fps": 30,
                "filesize": 40,
                "protocol": "https",
            },
            {
                "format_id": "399",
                "ext": "mp4",
                "vcodec": "av01.0.08M.08",
                "acodec": "none",
                "height": 1080,
                "fps": 60,
                "filesize": 80,
                "protocol": "https",
            },
        ]

        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            self.format_selector = self._select

        @staticmethod
        def _select(context: dict[str, Any]) -> list[dict[str, Any]]:
            videos = [item for item in context["formats"] if item["vcodec"] != "none"]
            audios = [item for item in context["formats"] if item["acodec"] != "none"]
            if not videos or not audios:
                return []
            video = videos[-1]
            audio = audios[-1]
            return [
                {
                    "format_id": f"{video['format_id']}+{audio['format_id']}",
                    "requested_formats": [video, audio],
                    "vcodec": video["vcodec"],
                    "acodec": audio["acodec"],
                }
            ]

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            selected = next(iter(self.format_selector({"formats": self.formats})))
            type(self).selected_ids = tuple(
                str(item["format_id"]) for item in selected["requested_formats"]
            )
            if download:
                output = Path(self.options["paths"]["home"])
                output.mkdir(parents=True, exist_ok=True)
                (output / "video.mp4").write_bytes(b"native")
            return {
                "id": "video",
                "title": "Video",
                "extractor_key": "Youtube",
                "webpage_url": "https://example.test/video",
                "ext": "mp4",
                "vcodec": selected["vcodec"],
                "acodec": selected["acodec"],
                "height": 1080,
                "fps": 30,
                "formats": self.formats,
                "requested_formats": selected["requested_formats"],
            }

    def unexpected_transcode(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("Fast MP4 must never start a codec transcode")

    monkeypatch.setattr(engine_module, "YoutubeDL", ProductionYoutubeDL)
    monkeypatch.setattr(engine_module, "transcode_video_to_container", unexpected_transcode)
    monkeypatch.setattr(
        engine_module,
        "is_guaranteed_container_compatible",
        lambda *_args: True,
    )
    monkeypatch.setattr(engine_module, "is_inline_video_streamable", lambda *_args: True)
    monkeypatch.setattr(
        engine_module,
        "probe_video",
        lambda _path: VideoProbe(
            2970.0,
            1080,
            True,
            video_codec="h264",
            audio_codec="aac",
            source_container="mov,mp4",
        ),
    )
    configured = _without_dns_checks(settings)

    result = engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("native-mp4"),
            url="https://example.test/video",
            mode=DownloadMode.VIDEO_1080,
            output_directory=configured.storage.downloads_path() / "native-mp4",
            container=OutputContainer.MP4,
            container_policy=ContainerPolicy.GUARANTEED,
        )
    )

    assert result.file_path.name == "video.mp4"
    assert result.file_size_bytes == len(b"native")
    assert ProductionYoutubeDL.selected_ids == ("137", "140")
    assert "399" not in ProductionYoutubeDL.selected_ids


def test_native_mp4_av1_downloads_without_transcoder(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Av1OnlyYoutubeDL(FakeYoutubeDL):
        format_selector: Callable[[dict[str, Any]], Iterable[dict[str, Any]]]
        formats: ClassVar[list[dict[str, Any]]] = [
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "filesize": 10,
            },
            {
                "format_id": "399",
                "ext": "mp4",
                "vcodec": "av01.0.08M.08",
                "acodec": "none",
                "height": 1080,
                "filesize": 80,
            },
        ]

        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            self.format_selector = self._select

        @staticmethod
        def _select(context: dict[str, Any]) -> list[dict[str, Any]]:
            videos = [item for item in context["formats"] if item["vcodec"] != "none"]
            audios = [item for item in context["formats"] if item["acodec"] != "none"]
            if not videos or not audios:
                return []
            return [{"requested_formats": [videos[-1], audios[-1]]}]

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            assert download
            selected = next(iter(self.format_selector({"formats": self.formats})))
            output = Path(self.options["paths"]["home"])
            output.mkdir(parents=True, exist_ok=True)
            (output / "video.mp4").write_bytes(b"native-av1")
            return {
                "id": "video",
                "title": "AV1 Video",
                "extractor_key": "Youtube",
                "webpage_url": "https://example.test/video",
                "ext": "mp4",
                "vcodec": "av01.0.08M.08",
                "acodec": "aac",
                "height": 1080,
                "formats": self.formats,
                "requested_formats": selected["requested_formats"],
            }

    def unexpected_transcode(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("Native AV1 MP4 must never start FFmpeg encoding")

    monkeypatch.setattr(engine_module, "YoutubeDL", Av1OnlyYoutubeDL)
    monkeypatch.setattr(engine_module, "transcode_video_to_container", unexpected_transcode)
    monkeypatch.setattr(
        engine_module,
        "is_guaranteed_container_compatible",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(engine_module, "is_inline_video_streamable", lambda *_args: False)
    monkeypatch.setattr(
        engine_module,
        "probe_video",
        lambda _path: VideoProbe(
            60.0,
            1080,
            True,
            video_codec="av1",
            audio_codec="aac",
            source_container="mov,mp4",
        ),
    )
    configured = _without_dns_checks(settings)

    result = engine_module.YtDlpEngine(configured).download(
        DownloadRequest(
            job_id=JobId("av1-only"),
            url="https://example.test/video",
            mode=DownloadMode.VIDEO_1080,
            output_directory=configured.storage.downloads_path() / "av1-only",
            container=OutputContainer.MP4,
            container_policy=ContainerPolicy.GUARANTEED,
            native_video_codec=NativeVideoCodec.AV1,
        )
    )

    assert result.file_path.name == "video.mp4"
    assert result.inline_video_streamable is False


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


class _YtDlpWrapperError(Exception):
    """Mirror of yt-dlp's DownloadError cause-chain shape (``exc_info`` tuple)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"unable to download format: {cause}")
        self.exc_info = (type(cause), cause, None)


def test_inspection_scratch_files_resolve_into_storage_temp_not_the_application_cwd(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression B: a read-only application cwd never receives inspection temp files."""
    app_root = tmp_path / "application-root"
    app_root.mkdir()
    monkeypatch.chdir(app_root)
    captured: dict[str, Any] = {}

    class ProbeYoutubeDL(FakeYoutubeDL):
        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            captured["options"] = options

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            # Replicates YoutubeDL._check_formats scratch resolution (yt-dlp 2026.07.04):
            # get_output_path('temp') joins paths['home'] with paths['temp']; an empty
            # result makes NamedTemporaryFile fall back to the process working directory.
            paths = self.options.get("paths") or {}
            resolved = str(Path(str(paths.get("home") or "")) / str(paths.get("temp") or ""))
            captured["temp_dir"] = Path(resolved) if resolved else None
            with tempfile.NamedTemporaryFile(
                suffix=".tmp", delete=False, dir=resolved or None
            ) as handle:
                captured["scratch"] = Path(handle.name)
            return dict(self.info)

    monkeypatch.setattr(engine_module, "YoutubeDL", ProbeYoutubeDL)
    configured = _without_dns_checks(settings)

    engine_module.YtDlpEngine(configured).inspect("https://example.test/media")

    temp_root = configured.storage.temp_path()
    scratch_dir = captured["temp_dir"]
    assert scratch_dir is not None
    assert scratch_dir.is_relative_to(temp_root)
    assert not scratch_dir.is_relative_to(app_root)
    assert Path(captured["scratch"]).is_relative_to(temp_root)
    # The private inspection workspace is removed after the run.
    assert not any(temp_root.glob("inspect-*"))
    # The simulated application source directory gained no files.
    assert list(app_root.iterdir()) == []


_YOUTUBE_FIXTURE_INFO: dict[str, Any] = {
    "id": "qRk26ZpZZMQ",
    "title": "Representative YouTube fixture",
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=qRk26ZpZZMQ",
    "duration": 212,
    "thumbnail": "https://i.ytimg.com/vi/qRk26ZpZZMQ/maxresdefault.jpg",
    "vcodec": "avc1.640028",
    "acodec": "mp4a.40.2",
    "ext": "mp4",
    "formats": [
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "filesize": 3_300_000,
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "width": 1920,
            "fps": 30,
            "filesize": 61_000_000,
        },
    ],
}


class _YouTubeProbeYoutubeDL(FakeYoutubeDL):
    """Inspect fake that requires yt-dlp-style format-probe scratch files to succeed."""

    info: ClassVar[dict[str, Any]] = _YOUTUBE_FIXTURE_INFO

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
            return iter(({"requested_formats": [videos[-1], audios[-1]]},))

        return video_audio

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        temp_dir = (self.options.get("paths") or {}).get("temp") or None
        assert temp_dir is not None, "inspection must configure a writable temp path"
        # Format probing (_check_formats) creates and writes a scratch file before metadata
        # processing can complete.
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False, dir=temp_dir) as handle:
            handle.write(b"probe")
            scratch = Path(handle.name)
        try:
            assert scratch.read_bytes() == b"probe"
        finally:
            scratch.unlink(missing_ok=True)
        return dict(self.info)


def test_youtube_fixture_inspection_succeeds_with_required_format_probe_tempfiles(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression D: representative YouTube inspection survives required tempfile usage."""
    monkeypatch.setattr(engine_module, "YoutubeDL", _YouTubeProbeYoutubeDL)
    configured = _without_dns_checks(settings)

    info = engine_module.YtDlpEngine(configured).inspect("https://youtu.be/qRk26ZpZZMQ")

    assert info.media_id == "qRk26ZpZZMQ"
    assert info.source == "youtube"
    assert info.kind is MediaKind.VIDEO
    assert info.format_options
    assert DownloadMode.VIDEO_1080 in {option.mode for option in info.format_options}
    # No inspection workspace is left behind under the storage temp root.
    assert not any(configured.storage.temp_path().glob("inspect-*"))


def test_read_only_filesystem_failure_maps_to_typed_local_runtime_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression E: the production EROFS failure keeps its real cause and attribution."""

    class ReadOnlyWorkspaceYoutubeDL(FakeYoutubeDL):
        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            cause = OSError(errno.EROFS, "Read-only file system", "/app/tmp1qe12lbf.tmp")
            raise _YtDlpWrapperError(cause)

    monkeypatch.setattr(engine_module, "YoutubeDL", ReadOnlyWorkspaceYoutubeDL)
    configured = _without_dns_checks(settings)

    with pytest.raises(LocalRuntimeError) as excinfo:
        engine_module.YtDlpEngine(configured).inspect("https://youtu.be/qRk26ZpZZMQ")

    error = excinfo.value
    assert not isinstance(error, DownloadFailedError)
    assert error.retryable is False
    assert error.os_errno == errno.EROFS
    assert error.adapter == "yt-dlp"
    assert error.failure_stage is FailureStage.INSPECTION
    assert error.source == "youtube"
    assert error_category(error) is ErrorCategory.LOCAL_RUNTIME
    assert "[Errno 30]" in str(error)
    assert "/app" not in str(error)
    assert "tmp1qe12lbf" not in str(error)
    # The private inspection workspace is cleaned up even on failure.
    assert not any(configured.storage.temp_path().glob("inspect-*"))


def test_download_failures_carry_download_stage_attribution(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DownloadFailureYoutubeDL(FakeYoutubeDL):
        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            raise RuntimeError("unexpected upstream interruption")

    monkeypatch.setattr(engine_module, "YoutubeDL", DownloadFailureYoutubeDL)
    configured = _without_dns_checks(settings)

    with pytest.raises(DownloadFailedError) as excinfo:
        engine_module.YtDlpEngine(configured).download(
            DownloadRequest(
                job_id=JobId("stage-attr"),
                url="https://example.test/media",
                mode=DownloadMode.BEST,
                output_directory=configured.storage.downloads_path() / "stage-attr",
            )
        )

    assert excinfo.value.adapter == "yt-dlp"
    assert excinfo.value.failure_stage is FailureStage.DOWNLOAD


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


@pytest.mark.parametrize("expected_video_indices", [(10,), (11, 15)])
def test_instagram_raw_discovery_requires_every_expected_video_slot(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    expected_video_indices: tuple[int, ...],
) -> None:
    class RawInstagramYoutubeDL:
        calls: ClassVar[list[tuple[bool, bool]]] = []

        def __init__(self, options: dict[str, Any]) -> None:
            assert "ignoreerrors" not in options

        def __enter__(self) -> RawInstagramYoutubeDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(
            self,
            _url: str,
            *,
            download: bool,
            process: bool = True,
        ) -> dict[str, Any]:
            self.calls.append((download, process))
            entries = [
                {
                    "id": "DZUtxnNDJg7" if index == 11 else f"PHOTO{index:02d}",
                    "extractor_key": "Instagram",
                    "formats": (
                        [{"format_id": "video", "vcodec": "avc1", "acodec": "none"}]
                        if index == 11
                        else []
                    ),
                }
                for index in range(1, 18)
            ]
            return {
                "_type": "playlist",
                "id": "DZUwLh3jEDk",
                "title": "Mixed carousel",
                "extractor_key": "Instagram",
                "entries": entries,
            }

        def sanitize_info(self, raw: Any) -> Any:
            return raw

    monkeypatch.setattr(engine_module, "YoutubeDL", RawInstagramYoutubeDL)
    configured = _without_dns_checks(settings)
    engine = engine_module.YtDlpEngine(configured)

    with pytest.raises(MediaUnavailableError, match="video slots"):
        engine.download_instagram_video_children(
            DownloadRequest(
                JobId("strict-carousel"),
                "https://www.instagram.com/p/DZUwLh3jEDk/",
                DownloadMode.BEST_ORIGINAL,
                configured.storage.downloads_path() / "strict-carousel",
            ),
            expected_parent_media_id="DZUwLh3jEDk",
            expected_total_slots=17,
            expected_video_indices=expected_video_indices,
        )

    assert RawInstagramYoutubeDL.calls == [(False, False)]


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
        **_kwargs: object,
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
