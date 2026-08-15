from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_COOKIE_UPLOAD_BYTES = 2 * 1024 * 1024


class CookieService(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    PINTEREST = "pinterest"
    SOUNDCLOUD = "soundcloud"


@dataclass(frozen=True, slots=True)
class CookieUpdateSummary:
    services: tuple[CookieService, ...]
    replaced: int
    added: int
