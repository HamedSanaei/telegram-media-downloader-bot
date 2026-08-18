#!/usr/bin/env python3
"""Validate the pinned gallery-dl vendor fixtures against our normalized contract."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection

EXPECTED: dict[str, tuple[str, tuple[MediaKind, ...]]] = {
    "instagram-single.json": ("instagram", (MediaKind.IMAGE,)),
    "instagram-carousel.json": (
        "instagram",
        (MediaKind.IMAGE, MediaKind.IMAGE),
    ),
    "instagram-mixed.json": ("instagram", (MediaKind.IMAGE, MediaKind.VIDEO)),
    "instagram-story.json": ("instagram", (MediaKind.IMAGE,)),
    "instagram-reel-ytdl.json": ("instagram", (MediaKind.VIDEO,)),
    "tiktok-photo.json": ("tiktok", (MediaKind.IMAGE, MediaKind.IMAGE)),
    "tiktok-video.json": ("tiktok", (MediaKind.VIDEO,)),
    "twitter-single.json": ("twitter", (MediaKind.IMAGE,)),
    "twitter-multiple.json": ("twitter", (MediaKind.IMAGE, MediaKind.IMAGE)),
    "twitter-mixed.json": ("twitter", (MediaKind.IMAGE, MediaKind.VIDEO)),
    "twitter-video.json": ("twitter", (MediaKind.VIDEO,)),
    "pinterest-single.json": ("pinterest", (MediaKind.IMAGE,)),
    "pinterest-video.json": ("pinterest", (MediaKind.VIDEO,)),
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
    for name, (provider, expected_kinds) in EXPECTED.items():
        payload = (fixtures / name).read_bytes()
        inspection = parse_inspection(payload, expected_provider=provider, max_assets=30)
        actual = tuple(asset.kind for asset in inspection.assets)
        if actual != expected_kinds:
            raise SystemExit(f"{name}: expected {expected_kinds}, found {actual}")
    print(f"Validated {len(EXPECTED)} sanitized gallery-dl 1.32.8 fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
