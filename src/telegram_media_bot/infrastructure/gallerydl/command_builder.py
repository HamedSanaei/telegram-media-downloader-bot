from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from telegram_media_bot.bootstrap.config import GalleryDlSection
from telegram_media_bot.domain.errors import GalleryDlUnsupportedUrlError

_PROVIDER_PATTERNS = {
    "instagram": re.compile(
        r"^/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$|"
        r"^/stories/(?:highlights/[0-9]+|[A-Za-z0-9_.-]+(?:/[0-9]+)?)/?$|"
        r"^/[A-Za-z0-9_.-]+/(?:avatar|highlights)/?$"
    ),
    "tiktok": re.compile(r"^/@[^/]+/(?:video|photo)/[0-9]+/?$"),
    "twitter": re.compile(r"^/[A-Za-z0-9_]{1,15}/status/[0-9]+/?$"),
    "pinterest": re.compile(r"^/pin/[0-9]+/?$"),
}
_HOSTS = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "x.com": "twitter",
    "www.x.com": "twitter",
    "twitter.com": "twitter",
    "www.twitter.com": "twitter",
    "pinterest.com": "pinterest",
    "www.pinterest.com": "pinterest",
}


def is_gallery_social_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").casefold() in _HOSTS


def provider_for_single_item(url: str, enabled: frozenset[str]) -> str:
    parsed = urlsplit(url)
    provider = _HOSTS.get((parsed.hostname or "").casefold())
    if parsed.scheme.casefold() not in {"http", "https"} or provider not in enabled:
        raise GalleryDlUnsupportedUrlError("URL is outside the enabled gallery providers")
    if not _PROVIDER_PATTERNS[provider].fullmatch(parsed.path):
        raise GalleryDlUnsupportedUrlError("Bulk or unsupported gallery URL is not allowed")
    # /stories/me/ is the operator's own stories tray, never a user's story collection.
    if provider == "instagram" and parsed.path.casefold() == "/stories/me/":
        raise GalleryDlUnsupportedUrlError("Operator stories tray is not a bulk target")
    return provider


class GalleryDlCommandBuilder:
    def __init__(self, settings: GalleryDlSection, canonical_cookie_file: Path | None) -> None:
        self._settings = settings
        self._canonical_cookie_file = canonical_cookie_file

    def inspection(self, url: str) -> tuple[str, list[str]]:
        provider = provider_for_single_item(url, self._settings.enabled_platforms)
        args = self._base(provider)
        args.extend(("-o", "output.jsonl=true", "--dump-json", "--simulate", "--no-download", url))
        return provider, args

    def inspect_url(self, provider: str, url: str) -> list[str]:
        """Unrestricted JSON-Lines inspection used by probes and the highlight tray browser.

        The provider must already be validated by the caller; this method intentionally skips
        the single-item URL pattern so authenticated probe/tray endpoints are reachable.
        """
        args = self._base(provider)
        args.extend(("-o", "output.jsonl=true", "--dump-json", "--simulate", "--no-download", url))
        return args

    def download(
        self, url: str, workspace: Path, *, images_only: bool = False
    ) -> tuple[str, list[str]]:
        provider = provider_for_single_item(url, self._settings.enabled_platforms)
        args = self._base(provider)
        if images_only and provider == "instagram":
            args.extend(("-o", "extractor.instagram.videos=false"))
        args.extend(
            (
                "--directory",
                str(workspace),
                "--filename",
                "{num:04}-{type}.{extension}",
                "--restrict-filenames",
                "ascii",
                "--no-skip",
                url,
            )
        )
        return provider, args

    def version(self) -> list[str]:
        return [sys.executable, "-m", "gallery_dl", "--config-ignore", "--version"]

    def _base(self, provider: str) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--no-input",
            "--no-colors",
            "--sleep-request",
            str(self._settings.sleep_request_seconds),
        ]
        cookie = self._settings.cookie_for(provider, self._canonical_cookie_file)
        if cookie is not None:
            args.extend(("--cookies", str(cookie)))
        return args
