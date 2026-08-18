from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.cookie_health import CookieHealthState
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import GalleryDlCookiesExpiredError
from telegram_media_bot.infrastructure.cookies.probe import GalleryDlCookieProbe
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult


class StubRunner:
    def __init__(self, result: GalleryProcessResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[list[str]] = []

    async def run_async(
        self, args: Sequence[str], *, timeout_seconds: float
    ) -> GalleryProcessResult:
        del timeout_seconds
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _settings(settings: Settings, probes: dict[str, dict[str, object]]) -> Settings:
    raw = settings.model_dump()
    raw["cookie_health"]["probes"] = probes
    return Settings.model_validate(raw)


def _result(return_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> GalleryProcessResult:
    return GalleryProcessResult(
        return_code=return_code, stdout=stdout, stderr=stderr, elapsed_seconds=0.5
    )


async def test_unconfigured_provider_is_unverified(settings: Settings) -> None:
    probe = GalleryDlCookieProbe(_settings(settings, {}), runner=StubRunner())
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.UNVERIFIED
    assert result.safe_reason == "no authenticated probe configured for this provider"


async def test_successful_authenticated_probe_is_healthy(settings: Settings) -> None:
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}},
    )
    runner = StubRunner(_result(stdout=b'[2,"dir",{"id":"x","category":"instagram"}]\n'))
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.HEALTHY
    assert result.auth_required_endpoint is True
    assert result.probed_url == "https://www.instagram.com/stories/me/"
    assert result.elapsed_seconds is not None


async def test_auth_failure_is_auth_failed(settings: Settings) -> None:
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}},
    )
    runner = StubRunner(
        _result(return_code=1, stderr=b"gallery-dl: error: cookies have expired (HTTP 403)")
    )
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.AUTH_FAILED
    assert result.http_status == 403


async def test_rate_limit_is_check_error_not_auth_failure(settings: Settings) -> None:
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}},
    )
    runner = StubRunner(_result(return_code=1, stderr=b"HTTP error 429 too many requests"))
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.CHECK_ERROR
    assert "rate limited" in (result.safe_reason or "")


async def test_probe_timeout_is_check_error(settings: Settings) -> None:
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}},
    )
    runner = StubRunner(error=GalleryDlCookiesExpiredError("timeout"))
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    # A raised transport/process error is never confused with a definitive auth conclusion.
    assert result.status is CookieHealthState.CHECK_ERROR
    assert result.conclusive is False


async def test_empty_probe_output_is_unverified(settings: Settings) -> None:
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}},
    )
    runner = StubRunner(_result(stdout=b""))
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.UNVERIFIED
    assert "no events" in (result.safe_reason or "")


async def test_probe_never_marks_healthy_from_anonymous_success(settings: Settings) -> None:
    # A public URL probe that succeeds anonymously is still reported UNVERIFIED because
    # auth_required is false (or the endpoint is not known to require auth).
    configured = _settings(
        settings,
        {"instagram": {"url": "https://www.instagram.com/cristiano/", "auth_required": False}},
    )
    runner = StubRunner(
        _result(stdout=b'[3,"https://cdn.invalid/1.jpg",{"category":"instagram"}]\n')
    )
    probe = GalleryDlCookieProbe(configured, runner=runner)
    result = await probe.probe(CookieService.INSTAGRAM)
    assert result.status is CookieHealthState.UNVERIFIED


async def test_probe_uses_canonical_cookies(settings: Settings, tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = str(cookie_file)
    raw["cookie_health"]["probes"] = {
        "instagram": {"url": "https://www.instagram.com/stories/me/", "auth_required": True}
    }
    configured = Settings.model_validate(raw)
    runner = StubRunner(_result(stdout=b'[2,"d",{"id":"x","category":"instagram"}]\n'))
    probe = GalleryDlCookieProbe(configured, runner=runner)
    await probe.probe(CookieService.INSTAGRAM)
    args = runner.calls[0]
    assert "--cookies" in args
    assert str(cookie_file) in args
