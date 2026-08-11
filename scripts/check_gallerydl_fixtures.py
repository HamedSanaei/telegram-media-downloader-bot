#!/usr/bin/env python3
"""Validate the pinned gallery-dl vendor fixtures against our normalized contract."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from telegram_media_bot.domain.errors import GalleryDlNoImagesError
from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection

EXPECTED: dict[str, tuple[str, tuple[MediaKind, ...]] | None] = {
    "instagram-single.json": ("instagram", (MediaKind.IMAGE,)),
    "instagram-carousel.json": (
        "instagram",
        (MediaKind.IMAGE, MediaKind.IMAGE),
    ),
    "instagram-mixed.json": ("instagram", (MediaKind.IMAGE, MediaKind.VIDEO)),
    "instagram-story.json": ("instagram", (MediaKind.IMAGE,)),
    "tiktok-photo.json": ("tiktok", (MediaKind.IMAGE, MediaKind.IMAGE)),
    "tiktok-video.json": None,
    "twitter-single.json": ("twitter", (MediaKind.IMAGE,)),
    "twitter-multiple.json": ("twitter", (MediaKind.IMAGE, MediaKind.IMAGE)),
    "twitter-mixed.json": ("twitter", (MediaKind.IMAGE, MediaKind.VIDEO)),
    "twitter-video.json": None,
    "pinterest-single.json": ("pinterest", (MediaKind.IMAGE,)),
    "pinterest-video.json": None,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check sanitized gallery-dl 1.32.8 fixture normalization"
    )
    parser.add_argument("--check-installed-version", action="store_true")
    args = parser.parse_args()
    if args.check_installed_version:
        completed = subprocess.run(
            [sys.executable, "-m", "gallery_dl", "--config-ignore", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.stdout.strip() != "1.32.8":
            raise SystemExit(
                f"Expected gallery-dl 1.32.8, found {completed.stdout.strip() or 'unknown'}"
            )
    fixtures = Path("tests/fixtures/gallerydl")
    for name, expected in EXPECTED.items():
        provider = name.split("-", maxsplit=1)[0]
        payload = (fixtures / name).read_bytes()
        if expected is None:
            try:
                parse_inspection(payload, expected_provider=provider, max_assets=30)
            except GalleryDlNoImagesError:
                continue
            raise SystemExit(f"{name}: expected the normalized video-only fallback signal")
        inspection = parse_inspection(payload, expected_provider=expected[0], max_assets=30)
        actual = tuple(asset.kind for asset in inspection.assets)
        if actual != expected[1]:
            raise SystemExit(f"{name}: expected {expected[1]}, found {actual}")
    print(f"Validated {len(EXPECTED)} sanitized gallery-dl 1.32.8 fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
