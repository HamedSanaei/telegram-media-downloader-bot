from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner
from telegram_media_bot.infrastructure.ytdlp import engine as ytdlp_engine_module

_HEADER = "# Netscape HTTP Cookie File\n"


def _record(domain: str, name: str, value: str) -> str:
    return f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n"


def _settings_with_cookie(settings: Settings, cookie: Path) -> Settings:
    raw = settings.model_dump()
    raw["security"]["reject_private_network_urls"] = False
    raw["yt_dlp"]["cookies_file"] = str(cookie)
    return Settings.model_validate(raw)


class _CookieReadingYoutubeDL:
    observations: ClassVar[list[tuple[Path, bytes]]] = []

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        cookie = Path(options["cookiefile"])
        self.observations.append((cookie, cookie.read_bytes()))
        self.format_selector = lambda context: iter(context["formats"][-1:])

    def __enter__(self) -> _CookieReadingYoutubeDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        assert download is False
        return {
            "id": "DGbwtVtthu8",
            "title": "Cookie reload fixture",
            "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=DGbwtVtthu8",
            "vcodec": "avc1.640028",
            "acodec": "mp4a.40.2",
            "ext": "mp4",
        }

    def sanitize_info(self, raw: Any) -> Any:
        return raw


class _CookieReadingGalleryRunner:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.observations: list[tuple[Path, bytes]] = []

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        **_kwargs: object,
    ) -> GalleryProcessResult:
        del timeout_seconds
        if "--version" in args:
            return GalleryProcessResult(0, b"1.32.8\n", b"", 0.01)
        cookie = Path(args[args.index("--cookies") + 1])
        self.observations.append((cookie, cookie.read_bytes()))
        return GalleryProcessResult(0, self._payload, b"", 0.01)


class _AllowValidator:
    def validate(self, url: str) -> str:
        return url


def test_next_ytdlp_job_reads_uploaded_youtube_cookie_without_restart(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / "cookies.txt"
    canonical.write_text(
        _HEADER
        + _record(".youtube.com", "SID", "youtube-old")
        + _record(".instagram.com", "sessionid", "instagram-stays"),
        encoding="utf-8",
    )
    configured = _settings_with_cookie(settings, canonical)
    _CookieReadingYoutubeDL.observations = []
    monkeypatch.setattr(ytdlp_engine_module, "YoutubeDL", _CookieReadingYoutubeDL)
    engine = ytdlp_engine_module.YtDlpEngine(configured)
    effective_cookie = configured.effective_cookie_file()
    assert effective_cookie is not None
    manager = NetscapeCookieManager(effective_cookie)

    engine.inspect("https://www.youtube.com/watch?v=DGbwtVtthu8")
    manager.merge((_HEADER + _record(".youtube.com", "SID", "youtube-new")).encode("utf-8"))
    engine.inspect("https://www.youtube.com/watch?v=DGbwtVtthu8")

    before, after = _CookieReadingYoutubeDL.observations
    assert before[0] == after[0] == configured.effective_cookie_file() == canonical.resolve()
    assert b"youtube-old" in before[1]
    assert b"youtube-new" in after[1]
    assert b"instagram-stays" in after[1]


def test_next_gallery_job_reads_uploaded_instagram_cookie_without_restart(
    settings: Settings,
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "cookies.txt"
    canonical.write_text(
        _HEADER
        + _record(".instagram.com", "sessionid", "instagram-old")
        + _record(".youtube.com", "SID", "youtube-stays"),
        encoding="utf-8",
    )
    configured = _settings_with_cookie(settings, canonical)
    payload = Path("tests/fixtures/gallerydl/instagram-single.json").read_bytes()
    runner = _CookieReadingGalleryRunner(payload)
    engine = GalleryDlEngine(configured, runner=cast(GalleryDlRunner, runner))
    engine._validator = cast(Any, _AllowValidator())
    effective_cookie = configured.effective_cookie_file()
    assert effective_cookie is not None
    manager = NetscapeCookieManager(effective_cookie)

    engine.inspect("https://instagram.com/p/abc123/")
    manager.merge(
        (_HEADER + _record(".instagram.com", "sessionid", "instagram-new")).encode("utf-8")
    )
    engine.inspect("https://instagram.com/p/abc123/")

    before, after = runner.observations
    assert before[0] == after[0] == configured.effective_cookie_file() == canonical.resolve()
    assert b"instagram-old" in before[1]
    assert b"instagram-new" in after[1]
    assert b"youtube-stays" in after[1]
