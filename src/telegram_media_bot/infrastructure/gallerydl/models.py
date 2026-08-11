from __future__ import annotations

from dataclasses import dataclass

from telegram_media_bot.domain.models import MediaAsset


@dataclass(frozen=True, slots=True)
class GalleryInspection:
    provider: str
    post_id: str
    title: str
    assets: tuple[MediaAsset, ...]


@dataclass(frozen=True, slots=True)
class GalleryProcessResult:
    return_code: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
